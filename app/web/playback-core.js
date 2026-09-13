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

if (typeof module !== "undefined") {
  module.exports = { PB_PALETTE, PB_OVERFLOW, assignPlaybackColors,
                     lastFixIndexAt, positionAt, boundsOf, needsReframe };
}
