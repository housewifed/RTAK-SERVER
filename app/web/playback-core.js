/* Playback logic with no DOM and no map: colour assignment, interpolation
 * between reported fixes, and when "Frame all" has to move the camera.
 *
 * Loaded as a plain script before app.js (its functions become globals there),
 * and required by the Node tests in tests/web/.
 */
"use strict";

// Trail colours, in fixed order. Validated all-pairs against the dark map
// surface (#10151c): the first three - blue, orange, aqua - pass every check,
// including separation for red-green colour-blind viewers. Past three no order
// can keep every pair apart, so devices 4-8 rely on their callsign label as well
// as colour. Hues are never invented or cycled: a ninth device is neutral.
const PB_PALETTE = [
  "#3987e5", // blue
  "#d95926", // orange
  "#199e70", // aqua
  "#c98500", // yellow
  "#d55181", // magenta
  "#008300", // green
  "#9085e9", // violet
  "#e66767", // red
];
const PB_OVERFLOW = "#8b98a5";

/** uid -> colour, assigned once per playback in callsign order.
 *
 * Assigned from EVERY device in the playback, never from the visible ones:
 * colour follows the device, so hiding one must not repaint the others. */
function assignPlaybackColors(entries) {
  const sorted = [...entries].sort((a, b) => {
    const byName = String(a.callsign || a.uid).localeCompare(String(b.callsign || b.uid));
    return byName !== 0 ? byName : String(a.uid).localeCompare(String(b.uid));
  });
  const out = new Map();
  sorted.forEach((e, i) => out.set(e.uid, i < PB_PALETTE.length ? PB_PALETTE[i] : PB_OVERFLOW));
  return out;
}

/** Index of the last fix at or before t, or -1 when t precedes the track. */
function lastFixIndexAt(pts, t) {
  let lo = 0, hi = pts.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].ts <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  return best;
}

/** Where a device was at t. Between two fixes this interpolates in a straight
 * line, which is honest about what we know: nothing was recorded in between. */
function positionAt(pts, t) {
  const i = lastFixIndexAt(pts, t);
  if (i < 0) return null;                       // not reporting yet
  const a = pts[i], b = pts[i + 1];
  if (!b) return { lat: a.lat, lon: a.lon, exact: true };
  const span = b.ts - a.ts;
  if (span <= 0) return { lat: a.lat, lon: a.lon, exact: true };
  const f = Math.min(1, Math.max(0, (t - a.ts) / span));
  return { lat: a.lat + (b.lat - a.lat) * f,
           lon: a.lon + (b.lon - a.lon) * f,
           exact: f === 0 };
}

/** {west, south, east, north} around [lon, lat] points, or null for none. */
function boundsOf(points) {
  if (!points.length) return null;
  let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;
  for (const [lon, lat] of points) {
    if (lon < west) west = lon;
    if (lon > east) east = lon;
    if (lat < south) south = lat;
    if (lat > north) north = lat;
  }
  return { west, south, east, north };
}

/** Should "Frame all" move the camera? Only once a device drifts into the outer
 * `margin` of the view (or out of it). Re-fitting on every frame instead would
 * make the zoom breathe constantly as devices shuffle about. */
function needsReframe(points, view, margin = 0.15) {
  if (!points.length) return false;
  const w = view.east - view.west, h = view.north - view.south;
  const inner = { west: view.west + w * margin, east: view.east - w * margin,
                  south: view.south + h * margin, north: view.north - h * margin };
  return points.some(([lon, lat]) =>
    lon < inner.west || lon > inner.east || lat < inner.south || lat > inner.north);
}

// ------------------------------------------------- incremental publishing
//
// Pushing the whole trail and every fix to the map on every animation frame made
// each update bigger than the last. Past ~1,000 fixes the map worker could not
// finish one before the next arrived: the dot drew minutes behind its own trail
// and the backlog crashed the tab. So the long trail and the fix dots are
// re-sent only when they change, and never queued behind a busy worker; the dot
// and a short head line to it are what move every frame.

/** Did the drawn fix index move for any device, and did any go backwards? A
 * device appearing or disappearing is a change too. */
function indexChanges(published, current) {
  let changed = published.size !== current.size, rewound = false;
  for (const [uid, i] of current) {
    if (!published.has(uid)) { changed = true; continue; }
    const p = published.get(uid);
    if (i !== p) changed = true;
    if (i < p) rewound = true;
  }
  return { changed, rewound };
}

/** Should the long trail and the fix dots be re-sent to the map this frame?
 *
 * The one rule that matters: nothing is sent while the worker is still busy
 * with the previous update, so updates cannot pile up. On top of that:
 *  - never while nothing changed;
 *  - maxStale is the escape hatch, at most one send per maxStale, so a map that
 *    stays busy (tiles loading while following) cannot freeze the trail;
 *  - a rewind or an explicit change (show/hide) goes out the moment the worker
 *    is idle; ordinary forward progress also waits out minInterval.
 * A rewind used to skip the idle check on a timer, which let a slider drag on a
 * long track rebuild the backlog. The stale trail is hidden instead (see
 * trailIsAhead) until the redraw can go out.
 */
function shouldPublish(s, opts) {
  const since = s.now - s.lastPublishAt;
  if (!s.changed && !s.dirty) return false;
  if (since >= opts.maxStale) return true;
  if (!s.idle) return false;
  return s.rewound || s.dirty || since >= opts.minInterval;
}

/** Is the trail on the map drawn past where some device's playhead now is?
 * True just after a rewind, until the shorter trail has been drawn. The renderer
 * hides the trail meanwhile rather than show it running ahead of the dot. */
function trailIsAhead(drawn, current) {
  for (const [uid, i] of current) {
    if (drawn.has(uid) && drawn.get(uid) > i) return true;
  }
  return false;
}

/** The short line from the last fix already drawn on the map, through any
 * newer fixes, to the dot. Empty after a rewind: the drawn trail is ahead of
 * the dot then and must be redrawn, not extended. publishedIdx -1 means this
 * device has not been drawn yet, so its whole path so far is the head. */
function headCoordinates(pts, publishedIdx, i, at) {
  if (i < publishedIdx) return [];
  const out = [];
  for (let k = Math.max(0, publishedIdx); k <= i; k++) out.push([pts[k].lon, pts[k].lat]);
  out.push([at.lon, at.lat]);
  return out;
}

if (typeof module !== "undefined") {
  module.exports = { PB_PALETTE, PB_OVERFLOW, assignPlaybackColors,
                     lastFixIndexAt, positionAt, boundsOf, needsReframe,
                     indexChanges, shouldPublish, headCoordinates, trailIsAhead };
}
