"""Cursor-on-Target (CoT) XML parsing and building.

Handles the legacy CoT XML wire format used by ATAK/iTAK on streaming
connections. TAK Protocol v1 (protobuf) is a later milestone; because this
server never advertises protobuf support (no t-x-takp-v announcement),
clients remain in XML mode, which is fully supported by ATAK, iTAK and
WinTAK.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

COT_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

PING_TYPE = "t-x-c-t"
PONG_TYPE = "t-x-c-t-r"


def cot_time(dt: Optional[datetime] = None) -> str:
    """Format a datetime the way CoT expects (UTC, millisecond precision)."""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_cot_time(value: str) -> Optional[datetime]:
    """Parse a CoT timestamp; tolerant of missing fractional seconds."""
    if not value:
        return None
    value = value.strip()
    for fmt in (COT_TIME_FMT, "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class CotEvent:
    """A parsed CoT event with the fields the server cares about.

    ``raw`` always carries the original bytes so re-broadcast is lossless
    even for detail elements this parser does not model.
    """

    uid: str
    type: str
    how: str = ""
    time: Optional[datetime] = None
    start: Optional[datetime] = None
    stale: Optional[datetime] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    hae: Optional[float] = None
    ce: Optional[float] = None
    le: Optional[float] = None
    callsign: Optional[str] = None
    group_name: Optional[str] = None
    group_role: Optional[str] = None
    course: Optional[float] = None
    speed: Optional[float] = None
    battery: Optional[int] = None
    device: Optional[str] = None
    platform: Optional[str] = None
    video_url: Optional[str] = None
    # GeoChat
    chat_message: Optional[str] = None
    chat_sender: Optional[str] = None
    chat_sender_uid: Optional[str] = None
    chat_room: Optional[str] = None
    # Emergency
    emergency_type: Optional[str] = None   # e.g. "911 Alert", "Ring The Bell"
    emergency_cancel: bool = False
    raw: bytes = b""

    @property
    def is_ping(self) -> bool:
        return self.type == PING_TYPE

    @property
    def is_chat(self) -> bool:
        return self.type.startswith("b-t-f") and self.chat_message is not None

    @property
    def is_emergency(self) -> bool:
        return self.type.startswith("b-a-o-")

    @property
    def is_position(self) -> bool:
        """True for atom (map-drawable) events that carry a real position."""
        return (
            self.type.startswith("a-")
            and self.lat is not None
            and self.lon is not None
            and not (self.lat == 0 and self.lon == 0)
        )


def _f(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # CoT uses large sentinel values for "unknown"
    if f >= 9999999.0:
        return None
    return f


def parse_event(data: bytes) -> Optional[CotEvent]:
    """Parse one CoT <event> document. Returns None if unparseable."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.tag != "event":
        return None

    uid = root.get("uid", "")
    ev_type = root.get("type", "")
    if not uid or not ev_type:
        return None

    ev = CotEvent(
        uid=uid,
        type=ev_type,
        how=root.get("how", ""),
        time=parse_cot_time(root.get("time", "")),
        start=parse_cot_time(root.get("start", "")),
        stale=parse_cot_time(root.get("stale", "")),
        raw=data,
    )

    point = root.find("point")
    if point is not None:
        ev.lat = _f(point.get("lat"))
        ev.lon = _f(point.get("lon"))
        ev.hae = _f(point.get("hae"))
        ev.ce = _f(point.get("ce"))
        ev.le = _f(point.get("le"))

    detail = root.find("detail")
    if detail is not None:
        contact = detail.find("contact")
        if contact is not None:
            ev.callsign = contact.get("callsign")
        group = detail.find("__group")
        if group is not None:
            ev.group_name = group.get("name")
            ev.group_role = group.get("role")
        track = detail.find("track")
        if track is not None:
            ev.course = _f(track.get("course"))
            ev.speed = _f(track.get("speed"))
        status = detail.find("status")
        if status is not None:
            try:
                ev.battery = int(status.get("battery", ""))
            except ValueError:
                ev.battery = None
        takv = detail.find("takv")
        if takv is not None:
            ev.device = takv.get("device")
            ev.platform = takv.get("platform")
        video = detail.find("__video")
        if video is not None:
            ev.video_url = video.get("url")
            conn = video.find("ConnectionEntry")
            if conn is not None and not ev.video_url:
                proto = conn.get("protocol", "rtsp")
                addr = conn.get("address", "")
                port = conn.get("port", "")
                path = conn.get("path", "")
                if addr:
                    if path and not path.startswith("/"):
                        path = "/" + path
                    ev.video_url = f"{proto}://{addr}:{port}{path}"

        # GeoChat: <__chat ...><chatgrp/></__chat> + <remarks>message</remarks>
        chat = detail.find("__chat")
        if chat is not None:
            ev.chat_room = chat.get("chatroom") or chat.get("id")
            ev.chat_sender = chat.get("senderCallsign")
            chatgrp = chat.find("chatgrp")
            if chatgrp is not None:
                # uid0 is usually the sender in direct chats
                ev.chat_sender_uid = chatgrp.get("uid0")
            remarks = detail.find("remarks")
            if remarks is not None and remarks.text:
                ev.chat_message = remarks.text.strip()
                if not ev.chat_sender:
                    ev.chat_sender = remarks.get("source")

        # Emergency beacon: <emergency type="911 Alert">callsign</emergency>
        emergency = detail.find("emergency")
        if emergency is not None:
            cancel = (emergency.get("cancel") or "").lower() == "true"
            ev.emergency_cancel = cancel
            ev.emergency_type = emergency.get("type") or (
                emergency.text.strip() if emergency.text else "Emergency")
            if not ev.callsign and emergency.text:
                ev.callsign = emergency.text.strip()

    return ev


def build_pong(ping_uid: str = "takcore-pong") -> bytes:
    """Response to a client ping (t-x-c-t)."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(seconds=10)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{ping_uid}" type="{PONG_TYPE}" how="h-g-i-g-o" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" stale="{cot_time(stale)}">'
        f'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999" le="9999999"/>'
        f"<detail/></event>"
    ).encode()


def build_emergency_cancel(uid: str, callsign: str = "") -> bytes:
    """Build the CoT that cancels an emergency, so devices stop alerting too.

    ATAK sends exactly this when a user cancels their own 911: type b-a-o-can
    with cancel="true" on the emergency element. The web UI's "clear" button
    broadcasts it under the alert's own uid, which is how the alert is keyed
    everywhere (ATAK's 911 event carries its own uid, e.g. <device>-9-1-1).
    """
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=1)
    cs = _xml_escape(callsign or uid)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{_xml_escape(uid)}" type="b-a-o-can" '
        f'how="h-g-i-g-o" time="{cot_time(now)}" start="{cot_time(now)}" '
        f'stale="{cot_time(stale)}">'
        f'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999" le="9999999"/>'
        f'<detail><emergency cancel="true">{cs}</emergency>'
        f'<contact callsign="{cs}"/></detail></event>'
    ).encode()


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_geochat(message: str, sender_callsign: str = "TAKCORE",
                  sender_uid: str = "takcore-server",
                  room: str = "All Chat Rooms",
                  room_uid: str = "All Chat Rooms") -> bytes:
    """Build a GeoChat CoT for broadcast from the web UI to all devices."""
    import uuid
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=5)
    msg_id = uuid.uuid4().hex[:20]
    uid = f"GeoChat.{sender_uid}.{room_uid}.{msg_id}"
    m = _xml_escape(message)
    cs = _xml_escape(sender_callsign)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{_xml_escape(uid)}" type="b-t-f" how="h-g-i-g-o" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" stale="{cot_time(stale)}">'
        f'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999" le="9999999"/>'
        f"<detail>"
        f'<__chat parent="RootContactGroup" groupOwner="false" '
        f'chatroom="{_xml_escape(room)}" id="{_xml_escape(room_uid)}" '
        f'senderCallsign="{cs}">'
        f'<chatgrp uid0="{_xml_escape(sender_uid)}" uid1="{_xml_escape(room_uid)}" '
        f'id="{_xml_escape(room_uid)}"/></__chat>'
        f'<link uid="{_xml_escape(sender_uid)}" type="a-f-G" relation="p-p"/>'
        f'<remarks source="BAO.F.ATAK.{_xml_escape(sender_uid)}" '
        f'time="{cot_time(now)}">{m}</remarks>'
        f'<__serverdestination destinations=""/>'
        f"</detail></event>"
    ).encode()


class CotStreamParser:
    """Splits a TCP byte stream into complete CoT <event> documents.

    TAK clients write concatenated XML documents (optionally preceded by an
    XML declaration) onto the socket with no framing. This parser buffers
    bytes and yields each complete ``<event ...>...</event>`` or
    self-closing ``<event ... />`` as raw bytes. Non-event content (XML
    declarations, <auth> blobs) is skipped.
    """

    MAX_BUFFER = 2 * 1024 * 1024  # drop runaway buffers (malformed peer)

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> List[bytes]:
        self._buf += data
        if len(self._buf) > self.MAX_BUFFER:
            self._buf = b""
            return []
        events: List[bytes] = []
        while True:
            start = self._buf.find(b"<event")
            if start == -1:
                # keep a small tail in case '<even' straddles a chunk edge
                if len(self._buf) > 64:
                    self._buf = self._buf[-64:]
                break
            gt = self._buf.find(b">", start)
            if gt == -1:
                self._buf = self._buf[start:]
                break
            if self._buf[gt - 1 : gt] == b"/":
                # self-closing <event ... />
                events.append(self._buf[start : gt + 1])
                self._buf = self._buf[gt + 1 :]
                continue
            end = self._buf.find(b"</event>", gt)
            if end == -1:
                self._buf = self._buf[start:]
                break
            events.append(self._buf[start : end + len(b"</event>")])
            self._buf = self._buf[end + len(b"</event>") :]
        return events
