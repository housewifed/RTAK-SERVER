"""SQLite persistence (stdlib).

The scaffold uses SQLite so the whole stack runs with zero external
dependencies. The interface is deliberately small so a PostgreSQL/PostGIS
implementation can replace it in a later milestone without touching the
rest of the server.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    uid        TEXT PRIMARY KEY,
    callsign   TEXT,
    cot_type   TEXT,
    team       TEXT,
    role       TEXT,
    device     TEXT,
    platform   TEXT,
    lat        REAL,
    lon        REAL,
    hae        REAL,
    course     REAL,
    speed      REAL,
    battery    INTEGER,
    video_url  TEXT,
    first_seen REAL,
    last_seen  REAL
);
CREATE TABLE IF NOT EXISTS positions (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    ts  REAL NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    hae REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_uid_ts ON positions (uid, ts);
CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions (ts);
CREATE TABLE IF NOT EXISTS streams (
    path       TEXT PRIMARY KEY,
    device_uid TEXT,
    kind       TEXT NOT NULL,          -- device | cot | camera
    source     TEXT,                   -- upstream URL for proxied paths
    name       TEXT,
    created    REAL,
    updated    REAL
);
CREATE INDEX IF NOT EXISTS idx_streams_device ON streams (device_uid);
CREATE TABLE IF NOT EXISTS chat (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    sender     TEXT,
    sender_uid TEXT,
    room       TEXT,
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat (ts);
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL,
    callsign   TEXT,
    alert_type TEXT,
    lat        REAL,
    lon        REAL,
    raised     REAL NOT NULL,
    cleared    REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_uid ON alerts (uid);
CREATE TABLE IF NOT EXISTS users (
    username  TEXT PRIMARY KEY,
    pw_hash   TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'viewer',
    created   REAL
);
CREATE TABLE IF NOT EXISTS enroll_users (
    username    TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    callsign    TEXT,
    expires     REAL,               -- unix ts, NULL = never
    max_uses    INTEGER,            -- NULL = unlimited
    uses        INTEGER DEFAULT 0,
    created     REAL,
    last_enroll REAL
);
"""


class Store:
    """Thread-safe SQLite store. Position rate for 100+ devices is well
    within SQLite's comfort zone (WAL mode, single writer)."""

    def __init__(self, path: str = "takcore.db") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def upsert_device(self, state: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO devices (uid, callsign, cot_type, team, role, device,
                                     platform, lat, lon, hae, course, speed,
                                     battery, video_url, first_seen, last_seen)
                VALUES (:uid, :callsign, :cot_type, :team, :role, :device,
                        :platform, :lat, :lon, :hae, :course, :speed,
                        :battery, :video_url, :now, :now)
                ON CONFLICT(uid) DO UPDATE SET
                    callsign  = COALESCE(excluded.callsign, devices.callsign),
                    cot_type  = COALESCE(excluded.cot_type, devices.cot_type),
                    team      = COALESCE(excluded.team, devices.team),
                    role      = COALESCE(excluded.role, devices.role),
                    device    = COALESCE(excluded.device, devices.device),
                    platform  = COALESCE(excluded.platform, devices.platform),
                    lat       = COALESCE(excluded.lat, devices.lat),
                    lon       = COALESCE(excluded.lon, devices.lon),
                    hae       = COALESCE(excluded.hae, devices.hae),
                    course    = COALESCE(excluded.course, devices.course),
                    speed     = COALESCE(excluded.speed, devices.speed),
                    battery   = COALESCE(excluded.battery, devices.battery),
                    video_url = COALESCE(excluded.video_url, devices.video_url),
                    last_seen = excluded.last_seen
                """,
                {**state, "now": now},
            )
            if state.get("lat") is not None and state.get("lon") is not None:
                self._db.execute(
                    "INSERT INTO positions (uid, ts, lat, lon, hae) VALUES (?,?,?,?,?)",
                    (state["uid"], now, state["lat"], state["lon"], state.get("hae")),
                )
            self._db.commit()

    def devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM devices ORDER BY last_seen DESC")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def device(self, uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM devices WHERE uid = ?", (uid,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))

    def history(self, minutes: float = 60.0,
                max_rows: int = 20000) -> List[Dict[str, Any]]:
        """All device positions within the last `minutes`, oldest first —
        the source for track playback."""
        since = time.time() - minutes * 60
        with self._lock:
            cur = self._db.execute(
                "SELECT uid, ts, lat, lon FROM positions WHERE ts >= ? "
                "ORDER BY ts ASC LIMIT ?", (since, max_rows))
            return [{"uid": r[0], "ts": r[1], "lat": r[2], "lon": r[3]}
                    for r in cur.fetchall()]

    def track(self, uid: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, lat, lon, hae FROM positions WHERE uid = ? "
                "ORDER BY ts DESC LIMIT ?",
                (uid, limit),
            )
            return [
                {"ts": r[0], "lat": r[1], "lon": r[2], "hae": r[3]}
                for r in reversed(cur.fetchall())
            ]

    def device_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]

    def delete_device(self, uid: str) -> bool:
        """Remove a device and everything tied to it: its breadcrumb history,
        any active/cleared alerts, and its device-attached video streams.
        Returns True if a device row existed. A removed device only reappears
        if it actively sends CoT again."""
        with self._lock:
            cur = self._db.execute("DELETE FROM devices WHERE uid = ?", (uid,))
            self._db.execute("DELETE FROM positions WHERE uid = ?", (uid,))
            self._db.execute("DELETE FROM alerts WHERE uid = ?", (uid,))
            self._db.execute("DELETE FROM streams WHERE device_uid = ?", (uid,))
            self._db.commit()
            return cur.rowcount > 0

    def delete_all_devices(self) -> int:
        """Clear every device plus all position history, alerts, and
        device-attached streams. Standalone streams (no device_uid) are left
        alone. Returns the number of devices removed."""
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            self._db.execute("DELETE FROM devices")
            self._db.execute("DELETE FROM positions")
            self._db.execute("DELETE FROM alerts")
            self._db.execute("DELETE FROM streams WHERE device_uid IS NOT NULL")
            self._db.commit()
            return n

    def prune_positions(self, max_age_days: float) -> int:
        """Delete position rows older than max_age_days. Returns rows removed.
        Runs periodically so the breadcrumb table can't grow without bound."""
        if max_age_days <= 0:
            return 0
        cutoff = time.time() - max_age_days * 86400
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM positions WHERE ts < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    # -- streams -------------------------------------------------------------

    def upsert_stream(self, row: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO streams (path, device_uid, kind, source, name,
                                     created, updated)
                VALUES (:path, :device_uid, :kind, :source, :name, :now, :now)
                ON CONFLICT(path) DO UPDATE SET
                    device_uid = COALESCE(excluded.device_uid, streams.device_uid),
                    kind       = excluded.kind,
                    source     = COALESCE(excluded.source, streams.source),
                    name       = COALESCE(excluded.name, streams.name),
                    updated    = excluded.updated
                """,
                {**row, "now": now},
            )
            self._db.commit()

    def streams(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM streams ORDER BY path")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def streams_for_device(self, uid: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM streams WHERE device_uid = ? ORDER BY kind", (uid,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def delete_stream(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM streams WHERE path = ?", (path,))
            self._db.commit()

    # -- chat ------------------------------------------------------------------

    def add_chat(self, sender: Optional[str], sender_uid: Optional[str],
                 room: Optional[str], message: str) -> Dict[str, Any]:
        ts = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO chat (ts, sender, sender_uid, room, message) "
                "VALUES (?,?,?,?,?)", (ts, sender, sender_uid, room, message))
            self._db.commit()
            rid = cur.lastrowid
        return {"id": rid, "ts": ts, "sender": sender, "sender_uid": sender_uid,
                "room": room, "message": message}

    def chat_history(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT id, ts, sender, sender_uid, room, message FROM chat "
                "ORDER BY ts DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in reversed(cur.fetchall())]

    def delete_chat(self, msg_id: int) -> bool:
        """Remove one chat message. Returns True if a row existed."""
        with self._lock:
            cur = self._db.execute("DELETE FROM chat WHERE id = ?", (msg_id,))
            self._db.commit()
            return cur.rowcount > 0

    def clear_chat(self) -> int:
        """Remove every chat message. Returns how many were removed. Chat has
        no retention sweep (unlike positions), so this is the only way history
        ever shrinks."""
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
            self._db.execute("DELETE FROM chat")
            self._db.commit()
            return n

    # -- alerts ----------------------------------------------------------------

    def raise_alert(self, uid: str, callsign: Optional[str],
                    alert_type: Optional[str], lat: Optional[float],
                    lon: Optional[float]) -> Dict[str, Any]:
        ts = time.time()
        with self._lock:
            # don't duplicate an already-active alert for the same uid
            cur = self._db.execute(
                "SELECT id FROM alerts WHERE uid = ? AND cleared IS NULL", (uid,))
            existing = cur.fetchone()
            if existing:
                return {"id": existing[0], "uid": uid, "callsign": callsign,
                        "alert_type": alert_type, "lat": lat, "lon": lon,
                        "raised": ts, "cleared": None}
            cur = self._db.execute(
                "INSERT INTO alerts (uid, callsign, alert_type, lat, lon, raised)"
                " VALUES (?,?,?,?,?,?)", (uid, callsign, alert_type, lat, lon, ts))
            self._db.commit()
            rid = cur.lastrowid
        return {"id": rid, "uid": uid, "callsign": callsign,
                "alert_type": alert_type, "lat": lat, "lon": lon,
                "raised": ts, "cleared": None}

    def clear_alert(self, uid: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE alerts SET cleared = ? WHERE uid = ? AND cleared IS NULL",
                (time.time(), uid))
            self._db.commit()

    def active_alerts(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT id, uid, callsign, alert_type, lat, lon, raised "
                "FROM alerts WHERE cleared IS NULL ORDER BY raised DESC")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # -- enrollment users ------------------------------------------------------

    def create_enroll_user(self, username: str, token_hash: str,
                           callsign: Optional[str], expires: Optional[float],
                           max_uses: Optional[int]) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO enroll_users
                    (username, token_hash, callsign, expires, max_uses,
                     uses, created)
                VALUES (?,?,?,?,?,0,?)
                ON CONFLICT(username) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    callsign   = excluded.callsign,
                    expires    = excluded.expires,
                    max_uses   = excluded.max_uses,
                    uses       = 0,
                    created    = excluded.created
                """,
                (username, token_hash, callsign, expires, max_uses, time.time()),
            )
            self._db.commit()

    def get_enroll_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM enroll_users WHERE username = ?", (username,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))

    def consume_enrollment(self, username: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE enroll_users SET uses = uses + 1, last_enroll = ? "
                "WHERE username = ?", (time.time(), username))
            self._db.commit()

    def enroll_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT username, callsign, expires, max_uses, uses, created, "
                "last_enroll FROM enroll_users ORDER BY created DESC")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def delete_enroll_user(self, username: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM enroll_users WHERE username = ?",
                             (username,))
            self._db.commit()

    # -- users -----------------------------------------------------------------

    def upsert_user(self, username: str, pw_hash: str, role: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO users (username, pw_hash, role, created) "
                "VALUES (?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                "pw_hash = excluded.pw_hash, role = excluded.role",
                (username, pw_hash, role, time.time()))
            self._db.commit()

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT username, pw_hash, role FROM users WHERE username = ?",
                (username,))
            row = cur.fetchone()
            if row is None:
                return None
            return {"username": row[0], "pw_hash": row[1], "role": row[2]}

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT username, role, created FROM users ORDER BY username")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_role(self, username: str, role: str) -> bool:
        """Change only a user's role (no password reset). True if a row changed."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE users SET role = ? WHERE username = ?", (role, username))
            self._db.commit()
            return cur.rowcount > 0

    def delete_user(self, username: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM users WHERE username = ?", (username,))
            self._db.commit()

    def user_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def admin_count(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()
