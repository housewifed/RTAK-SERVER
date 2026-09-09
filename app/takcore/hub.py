"""Central hub: routes CoT between TAK clients, the web UI, and the store."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional, Set

from .cot import CotEvent
from .store import Store

log = logging.getLogger("takcore.hub")


class TakSession:
    """One connected TAK device (ATAK/iTAK) socket session."""

    _counter = 0

    def __init__(self, writer: asyncio.StreamWriter, peer: str) -> None:
        TakSession._counter += 1
        self.id = TakSession._counter
        self.writer = writer
        self.peer = peer
        self.uid: Optional[str] = None  # learned from first SA event
        self.cn: Optional[str] = None  # peer cert commonName, when TLS+mTLS
        self.callsign: Optional[str] = None
        self.connected_at = time.time()

    def send(self, data: bytes) -> None:
        if not self.writer.is_closing():
            self.writer.write(data)


class Hub:
    """Fan-out point. Everything that happens flows through here.

    - TAK sessions register/unregister as devices connect.
    - Every event from a device is re-broadcast to all *other* devices
      (classic TAK server behaviour) and converted to JSON for web clients.
    - ``latest_sa`` keeps the newest situational-awareness event per uid so
      newly connected devices and browsers get the current picture
      immediately.
    - Web subscribers are thread-safe queues (the HTTP server runs in
      threads; the TAK server runs on the asyncio loop).
    """

    # Enqueued to a web subscriber that fell behind; the SSE handler breaks on
    # it so the browser's EventSource reconnects and re-syncs from a snapshot.
    CLOSE = object()

    def __init__(self, store: Store, registry=None) -> None:
        self.store = store
        self.registry = registry  # StreamRegistry, set by __main__
        self.loop = None  # asyncio loop, set by __main__ for cross-thread sends
        self.sessions: Set[TakSession] = set()
        self.latest_sa: Dict[str, bytes] = {}
        self._uid_owner: Dict[str, TakSession] = {}
        self._web_subs: Set["queue.Queue[str]"] = set()
        self._web_lock = threading.Lock()

    # -- TAK side -----------------------------------------------------------

    def register(self, session: TakSession) -> None:
        self.sessions.add(session)
        log.info("session %d connected from %s (%d online)",
                 session.id, session.peer, len(self.sessions))
        # replay current situational awareness to the newcomer
        for raw in self.latest_sa.values():
            session.send(raw)

    def unregister(self, session: TakSession) -> None:
        self.sessions.discard(session)
        log.info("session %d (%s) disconnected (%d online)",
                 session.id, session.callsign or session.peer, len(self.sessions))
        if session.uid:
            self._push_web({"kind": "offline", "uid": session.uid})
        for uid in [u for u, s in self._uid_owner.items() if s is session]:
            del self._uid_owner[uid]

    def _check_self_uid(self, ev: CotEvent, origin: Optional[TakSession]) -> bool:
        """Anti-spoof: a device may only report its OWN position. The first
        self-position a session sends binds that uid to it; a self-position for
        that uid from a different live session is rejected. Non-self events
        (markers/chat carrying other uids) always pass."""
        if origin is None:
            return True  # internal source (e.g. camera/registry)
        is_self = origin.uid is None or ev.uid == origin.uid
        if not is_self:
            return True
        owner = self._uid_owner.get(ev.uid)
        if owner is not None and owner is not origin:
            # A phone that changes network (Wi-Fi -> cellular) reconnects from
            # a new address while the old socket lingers as a zombie ESTAB, so
            # the binding cannot be the socket: identity is what the client
            # certificate proves. The same CN reclaims its own uid; a different
            # one - or no certificate at all, where nothing is proven - does
            # not. Without this the device is locked out of reporting its own
            # position until the dead socket is reaped, which can take hours.
            if not (origin.cn and origin.cn == owner.cn):
                log.warning("spoof rejected: self-position for %s from %s "
                            "(cn=%s) already owned by %s (cn=%s)", ev.uid,
                            origin.peer, origin.cn, owner.peer, owner.cn)
                return False
            log.info("uid %s reclaimed by %s (cn=%s); dropping the superseded "
                     "session from %s", ev.uid, origin.peer, origin.cn,
                     owner.peer)
            self._evict(owner)
        self._uid_owner[ev.uid] = origin
        return True

    def _evict(self, session: TakSession) -> None:
        """Drop a session that the same device has superseded. Deliberately not
        unregister(): that would announce the device offline to the web at the
        moment it is coming back."""
        self.sessions.discard(session)
        for uid in [u for u, s in self._uid_owner.items() if s is session]:
            del self._uid_owner[uid]
        try:
            session.writer.close()
        except Exception:  # noqa: BLE001
            log.debug("closing superseded session failed", exc_info=True)

    def publish(self, ev: CotEvent, origin: Optional[TakSession]) -> None:
        """Handle one parsed event from a device (or internal source)."""
        if ev.is_position and not self._check_self_uid(ev, origin):
            return  # spoofed self-position: drop entirely (no store, no fanout)

        if origin is not None and origin.uid is None and ev.is_position:
            origin.uid = ev.uid
            origin.callsign = ev.callsign

        if ev.is_position:
            self._handle_position(ev, origin)
        if ev.is_chat:
            self._handle_chat(ev)
        if ev.is_emergency:
            self._handle_emergency(ev)

        self._broadcast_to_others(ev, origin)

    def _handle_position(self, ev: CotEvent,
                         origin: Optional[TakSession] = None) -> None:
        self.latest_sa[ev.uid] = ev.raw
        state = self._state_from_event(ev)
        self.store.upsert_device(state)
        msg = {"kind": "position", **state}
        if origin is not None and origin.cn:
            msg["cn"] = origin.cn  # provenance for audit; web UI ignores it
        self._push_web(msg)
        if ev.video_url and self.registry is not None:
            try:
                self.registry.register_cot_video(ev.uid, ev.video_url)
            except Exception:  # noqa: BLE001
                log.exception("cot video registration failed for %s", ev.uid)

    def _handle_chat(self, ev: CotEvent) -> None:
        try:
            row = self.store.add_chat(ev.chat_sender, ev.chat_sender_uid,
                                      ev.chat_room, ev.chat_message)
            self._push_web({"kind": "chat", **row})
            log.info("chat from %s: %s", ev.chat_sender or ev.uid,
                     ev.chat_message)
        except Exception:  # noqa: BLE001
            log.exception("chat handling failed")

    def _handle_emergency(self, ev: CotEvent) -> None:
        try:
            if ev.emergency_cancel:
                self.store.clear_alert(ev.uid)
                self._push_web({"kind": "alert_clear", "uid": ev.uid})
                log.info("emergency cleared for %s", ev.callsign or ev.uid)
            else:
                row = self.store.raise_alert(
                    ev.uid, ev.callsign, ev.emergency_type, ev.lat, ev.lon)
                self._push_web({"kind": "alert", **row})
                log.warning("EMERGENCY (%s) from %s at %s,%s",
                            ev.emergency_type, ev.callsign or ev.uid,
                            ev.lat, ev.lon)
        except Exception:  # noqa: BLE001
            log.exception("emergency handling failed")

    def _broadcast_to_others(self, ev: CotEvent,
                             origin: Optional[TakSession]) -> None:
        for s in list(self.sessions):
            if s is origin:
                continue
            try:
                s.send(ev.raw)
            except Exception:  # noqa: BLE001 - never let one bad socket stop the loop
                self.sessions.discard(s)

    def _do_broadcast(self, raw: bytes) -> None:
        for s in list(self.sessions):
            try:
                s.send(raw)
            except Exception:  # noqa: BLE001
                self.sessions.discard(s)

    def broadcast_raw(self, raw: bytes) -> None:
        """Send a raw CoT to every connected device (used for outgoing chat).

        Thread-safe: schedules the write on the asyncio loop when called from
        an HTTP handler thread.
        """
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._do_broadcast, raw)
        else:
            self._do_broadcast(raw)

    # -- Web side -----------------------------------------------------------

    def subscribe_web(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=1000)
        with self._web_lock:
            self._web_subs.add(q)
        return q

    def unsubscribe_web(self, q: "queue.Queue[str]") -> None:
        with self._web_lock:
            self._web_subs.discard(q)

    def _push_web(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg)
        with self._web_lock:
            for q in list(self._web_subs):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # subscriber fell behind: drop it and leave a CLOSE so the
                    # handler ends instead of silently starving forever
                    self._web_subs.discard(q)
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(self.CLOSE)
                    except queue.Full:  # pragma: no cover
                        pass

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _state_from_event(ev: CotEvent) -> Dict[str, Any]:
        return {
            "uid": ev.uid,
            "callsign": ev.callsign,
            "cot_type": ev.type,
            "team": ev.group_name,
            "role": ev.group_role,
            "device": ev.device,
            "platform": ev.platform,
            "lat": ev.lat,
            "lon": ev.lon,
            "hae": ev.hae,
            "course": ev.course,
            "speed": ev.speed,
            "battery": ev.battery,
            "video_url": ev.video_url,
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "sessions": len(self.sessions),
            "known_devices": len(self.latest_sa),
            "web_subscribers": len(self._web_subs),
        }
