# Track recording and playback

## The problem

Playback today is a single button that loads the last 120 minutes for every
device and scrubs through it. You cannot record a specific run, you cannot see
what was recorded, and you cannot choose who or when to replay. Track recording
is also entangled in people's minds with *video* recording, which is a separate
MediaMTX feature — a device's breadcrumbs are stored whether or not it has a
camera.

## Design

### A session is a bookmark, not a copy

Positions for every device are already written to the `positions` table on
arrival and pruned after `POSITION_RETENTION_DAYS`. A "recording" therefore does
not need to copy anything: it records **which devices** and **which window**.
Playback reads the positions back through that window.

This keeps one source of truth, makes starting a recording instant, and means a
recording that is running when the server restarts survives.

```
track_sessions
  id          INTEGER PRIMARY KEY
  name        TEXT           -- optional label, e.g. "north patrol"
  devices     TEXT NOT NULL  -- JSON array of uids; [] means "all devices"
  started     REAL NOT NULL
  ended       REAL           -- NULL while still recording
  created_by  TEXT
```

`devices = []` is a deliberate "all devices, including ones that appear later",
which is what "select all" means when a new device may join mid-recording.

### Retention must not eat a saved track

`prune_positions` deletes rows older than the retention window. Left alone it
would silently empty a saved recording, so the prune now skips any row covered
by a session for that device. A recording you kept is a statement that those
points matter.

### API

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/tracks` | viewer | list sessions, newest first, with duration and point counts |
| POST | `/api/tracks` | operator | start recording — `{name?, devices: [uid...], all?: bool}` |
| POST | `/api/tracks/stop` | operator | stop — `{id}` |
| DELETE | `/api/tracks?id=` | operator | delete the bookmark (never the positions) |
| GET | `/api/history?from=&to=&uids=` | viewer | positions for an arbitrary window and device set |

`/api/history?minutes=` keeps working, so nothing that exists breaks.

### UI

**Record panel** (new `⏺ Record` button in the header): the device list with a
checkbox each and a *Select all* master, an optional name, and Start. While a
recording runs it shows in the panel with a live elapsed timer and a Stop
button. Recording is explicitly independent of video — the panel says so.

**Playback picker** (the existing `⏱ Playback` button now opens this first):

- *Recorded tracks* — a table of sessions: Name, Devices, Date, Start, Duration,
  Points. Clicking a column header sorts by it, including by device, which is
  what makes a long list usable. Play replays exactly that session's window and
  devices.
- *Custom range* — from/to datetime inputs plus the same device checklist, for
  replaying something nobody thought to record.

The existing playback bar (scrubber, play/pause, close) is unchanged; its label
now names what is being replayed.

## Testing

- Store: session lifecycle, duration, `devices=[]` semantics, and that prune
  keeps points inside a saved session while still deleting everything else.
- API: role enforcement and the argument validation on each endpoint, driven
  against a live takcore.
- `full_system_check.py`: a section that records a session, drives positions
  into it, stops it, replays the window, and deletes it.
- Browser: record → list → sort → play, verified against a moving device.

## Out of scope

Exporting a track (GPX/KML), sharing a recording between users, and per-track
retention overrides. None are needed to make playback usable, and each is a
feature in its own right.
