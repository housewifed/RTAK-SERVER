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

// ------------------------------------------------- incremental publishing
//
// Regression: renderPlaybackAt pushed the whole trail and every fix to the map
// on every animation frame. Past ~1,000 fixes the map worker fell behind: the
// dot drew minutes behind its own trail, and the backlog crashed the tab.

const P = { minInterval: 250, maxStale: 2000 };
const base = { now: 10000, lastPublishAt: 0, idle: true,
               changed: false, rewound: false, dirty: false };

test("nothing to publish when nothing changed", () => {
  assert.equal(core.shouldPublish({ ...base }, P), false);
});

test("forward progress publishes once the worker is idle and the interval passed", () => {
  assert.equal(core.shouldPublish({ ...base, changed: true, lastPublishAt: 9700 }, P), true);
});

test("forward progress waits out the minimum interval", () => {
  assert.equal(core.shouldPublish({ ...base, changed: true, lastPublishAt: 9900 }, P), false);
});

test("forward progress never queues behind a busy worker", () => {
  // this is the backlog that crashed the tab
  assert.equal(core.shouldPublish({ ...base, changed: true, idle: false, lastPublishAt: 9000 }, P), false);
});

test("a busy worker cannot starve the trail forever", () => {
  assert.equal(core.shouldPublish({ ...base, changed: true, idle: false, lastPublishAt: 7500 }, P), true);
});

test("a rewind waits for a busy worker like everything else", () => {
  // Code review: sending rewinds on a timer while the worker was busy let a
  // slider drag on a long track rebuild the backlog. The stale trail is hidden
  // instead until the redraw can go out.
  assert.equal(core.shouldPublish({ ...base, changed: true, rewound: true, idle: false, lastPublishAt: 9850 }, P), false);
});

test("a rewind goes out as soon as the worker is idle, without waiting out the interval", () => {
  assert.equal(core.shouldPublish({ ...base, changed: true, rewound: true, idle: true, lastPublishAt: 9990 }, P), true);
});

test("a rewind stuck behind a busy worker still goes out after maxStale", () => {
  assert.equal(core.shouldPublish({ ...base, changed: true, rewound: true, idle: false, lastPublishAt: 7500 }, P), true);
});

test("trailIsAhead is true while any drawn trail extends past its device's playhead", () => {
  assert.equal(core.trailIsAhead(new Map([["a", 50]]), new Map([["a", 40]])), true);
  assert.equal(core.trailIsAhead(new Map([["a", 40]]), new Map([["a", 50]])), false);
  assert.equal(core.trailIsAhead(new Map([["a", 40]]), new Map([["a", 40]])), false);
  assert.equal(core.trailIsAhead(new Map([["a", 50]]), new Map()), false);
});

test("an explicit change (show/hide) skips the interval but not the idle check", () => {
  assert.equal(core.shouldPublish({ ...base, dirty: true, lastPublishAt: 9990 }, P), true);
  assert.equal(core.shouldPublish({ ...base, dirty: true, idle: false, lastPublishAt: 9990 }, P), false);
});

const walk = Array.from({ length: 20 }, (_, k) => ({ ts: 100 + k, lat: k, lon: k * 2 }));

test("the head runs from the last drawn fix through newer fixes to the dot", () => {
  const head = core.headCoordinates(walk, 10, 13, { lon: 27, lat: 13.5 });
  assert.deepEqual(head, [[20, 10], [22, 11], [24, 12], [26, 13], [27, 13.5]]);
});

test("the head is just the last drawn fix and the dot between two fixes", () => {
  assert.deepEqual(core.headCoordinates(walk, 10, 10, { lon: 20.5, lat: 10.25 }),
                   [[20, 10], [20.5, 10.25]]);
});

test("after a rewind the head is empty until the trail is redrawn", () => {
  assert.deepEqual(core.headCoordinates(walk, 15, 12, { lon: 24.5, lat: 12.25 }), []);
});

test("a device not drawn yet gets its whole path so far as the head", () => {
  assert.deepEqual(core.headCoordinates(walk, -1, 2, { lon: 5, lat: 2.5 }),
                   [[0, 0], [2, 1], [4, 2], [5, 2.5]]);
});

test("indexChanges spots forward progress, rewinds and new devices", () => {
  const published = new Map([["a", 5], ["b", 9]]);
  assert.deepEqual(core.indexChanges(published, new Map([["a", 5], ["b", 9]])),
                   { changed: false, rewound: false });
  assert.deepEqual(core.indexChanges(published, new Map([["a", 6], ["b", 9]])),
                   { changed: true, rewound: false });
  assert.deepEqual(core.indexChanges(published, new Map([["a", 5], ["b", 4]])),
                   { changed: true, rewound: true });
  assert.deepEqual(core.indexChanges(published, new Map([["a", 5], ["b", 9], ["c", 0]])),
                   { changed: true, rewound: false });
});

test("a device that disappears from the visible set counts as a change", () => {
  assert.deepEqual(core.indexChanges(new Map([["a", 5], ["b", 9]]), new Map([["a", 5]])),
                   { changed: true, rewound: false });
});

// A device that is shown but has not reached its first fix at the playhead is
// represented as index -1, not left out. Code review: left out, a scrub back to
// before a late-starting device's first fix neither hid its old trail nor
// counted as a rewind, so the trail lingered with no dot for up to 250ms.

test("a device rewound to before its first fix makes its drawn trail count as ahead", () => {
  assert.equal(core.trailIsAhead(new Map([["late", 400]]), new Map([["late", -1]])), true);
});

test("a device rewound to before its first fix counts as a rewind", () => {
  assert.deepEqual(core.indexChanges(new Map([["late", 400]]), new Map([["late", -1]])),
                   { changed: true, rewound: true });
});

test("a device that has not started and was never drawn is neither ahead nor a rewind", () => {
  assert.equal(core.trailIsAhead(new Map([["late", -1]]), new Map([["late", -1]])), false);
  assert.deepEqual(core.indexChanges(new Map([["late", -1]]), new Map([["late", -1]])),
                   { changed: false, rewound: false });
});
