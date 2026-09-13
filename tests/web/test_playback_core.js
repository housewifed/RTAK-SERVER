// Pure playback logic: colour assignment, interpolation, and when "Frame all"
// has to move the camera. Run with:  node --test tests/web/
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../../app/web/playback-core.js");

// -------------------------------------------------------------- colours

test("devices get the validated palette in callsign order", () => {
  const colors = core.assignPlaybackColors([
    { uid: "u-c", callsign: "Charlie" },
    { uid: "u-a", callsign: "Alpha" },
    { uid: "u-b", callsign: "Bravo" },
  ]);
  assert.equal(colors.get("u-a"), core.PB_PALETTE[0]);
  assert.equal(colors.get("u-b"), core.PB_PALETTE[1]);
  assert.equal(colors.get("u-c"), core.PB_PALETTE[2]);
});

test("the first three slots are the ones that passed the all-pairs check", () => {
  // blue, orange, aqua: the only prefix of the palette that is distinguishable
  // pairwise for colour-blind viewers on the dark map. Guard the order.
  assert.deepEqual(core.PB_PALETTE.slice(0, 3), ["#3987e5", "#d95926", "#199e70"]);
});

test("assignment is deterministic whatever order devices arrive in", () => {
  const a = core.assignPlaybackColors([{ uid: "1", callsign: "B" }, { uid: "2", callsign: "A" }]);
  const b = core.assignPlaybackColors([{ uid: "2", callsign: "A" }, { uid: "1", callsign: "B" }]);
  assert.deepEqual([...a.entries()].sort(), [...b.entries()].sort());
});

test("a device without a callsign sorts by its uid", () => {
  const c = core.assignPlaybackColors([{ uid: "zz" }, { uid: "aa" }]);
  assert.equal(c.get("aa"), core.PB_PALETTE[0]);
});

test("identical callsigns are tie-broken by uid, so colours never swap", () => {
  const c = core.assignPlaybackColors([
    { uid: "uid-9", callsign: "Same" }, { uid: "uid-1", callsign: "Same" }]);
  assert.equal(c.get("uid-1"), core.PB_PALETTE[0]);
  assert.equal(c.get("uid-9"), core.PB_PALETTE[1]);
});

test("a ninth device gets the neutral colour instead of an invented hue", () => {
  const entries = Array.from({ length: 10 }, (_, i) =>
    ({ uid: `u${i}`, callsign: `D${String(i).padStart(2, "0")}` }));
  const c = core.assignPlaybackColors(entries);
  assert.equal(new Set(entries.slice(0, 8).map((e) => c.get(e.uid))).size, 8);
  assert.equal(c.get("u8"), core.PB_OVERFLOW);
  assert.equal(c.get("u9"), core.PB_OVERFLOW);
});

// -------------------------------------------------------- interpolation

const track = [
  { ts: 100, lat: 0, lon: 0 },
  { ts: 110, lat: 10, lon: 20 },
  { ts: 130, lat: 10, lon: 40 },
];

test("lastFixIndexAt finds the last fix at or before t", () => {
  assert.equal(core.lastFixIndexAt(track, 99), -1);
  assert.equal(core.lastFixIndexAt(track, 100), 0);
  assert.equal(core.lastFixIndexAt(track, 109.9), 0);
  assert.equal(core.lastFixIndexAt(track, 110), 1);
  assert.equal(core.lastFixIndexAt(track, 500), 2);
});

test("positionAt interpolates linearly between the fixes around t", () => {
  const p = core.positionAt(track, 105);
  assert.equal(p.lat, 5);
  assert.equal(p.lon, 10);
  assert.equal(p.exact, false);
});

test("positionAt is exact on a fix and holds the last fix after the track", () => {
  assert.deepEqual(core.positionAt(track, 110), { lat: 10, lon: 20, exact: true });
  assert.deepEqual(core.positionAt(track, 999), { lat: 10, lon: 40, exact: true });
});

test("positionAt is null before a device starts reporting", () => {
  assert.equal(core.positionAt(track, 50), null);
});

test("two fixes with the same timestamp do not divide by zero", () => {
  const dup = [{ ts: 1, lat: 1, lon: 1 }, { ts: 1, lat: 2, lon: 2 }];
  const p = core.positionAt(dup, 1);
  assert.ok(Number.isFinite(p.lat) && Number.isFinite(p.lon));
});

// ------------------------------------------------------------ framing

const view = { west: 0, south: 0, east: 100, north: 100 };

test("no reframe while every device sits comfortably inside the view", () => {
  assert.equal(core.needsReframe([[50, 50], [40, 60]], view, 0.15), false);
});

test("reframe once a device drifts into the outer margin", () => {
  // the inner box is 15..85 on both axes
  assert.equal(core.needsReframe([[50, 50], [90, 50]], view, 0.15), true);
  assert.equal(core.needsReframe([[50, 10]], view, 0.15), true);
});

test("reframe when a device has left the view entirely", () => {
  assert.equal(core.needsReframe([[150, 50]], view, 0.15), true);
});

test("nothing to frame means no camera move", () => {
  assert.equal(core.needsReframe([], view, 0.15), false);
});

test("boundsOf wraps the points, and is null for none", () => {
  assert.deepEqual(core.boundsOf([[1, 5], [3, 2], [-4, 9]]),
    { west: -4, south: 2, east: 3, north: 9 });
  assert.equal(core.boundsOf([]), null);
});
