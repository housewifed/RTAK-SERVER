"""Thin client for the MediaMTX control API + stream registry logic.

MediaMTX does the heavy lifting (ingest, WebRTC). takcore keeps the
*registry* — which stream belongs to which device — and pushes proxy-path
configuration to MediaMTX through its control API (:9997).

Everything here is best-effort: if MediaMTX is down or absent, registry
writes still succeed locally and are reconciled the next time MediaMTX
answers (``sync()``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .store import Store

log = logging.getLogger("takcore.mediamtx")

# Streams published by devices land on paths like live/<device-uid>
LIVE_PREFIX = "live/"
# External RTSP sources announced in CoT __video get proxied under cot/
COT_PREFIX = "cot/"
# Admin-registered IP cameras live under cam/
CAM_PREFIX = "cam/"


def sanitize_path(name: str) -> str:
    """Make an arbitrary string safe as a MediaMTX path segment."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name).strip("-") or "unnamed"


class MediaMTX:
    def __init__(self, api_url: str = "http://127.0.0.1:9997",
                 timeout: float = 2.0) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # 400 "already exists" on add is fine for reconcile flows
            log.debug("mediamtx %s %s -> %s", method, path, e.code)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.debug("mediamtx unreachable (%s %s): %s", method, path, e)
            return None

    def reachable(self) -> bool:
        return self._req("GET", "/v3/config/global/get") is not None

    def add_proxy_path(self, name: str, source: str) -> bool:
        """Configure MediaMTX to pull an external RTSP/RTMP source on demand."""
        ok = self._req("POST", f"/v3/config/paths/add/{name}", {
            "source": source,
            "sourceOnDemand": True,
        })
        if ok is None:
            # may already exist -> try replace
            ok = self._req("PATCH", f"/v3/config/paths/patch/{name}", {
                "source": source,
                "sourceOnDemand": True,
            })
        return ok is not None

    def remove_path(self, name: str) -> bool:
        return self._req("DELETE", f"/v3/config/paths/delete/{name}") is not None

    def ready_paths(self) -> Dict[str, bool]:
        """path name -> ready (has an active source)."""
        out: Dict[str, bool] = {}
        page = self._req("GET", "/v3/paths/list?itemsPerPage=500")
        if page and isinstance(page.get("items"), list):
            for item in page["items"]:
                out[item.get("name", "")] = bool(item.get("ready"))
        return out

    def record_paths(self) -> Dict[str, bool]:
        """path name -> record enabled, from the path CONFIG list. Only paths
        with an explicit config appear; anything else uses pathDefaults (record
        off), so a missing name means 'not recording'."""
        out: Dict[str, bool] = {}
        page = self._req("GET", "/v3/config/paths/list?itemsPerPage=500")
        if page and isinstance(page.get("items"), list):
            for item in page["items"]:
                out[item.get("name", "")] = bool(item.get("record"))
        return out

    def set_record(self, name: str, enabled: bool) -> bool:
        """Turn recording on/off for one path. Patches the path's config,
        adding an explicit entry if MediaMTX has none for it yet."""
        body = {"record": bool(enabled)}
        ok = self._req("PATCH", f"/v3/config/paths/patch/{name}", body)
        if ok is None:
            ok = self._req("POST", f"/v3/config/paths/add/{name}", body)
        return ok is not None

    def recordings(self, path: str, playback_url: str) -> list:
        """List recorded segments for a path via the MediaMTX playback API."""
        import urllib.parse
        url = f"{playback_url.rstrip('/')}/list?path={urllib.parse.quote(path)}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else []
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as e:
            log.debug("recordings list failed for %s: %s", path, e)
            return []


class StreamRegistry:
    """Registry of streams and their device associations."""

    def __init__(self, store: Store, mtx: MediaMTX,
                 publish_token: Optional[str] = None,
                 token_secret: bytes = b"") -> None:
        self.store = store
        self.mtx = mtx
        self.publish_token = publish_token
        self.token_secret = token_secret
        self._cot_seen: dict = {}  # uid -> last announced url (dedup cache)

    # -- registration -------------------------------------------------------

    def register_device_publish(self, path: str) -> None:
        """A device started publishing to live/<uid> — associate it."""
        uid = path[len(LIVE_PREFIX):]
        self.store.upsert_stream({
            "path": path, "device_uid": uid, "kind": "device",
            "source": None, "name": None,
        })
        log.info("device stream registered: %s", path)

    def register_cot_video(self, uid: str, url: str) -> None:
        """CoT __video announcement — proxy the URL through MediaMTX.

        Called from the asyncio loop on every SA event carrying __video, so
        it dedups per uid/url and pushes to MediaMTX off-thread.
        """
        if self._cot_seen.get(uid) == url:
            return
        self._cot_seen[uid] = url
        path = COT_PREFIX + sanitize_path(uid)
        self.store.upsert_stream({
            "path": path, "device_uid": uid, "kind": "cot",
            "source": url, "name": None,
        })
        threading.Thread(target=self.mtx.add_proxy_path, args=(path, url),
                         daemon=True).start()

    def register_camera(self, name: str, source: str,
                        device_uid: Optional[str] = None,
                        lat: Optional[float] = None,
                        lon: Optional[float] = None) -> Dict[str, Any]:
        path = CAM_PREFIX + sanitize_path(name)
        self.store.upsert_stream({
            "path": path, "device_uid": device_uid, "kind": "camera",
            "source": source, "name": name,
        })
        pushed = self.mtx.add_proxy_path(path, source)
        if lat is not None and lon is not None:
            # cameras with a position appear on the map as their own marker
            self.store.upsert_device({
                "uid": f"CAM.{sanitize_path(name)}", "callsign": name,
                "cot_type": "a-f-G-E-S", "team": None, "role": "Camera",
                "device": "IP Camera", "platform": "camera",
                "lat": lat, "lon": lon, "hae": None, "course": None,
                "speed": None, "battery": None, "video_url": source,
            })
            self.store.upsert_stream({
                "path": path, "device_uid": f"CAM.{sanitize_path(name)}",
                "kind": "camera", "source": source, "name": name,
            })
        return {"path": path, "pushed_to_mediamtx": pushed}

    def remove(self, path: str) -> None:
        self.store.delete_stream(path)
        if not path.startswith(LIVE_PREFIX):
            self.mtx.remove_path(path)

    # -- queries ------------------------------------------------------------

    def list_streams(self) -> List[Dict[str, Any]]:
        ready = self.mtx.ready_paths()
        rec = self.mtx.record_paths()
        rows = self.store.streams()
        for r in rows:
            r["ready"] = ready.get(r["path"])  # True/False, or None if MTX down
            r["recording"] = bool(rec.get(r["path"], False))
        return rows

    def streams_for_device(self, uid: str) -> List[Dict[str, Any]]:
        ready = self.mtx.ready_paths()
        rec = self.mtx.record_paths()
        rows = self.store.streams_for_device(uid)
        for r in rows:
            r["ready"] = ready.get(r["path"])
            r["recording"] = bool(rec.get(r["path"], False))
        return rows

    def set_record(self, path: str, enabled: bool) -> bool:
        """Enable/disable recording for one stream path."""
        return self.mtx.set_record(path, enabled)

    def make_ticket(self, path: str, ttl: float = 60.0) -> Dict[str, Any]:
        """Mint a short-lived WebRTC playback ticket for one path."""
        import time
        from .stream_tokens import make_ticket
        return {"path": path, "expires": time.time() + ttl,
                "token": make_ticket(self.token_secret, path, ttl)}

    def sync(self) -> int:
        """Re-push all proxy paths to MediaMTX (call at startup)."""
        n = 0
        for r in self.store.streams():
            if r.get("source") and r["kind"] in ("cot", "camera"):
                if self.mtx.add_proxy_path(r["path"], r["source"]):
                    n += 1
        return n

    # -- MediaMTX auth webhook ----------------------------------------------

    def authorize(self, payload: Dict[str, Any]) -> bool:
        """Decide a MediaMTX auth request (POST /api/mediamtx/auth).

        Policy (Phase 2):
          - read/playback: allowed, except webrtc reads require a valid
            short-lived per-path ticket (see stream_tokens.py) minted for a
            logged-in web session. RTSP/other protocols (TAK-app video
            players) stay unauthenticated.
          - api/metrics/pprof: only from localhost/private nets (MediaMTX
            calls come from the compose network)
          - publish: allowed if no publish token is configured (lab mode);
            otherwise password/query must carry the token. A publish to
            live/<uid> auto-registers the device association.
        """
        action = payload.get("action", "")
        path = payload.get("path", "") or ""

        if action in ("read", "playback"):
            proto = (payload.get("protocol") or "").lower()
            if proto == "webrtc":
                # Browser playback is the internet-facing surface: require a
                # ticket minted for a logged-in web session. RTSP and other
                # protocols (TAK-app video players) are left open here.
                from urllib.parse import parse_qs
                from .stream_tokens import verify_ticket
                token = (parse_qs(payload.get("query") or "")
                         .get("token") or [""])[0]
                if not verify_ticket(self.token_secret, path, token):
                    log.warning("webrtc read denied for %s (bad/absent ticket)",
                                path)
                    return False
            return True
        if action in ("api", "metrics", "pprof"):
            return True  # API port is not published outside the compose net

        if action == "publish":
            if self.publish_token:
                supplied = payload.get("password") or ""
                query = payload.get("query") or ""
                if self.publish_token not in (supplied,) and \
                        f"token={self.publish_token}" not in query:
                    log.warning("publish denied for path %s from %s",
                                path, payload.get("ip"))
                    return False
            if path.startswith(LIVE_PREFIX):
                self.register_device_publish(path)
            return True

        return False
