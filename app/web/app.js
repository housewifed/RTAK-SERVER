/* TAK Revamp — live map (Phase 2: live video popups)
 *
 * Positions arrive over SSE (/api/stream). Clicking a marker opens a popup;
 * if the device has a registered stream, a WebRTC (WHEP) player starts
 * inside the popup, expandable to fullscreen. Streams are served by
 * MediaMTX (port 8889) and registered/authorized through takcore.
 */

"use strict";

const STALE_MS = 90_000;
const WEBRTC_PORT = 8889; // MediaMTX WHEP port for DIRECT (unproxied) access

const TEAM_COLORS = {
  White: "#ffffff", Yellow: "#f1c40f", Orange: "#e67e22", Magenta: "#d63de0",
  Red: "#e74c3c", Maroon: "#8e2f2f", Purple: "#8e44ad", "Dark Blue": "#2c3e9e",
  Blue: "#3498db", Cyan: "#1abc9c", Teal: "#16a085", Green: "#2ecc71",
  "Dark Green": "#1e6f43", Brown: "#795548",
};
const DEFAULT_COLOR = "#3fb6ff";
const CAMERA_COLOR = "#f39c12";

const devices = new Map();          // uid -> state
const streamsByDevice = new Map();  // uid -> [stream rows]
let popupUid = null;
// Monotonic token for the in-flight openPopup call. The close handler below
// nils popupUid (fired by closeOnClick when a second marker is clicked),
// which used to race openPopup's async guard and leave the popup stuck on
// "checking for live video…". The generation counter is immune to close —
// only a newer openPopup supersedes an in-flight one.
let popupGen = 0;
let player = null;                  // { pc, video, path }
const popup = new maplibregl.Popup({ offset: 14, maxWidth: "340px" });
popup.on("close", () => {
  if (!overlayOpen()) stopPlayer();
  stopPopupRetry();
  popupUid = null;
});

// ------------------------------------------------------------------- map

const map = new maplibregl.Map({
  container: "map",
  center: [0, 20],
  zoom: 2,
  attributionControl: { compact: true },
  style: {
    version: 8,
    // glyphs are required for the text/symbol label layer to render.
    // NOTE: fonts.openmaptiles.org started returning HTML instead of pbf
    // (2026-07) which silently broke ALL rendering of the devices source
    // ("Unimplemented type: 4" in the map error event) — dots and labels
    // both vanished. demotiles.maplibre.org is the maintained fallback;
    // for air-gapped sites self-host the glyphs and point this at them.
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#10151c" } },
      { id: "osm", type: "raster", source: "osm",
        paint: { "raster-brightness-max": 0.75, "raster-saturation": -0.35 } },
    ],
  },
});
map.addControl(new maplibregl.NavigationControl(), "bottom-right");

map.on("load", () => {
  map.addSource("devices", { type: "geojson", data: featureCollection() });

  map.addLayer({
    id: "device-dots",
    type: "circle",
    source: "devices",
    paint: {
      "circle-radius": ["case", ["get", "alerting"], 12, 8],
      "circle-color": ["get", "color"],
      "circle-opacity": ["case", ["get", "stale"], 0.35, 0.95],
      "circle-stroke-width": ["case", ["get", "alerting"], 4,
        ["case", ["get", "hasVideo"], 3, 2]],
      "circle-stroke-color": ["case", ["get", "alerting"], "#ff8888",
        ["case", ["get", "hasVideo"], CAMERA_COLOR, "#0d1117"]],
    },
  });

  map.addLayer({
    id: "device-labels",
    type: "symbol",
    source: "devices",
    layout: {
      "text-field": ["get", "label"],
      // demotiles only serves this font; the default Open Sans Regular 404s
      "text-font": ["Open Sans Semibold"],
      "text-size": 11,
      "text-offset": [0, 1.4],
      "text-anchor": "top",
      "text-allow-overlap": true,
    },
    paint: {
      "text-color": "#e6edf3",
      "text-halo-color": "#0d1117",
      "text-halo-width": 1.4,
    },
  });

  map.on("click", "device-dots", (e) => openPopup(e.features[0].properties.uid));
  map.on("mouseenter", "device-dots", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "device-dots", () => (map.getCanvas().style.cursor = ""));

  mapReady = true;
  refresh(); // render anything that arrived before the map finished loading
});

// Auth/login must NOT depend on the map loading — run it immediately so the
// login screen works even if map tiles are slow or blocked.
gateOnAuth();

let mapReady = false;
let liveStarted = false;
function startLive() {
  if (liveStarted) return;
  liveStarted = true;
  connectSSE();
  pollStreams();
  loadChatHistory();
  setInterval(refresh, 5000);
  setInterval(pollStreams, 10000);
}

// ---------------------------------------------------------------- auth gate

async function gateOnAuth() {
  let me = { auth_required: false };
  try { me = await (await fetch("/api/me")).json(); } catch { /* ignore */ }
  if (me && me.auth) {          // logged in
    showUserChip(me);
    startLive();
  } else if (me && me.auth_required) {   // auth on, not logged in
    showLogin();
  } else {                      // auth disabled
    startLive();
  }
}

function showLogin() {
  const ov = document.getElementById("login-overlay");
  ov.hidden = false;
  const form = document.getElementById("login-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = document.getElementById("login-error");
    err.hidden = true;
    const resp = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("login-user").value,
        password: document.getElementById("login-pass").value,
      }),
    });
    if (resp.ok) { location.reload(); }
    else { err.hidden = false; }
  };
  document.getElementById("login-user").focus();
}

function showUserChip(me) {
  const bar = document.getElementById("topbar");
  const chip = document.createElement("span");
  chip.id = "user-chip";
  chip.innerHTML = `<b>${escapeHtml(me.username)}</b> · ${escapeHtml(me.role)}`;
  const out = document.createElement("button");
  out.id = "logout-btn"; out.textContent = "Sign out";
  out.onclick = async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  };
  bar.appendChild(chip);
  bar.appendChild(out);
  // hide admin-only controls for non-admins
  if (me.role !== "admin") {
    for (const id of ["enroll-btn", "users-btn", "devices-btn"]) {
      const b = document.getElementById(id);
      if (b) b.style.display = "none";
    }
  }
}

// ------------------------------------------------------------------ data

function connectSSE() {
  const es = new EventSource("/api/stream");
  const conn = document.getElementById("conn");
  es.onopen = () => { conn.textContent = "live"; conn.className = "pill online"; };
  es.onerror = () => { conn.textContent = "reconnecting…"; conn.className = "pill offline"; };
  es.onmessage = (msg) => {
    const d = JSON.parse(msg.data);
    if (d.kind === "position" && d.lat != null && d.lon != null) {
      const prev = devices.get(d.uid) || {};
      devices.set(d.uid, { ...prev, ...d, lastSeen: Date.now(), offline: false });
      refresh();
    } else if (d.kind === "offline" && devices.has(d.uid)) {
      devices.get(d.uid).offline = true;
      refresh();
    } else if (d.kind === "removed") {
      removeDeviceLocal(d.uid);
    } else if (d.kind === "cleared") {
      clearAllDevicesLocal();
    } else if (d.kind === "chat") {
      addChatMessage(d);
    } else if (d.kind === "alert") {
      raiseAlert(d);
    } else if (d.kind === "alert_clear") {
      clearAlert(d.uid);
    }
  };
}

// Admin removed a device (or all) — drop it from the live map/roster without a
// reload. Fired for every open session via the SSE "removed"/"cleared" events.
let devicesPanelRefresh = null;   // set while the Devices panel is open

function removeDeviceLocal(uid) {
  devices.delete(uid);
  streamsByDevice.delete(uid);
  if (alerts.has(uid)) clearAlert(uid);
  refresh();
  if (devicesPanelRefresh) devicesPanelRefresh();
}

function clearAllDevicesLocal() {
  devices.clear();
  streamsByDevice.clear();
  alerts.clear();
  document.getElementById("alert-banner").hidden = true;
  refresh();
  if (devicesPanelRefresh) devicesPanelRefresh();
}

// ---------------------------------------------------------------- chat

const chatSeen = new Set();
let chatUnread = 0;

function addChatMessage(m, replayOnly = false) {
  if (m.id != null) {
    if (chatSeen.has(m.id)) return;
    chatSeen.add(m.id);
  }
  const log = document.getElementById("chat-log");
  const el = document.createElement("div");
  el.className = "msg" + (m.sender_uid === "takcore-server" ? " self" : "");
  const when = new Date((m.ts || Date.now() / 1000) * 1000)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  el.innerHTML = `<span class="who">${escapeHtml(m.sender || "?")}</span>` +
    `${escapeHtml(m.message)}<span class="when">${when}</span>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  if (!replayOnly && document.getElementById("chat-panel").hidden) {
    chatUnread++;
    updateChatBadge();
  }
}

function updateChatBadge() {
  let b = document.querySelector("#chat-btn .badge");
  if (chatUnread <= 0) { if (b) b.remove(); return; }
  if (!b) {
    b = document.createElement("span");
    b.className = "badge";
    document.getElementById("chat-btn").appendChild(b);
  }
  b.textContent = chatUnread;
}

async function loadChatHistory() {
  try {
    const rows = await (await fetch("/api/chat")).json();
    rows.forEach((m) => addChatMessage(m, true));
  } catch { /* ignore */ }
}

document.getElementById("chat-btn").onclick = () => {
  const p = document.getElementById("chat-panel");
  p.hidden = !p.hidden;
  if (!p.hidden) { chatUnread = 0; updateChatBadge();
    document.getElementById("chat-input").focus(); }
};
document.getElementById("chat-close").onclick = () =>
  (document.getElementById("chat-panel").hidden = true);
document.getElementById("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const msg = input.value.trim();
  if (!msg) return;
  input.value = "";
  await fetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: msg, sender: "TAKCORE" }),
  });
};

// ---------------------------------------------------------------- alerts

const alerts = new Map(); // uid -> alert

function raiseAlert(a) {
  alerts.set(a.uid, a);
  const banner = document.getElementById("alert-banner");
  const names = [...alerts.values()]
    .map((x) => `${x.callsign || x.uid} (${x.alert_type || "emergency"})`);
  banner.textContent = "🚨 EMERGENCY: " + names.join("  •  ") + " — click to locate";
  banner.hidden = false;
  banner.onclick = () => {
    const first = [...alerts.values()][0];
    if (first && first.lat != null)
      map.flyTo({ center: [first.lon, first.lat], zoom: 15 });
  };
  const d = devices.get(a.uid);
  if (d) { d.alerting = true; refresh(); }
}

function clearAlert(uid) {
  alerts.delete(uid);
  const d = devices.get(uid);
  if (d) { d.alerting = false; refresh(); }
  const banner = document.getElementById("alert-banner");
  if (alerts.size === 0) banner.hidden = true;
  else raiseAlert([...alerts.values()][0]);
}

async function pollStreams() {
  try {
    const rows = await (await fetch("/api/streams", { cache: "no-store" })).json();
    streamsByDevice.clear();
    for (const s of rows) {
      if (!s.device_uid) continue;
      if (!streamsByDevice.has(s.device_uid)) streamsByDevice.set(s.device_uid, []);
      streamsByDevice.get(s.device_uid).push(s);
    }
    refresh();
  } catch { /* server restarting; next poll retries */ }
}

function hasVideo(uid) { return streamsByDevice.has(uid); }

function featureCollection() {
  const features = [];
  for (const d of devices.values()) {
    if (d.lat == null || d.lon == null) continue;
    const isCam = d.platform === "camera";
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [d.lon, d.lat] },
      properties: {
        uid: d.uid,
        label: (d.alerting ? "🚨 " : "") + (hasVideo(d.uid) ? "📹 " : "") +
               (d.callsign || d.uid),
        color: d.alerting ? "#ff2d2d"
               : (isCam ? CAMERA_COLOR : (TEAM_COLORS[d.team] || DEFAULT_COLOR)),
        stale: d.alerting ? false : (isCam ? false : isStale(d)),
        hasVideo: hasVideo(d.uid),
        alerting: !!d.alerting,
      },
    });
  }
  return { type: "FeatureCollection", features };
}

function isStale(d) {
  return d.offline || Date.now() - (d.lastSeen || 0) > STALE_MS;
}

function refresh() {
  renderRoster();
  document.getElementById("counts").textContent =
    `${devices.size} device${devices.size === 1 ? "" : "s"}`;
  if (playbackMode) return; // playback controls the map source
  const src = map.getSource("devices");
  if (src) src.setData(featureCollection());
}

// ------------------------------------------------------------- WHEP video

function whepUrl(path, token) {
  // When the page is served on a standard port (location.port === "", i.e.
  // through the HTTPS reverse proxy), MediaMTX is reached SAME-ORIGIN and Caddy
  // routes /<path>/whep to it. Forcing :8889 there would be a cross-port,
  // plain-HTTP request that a secure page blocks as mixed content, so video
  // would silently fail. On direct LAN access (http://host:8080) there is a
  // port, so we still dial MediaMTX on :8889 as before.
  const port = location.port ? `:${WEBRTC_PORT}` : "";
  const base = `${location.protocol}//${location.hostname}${port}/${path}/whep`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

// Ask takcore (with our session cookie) for a short-lived ticket that
// authorizes WebRTC playback of this exact path.
async function streamTicket(path) {
  try {
    const r = await fetch(`/api/streams/ticket?path=${encodeURIComponent(path)}`,
                          { cache: "no-store" });
    if (!r.ok) return "";
    return (await r.json()).token || "";
  } catch { return ""; }
}

// Address the PHONE should publish to. Prefer the server's advertised
// LAN address (SERVER_HOST) — location.hostname is "localhost" when the
// map is viewed on the server itself, which a phone can't dial.
let publishHostCache = "";
let publishTokenCache = "";
async function publishHost() {
  if (publishHostCache) return publishHostCache;
  let host = "";
  try {
    const cfg = await (await fetch("/api/config")).json();
    host = cfg.server_host || "";
    publishTokenCache = cfg.publish_token || "";
  } catch { /* ignore */ }
  if (!host || host === "localhost" || host === "127.0.0.1")
    host = localStorage.getItem("takServerHost") || location.hostname;
  publishHostCache = host;
  return host;
}

// Offer/answer dance shared by the popup player and the camera grid.
async function negotiateWhep(pc, path) {
  const token = await streamTicket(path);
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await new Promise((res) => {
    if (pc.iceGatheringState === "complete") return res();
    pc.addEventListener("icegatheringstatechange",
      () => pc.iceGatheringState === "complete" && res());
    setTimeout(res, 1500); // don't wait forever on ICE
  });
  const resp = await fetch(whepUrl(path, token), {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: pc.localDescription.sdp,
  });
  if (!resp.ok) throw new Error(`WHEP ${resp.status}`);
  await pc.setRemoteDescription({ type: "answer", sdp: await resp.text() });
}

async function startPlayer(path, videoEl, statusEl) {
  stopPlayer();
  const pc = new RTCPeerConnection();
  player = { pc, video: videoEl, path };
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.ontrack = (e) => { videoEl.srcObject = e.streams[0]; };
  pc.onconnectionstatechange = () => {
    if (!statusEl) return;
    if (pc.connectionState === "connected") statusEl.textContent = "";
    else if (["failed", "disconnected"].includes(pc.connectionState))
      statusEl.textContent = "stream lost — close and reopen to retry";
  };
  try {
    await negotiateWhep(pc, path);
  } catch (err) {
    if (statusEl) {
      const host = await publishHost();
      statusEl.classList.add("expanded"); // flow below the video, don't overlay
      statusEl.innerHTML =
        `no live video (${escapeHtml(err.message)}) — is the device streaming?` +
        publishHintHtml(host, path);
      wirePublishHint(statusEl, host, path);
    }
    stopPlayer();
  }
}

// A phone with no live stream gets setup help: the publish URLs plus a
// setup QR for the right app. The publish URLs are identical for every
// device; only the deep link differs — Android/ATAK gets an OpenTAK ICU
// link (opentakicu://import), iOS/iTAK gets a Larix Broadcaster Grove
// link. See streamApp()/setupDeeplink() below.
const ICU_PORTS = { rtsp: 8554, rtmp: 1935, srt: 8890 };
const PUBLISH_PROTOS = ["rtsp", "rtmp", "srt"];
const PUBLISH_USER = "publisher";

function deviceForPath(path) {
  return devices.get(path.split("/").pop());
}

function labelForPath(path) {
  const d = deviceForPath(path);
  return (d && (d.callsign || d.uid)) || path;
}

function publishUrl(proto, host, path) {
  const t = publishTokenCache;
  if (proto === "srt") {
    // MediaMTX SRT auth reads user/pass from the streamid.
    const sid = t ? `publish:${path}:${PUBLISH_USER}:${t}` : `publish:${path}`;
    return `srt://${host}:8890?streamid=${sid}`;
  }
  const auth = t ? `${PUBLISH_USER}:${t}@` : "";
  return `${proto}://${auth}${host}:${ICU_PORTS[proto]}/${path}`;
}

// OpenTAK ICU deeplink. Param names match ICU's Preferences constants.
// ICU builds the SRT streamid itself as publish:<path>:<username>:<password>
// from these fields (verified against a real MediaMTX SRT reject log), and
// uses username/password for RTMP/RTSP auth too — so ALWAYS send them and keep
// the path clean. (Do NOT fold creds into the path: ICU would then append its
// own username/password on top, producing a doubled, invalid streamid.)
function icuDeeplink(proto, host, path) {
  const t = publishTokenCache;
  let url = `opentakicu://import?protocol=${proto}` +
    `&address=${encodeURIComponent(host)}` +
    `&port=${ICU_PORTS[proto]}` +
    `&path=${encodeURIComponent(path)}`;
  if (t) url += `&username=${encodeURIComponent(PUBLISH_USER)}` +
               `&password=${encodeURIComponent(t)}`;
  return url;
}

// Which phone streaming app fits this device. iOS has no OpenTAK ICU, so
// iTAK/iOS devices use Larix Broadcaster; everything else uses ICU.
function streamApp(d) {
  const p = ((d && d.platform) || "").toLowerCase();
  return (p.includes("itak") || p.includes("ios")) ? "larix" : "icu";
}

// Larix Broadcaster config via a Larix Grove deep link (iOS camera publisher).
// Format reference: https://softvelum.com/larix/grove/
function larixGroveUrl(proto, host, path) {
  const t = publishTokenCache;
  const parts = [
    `conn[][name]=${encodeURIComponent("TAK-Revamp " + proto.toUpperCase())}`,
    `conn[][mode]=va`,
    `conn[][overwrite]=on`,
  ];
  if (proto === "srt") {
    // Larix carries the SRT streamid in its own field, not in the URL query.
    // MediaMTX takes publish credentials from the streamid, so embed them
    // there (matching publishUrl's SRT form) rather than as an SRT
    // passphrase (conn[][srtpass]).
    parts.splice(1, 0, `conn[][url]=${encodeURIComponent(`srt://${host}:${ICU_PORTS.srt}`)}`);
    parts.push(`conn[][srtmode]=c`);
    const sid = t ? `publish:${path}:${PUBLISH_USER}:${t}` : `publish:${path}`;
    parts.push(`conn[][srtstreamid]=${encodeURIComponent(sid)}`);
  } else {
    // RTMP/RTSP DO support user/pass auth in Larix, so set the dedicated auth
    // fields (publishUrl() also embeds user:pass@ in the URL as a backup).
    // SRT has NO user/pass concept — Larix rejects conn[][user]/[pass] for it
    // ("User/pass authentication is not supported"), so SRT creds ride in the
    // streamid above instead of here.
    parts.splice(1, 0, `conn[][url]=${encodeURIComponent(publishUrl(proto, host, path))}`);
    if (t) {
      parts.push(`conn[][user]=${encodeURIComponent(PUBLISH_USER)}`);
      parts.push(`conn[][pass]=${encodeURIComponent(t)}`);
    }
  }
  return `larix://set/v1?${parts.join("&")}`;
}

// Pick the right setup deep link for the device's app.
function setupDeeplink(app, proto, host, path) {
  return app === "larix" ? larixGroveUrl(proto, host, path)
                         : icuDeeplink(proto, host, path);
}

// Build the "publish to…" hint (URLs + QR buttons) shown when a device has
// no playable video.
function publishHintHtml(host, path) {
  const app = streamApp(deviceForPath(path));
  const appName = app === "larix" ? "Larix Broadcaster (iOS)" : "OpenTAK ICU";
  const urls = PUBLISH_PROTOS.map((p) =>
    `${p.toUpperCase()} <code>${escapeHtml(publishUrl(p, host, path))}</code>`
  ).join("<br>");
  const btns = PUBLISH_PROTOS.map((p) =>
    `<button data-proto="${p}">${p.toUpperCase()}</button>`).join("");
  return `<div class="publish-hint">In ${escapeHtml(appName)}, publish to:<br>${urls}` +
    `<div class="icu-qr-row">📱 ${escapeHtml(appName)} setup QR:${btns}</div></div>`;
}

function wirePublishHint(rootEl, host, path) {
  rootEl.querySelectorAll(".icu-qr-row button").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      showSetupQrModal(btn.dataset.proto, host, path);
    };
  });
}

function showSetupQrModal(proto, host, path) {
  closeIcuQrModal();
  const name = labelForPath(path);
  const app = streamApp(deviceForPath(path));
  const appName = app === "larix" ? "Larix Broadcaster" : "OpenTAK ICU";
  const cap = app === "larix"
    ? "Scan with the phone's camera — opens Larix Broadcaster pre-configured to publish here."
    : "Scan with the phone's camera — opens OpenTAK ICU (1.5.4+) pre-configured to publish here.";
  const ov = document.createElement("div");
  ov.id = "icu-qr-overlay";
  ov.innerHTML = `
    <div class="icu-qr-card">
      <header>
        <span>${escapeHtml(name)} · ${proto.toUpperCase()} stream · ${escapeHtml(appName)}</span>
        <button class="icu-qr-close" title="Close">✕</button>
      </header>
      <div class="icu-qr-code"></div>
      <p class="icu-qr-cap">${escapeHtml(cap)}</p>
      <code>${escapeHtml(publishUrl(proto, host, path))}</code>
    </div>`;
  document.body.appendChild(ov);
  new QRCode(ov.querySelector(".icu-qr-code"),
    { text: setupDeeplink(app, proto, host, path), width: 220, height: 220,
      correctLevel: QRCode.CorrectLevel.M });
  ov.querySelector(".icu-qr-close").onclick = closeIcuQrModal;
  ov.addEventListener("click", (e) => { if (e.target === ov) closeIcuQrModal(); });
}

function closeIcuQrModal() {
  const ov = document.getElementById("icu-qr-overlay");
  if (ov) ov.remove();
}

function stopPlayer() {
  if (player) {
    try { player.pc.close(); } catch { /* already closed */ }
    if (player.video) player.video.srcObject = null;
    player = null;
  }
}

// -------------------------------------------------------- fullscreen view

function overlayOpen() {
  return !!document.getElementById("video-overlay");
}

function expandVideo(path, title) {
  const ov = document.createElement("div");
  ov.id = "video-overlay";
  ov.innerHTML = `
    <div class="ov-bar">
      <span>${escapeHtml(title)}</span>
      <button id="ov-close" title="Close">✕</button>
    </div>
    <video autoplay playsinline muted></video>
    <div class="ov-status"></div>`;
  document.body.appendChild(ov);
  const video = ov.querySelector("video");
  const status = ov.querySelector(".ov-status");
  ov.querySelector("#ov-close").onclick = closeOverlay;
  ov.addEventListener("click", (e) => { if (e.target === ov) closeOverlay(); });
  startPlayer(path, video, status);
}

function closeOverlay() {
  const ov = document.getElementById("video-overlay");
  if (ov) ov.remove();
  stopPlayer();
}

// ------------------------------------------------------ multi-camera grid

const GRID_MAX = 10;
const gridTiles = new Map(); // path -> {pc, el}
let gridFocus = null;

function streamLabel(s) {
  const d = s.device_uid ? devices.get(s.device_uid) : null;
  return (d && (d.callsign || d.uid)) || s.name || s.path;
}

async function openGrid() {
  if (document.getElementById("grid-overlay")) return;
  if (popup) popup.remove();
  stopPlayer();
  const ov = document.createElement("div");
  ov.id = "grid-overlay";
  ov.innerHTML = `
    <div class="ov-bar">
      <span>⊞ Cameras</span>
      <span id="grid-count" class="pill"></span>
      <button id="grid-pick">☰ Choose cameras</button>
      <button id="grid-close" title="Close">✕</button>
    </div>
    <div id="grid-area"></div>
    <div id="grid-picker" hidden></div>`;
  document.body.appendChild(ov);
  ov.querySelector("#grid-close").onclick = closeGrid;
  ov.querySelector("#grid-pick").onclick = toggleGridPicker;
  let rows = [];
  try { rows = await (await fetch("/api/streams")).json(); }
  catch { /* server restarting; picker can retry */ }
  rows.filter((s) => s.ready).slice(0, GRID_MAX).forEach(addGridTile);
  layoutGrid();
}

function addGridTile(s) {
  if (gridTiles.has(s.path) || gridTiles.size >= GRID_MAX) return;
  const area = document.getElementById("grid-area");
  if (!area) return;
  const el = document.createElement("div");
  el.className = "grid-tile";
  el.innerHTML = `
    <video autoplay playsinline muted></video>
    <span class="gt-label">${escapeHtml(streamLabel(s))}</span>
    <span class="gt-status">connecting…</span>`;
  area.appendChild(el);
  const video = el.querySelector("video");
  const status = el.querySelector(".gt-status");
  video.addEventListener("playing", () => (status.textContent = ""));
  el.onclick = () => {
    gridFocus = gridFocus === s.path ? null : s.path;
    layoutGrid();
  };
  const pc = new RTCPeerConnection();
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.ontrack = (e) => { video.srcObject = e.streams[0]; };
  pc.onconnectionstatechange = () => {
    if (["failed", "disconnected"].includes(pc.connectionState))
      status.textContent = "stream lost";
  };
  gridTiles.set(s.path, { pc, el });
  negotiateWhep(pc, s.path)
    .catch((err) => (status.textContent = `no video (${err.message})`));
  layoutGrid();
}

function removeGridTile(path) {
  const t = gridTiles.get(path);
  if (!t) return;
  try { t.pc.close(); } catch { /* already closed */ }
  t.el.remove();
  gridTiles.delete(path);
  if (gridFocus === path) gridFocus = null;
  layoutGrid();
}

function layoutGrid() {
  const area = document.getElementById("grid-area");
  if (!area) return;
  const n = gridTiles.size;
  document.getElementById("grid-count").textContent =
    `${n}/${GRID_MAX} camera${n === 1 ? "" : "s"}`;
  let empty = area.querySelector(".grid-empty");
  if (!n) {
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "grid-empty";
      empty.textContent =
        "no live cameras — ☰ Choose cameras to add feeds";
      area.appendChild(empty);
    }
  } else if (empty) empty.remove();
  const focused = !!gridFocus && gridTiles.has(gridFocus);
  area.classList.toggle("focused", focused);
  // square-ish tiling: 1 → 1 col, 2-4 → 2, 5-9 → 3, 10 → 4
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  area.style.setProperty("--grid-cols", cols);
  for (const [path, t] of gridTiles)
    t.el.classList.toggle("focus", focused && path === gridFocus);
}

async function toggleGridPicker() {
  const box = document.getElementById("grid-picker");
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = "loading…";
  let rows = [];
  try { rows = await (await fetch("/api/streams")).json(); }
  catch { /* ignore */ }
  if (!rows.length) {
    box.innerHTML = "no streams registered — enroll a device or add an IP camera";
    return;
  }
  box.innerHTML = "";
  rows.forEach((s) => {
    const btn = document.createElement("button");
    btn.className = "gp-item" + (gridTiles.has(s.path) ? " on" : "");
    btn.innerHTML = `
      <b>${escapeHtml(streamLabel(s))}</b>
      <span class="gp-live${s.ready ? " on" : ""}">${s.ready ? "● live" : "○ offline"}</span>
      <span class="gp-path">${escapeHtml(s.path)}</span>`;
    btn.onclick = () => {
      if (gridTiles.has(s.path)) removeGridTile(s.path);
      else addGridTile(s);
      btn.classList.toggle("on", gridTiles.has(s.path));
    };
    box.appendChild(btn);
  });
}

function closeGrid() {
  for (const path of [...gridTiles.keys()]) removeGridTile(path);
  gridFocus = null;
  const ov = document.getElementById("grid-overlay");
  if (ov) ov.remove();
}

document.getElementById("grid-btn").onclick = openGrid;
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.getElementById("icu-qr-overlay")) closeIcuQrModal();
  else if (gridFocus) { gridFocus = null; layoutGrid(); }
  else closeGrid();
});

// ---------------------------------------------------------------- popup

async function openPopup(uid) {
  const d = devices.get(uid);
  if (!d) return;
  const gen = ++popupGen;
  popupUid = uid;
  stopPopupRetry();
  popup.setLngLat([d.lon, d.lat]).setHTML(popupHtml(d)).addTo(map);

  // Resolve the publish host up front, then the single guard below covers all
  // awaits — after it, rendering is synchronous and cannot race a popup close
  // (which was leaving the popup stuck on "checking for live video…").
  const host = await publishHost();
  if (gen !== popupGen) return; // a newer openPopup superseded this one
  await updatePopupVideo(uid, d, host, gen);
}

// Decide what the popup's video slot shows: the live player if a stream is
// ready, otherwise the setup hint. While no video is ready it re-checks on a
// timer so the popup upgrades to video on its own once the device starts
// streaming — no manual reopen or page refresh needed.
async function updatePopupVideo(uid, d, host, gen) {
  let streams = streamsByDevice.get(uid) || [];
  try {
    streams = await (await fetch(
      `/api/devices/${encodeURIComponent(uid)}/streams`,
      { cache: "no-store" })).json();
  } catch { /* keep cached */ }
  if (gen !== popupGen) return;
  const slot = document.getElementById("popup-video-slot");
  if (!slot) return;

  const playable = streams.find((s) => s.ready);
  if (!playable) {
    // No live video yet. Show the setup hint (only build it once so the QR
    // buttons don't flicker each poll) and keep watching.
    if (!slot.querySelector(".no-stream")) {
      const path = streams.length ? streams[0].path : `live/${uid}`;
      slot.innerHTML =
        `<div class="video-slot no-stream">` +
        `<div class="ns-lead">📹 no live video yet — set up streaming from this device</div>` +
        publishHintHtml(host, path) + `</div>`;
      wirePublishHint(slot, host, path);
    }
    schedulePopupRetry(uid, d, host, gen);
    return;
  }
  if (slot.querySelector(".video-box")) return; // already playing
  renderPopupPlayer(slot, playable.path, d, playable.recording);
}

function renderPopupPlayer(slot, path, d, recording) {
  slot.innerHTML = `
    <div class="video-box">
      <video autoplay playsinline muted></video>
      <div class="video-status">connecting…</div>
      <div class="video-controls">
        <button class="vc-expand" title="Expand">⛶ Expand</button>
        <button class="vc-record${recording ? " on" : ""}"
          title="Record this feed to disk">${recording ? "● REC" : "○ Rec"}</button>
        <button class="vc-rec" title="Recorded clips">⏺ Recorded</button>
        <span class="vc-path">${escapeHtml(path)}</span>
      </div>
      <div class="rec-list" hidden></div>
    </div>`;
  const video = slot.querySelector("video");
  const status = slot.querySelector(".video-status");
  video.addEventListener("playing", () => (status.textContent = ""));
  slot.querySelector(".vc-expand").onclick = () => {
    popup.remove();
    expandVideo(path, d.callsign || d.uid);
  };
  slot.querySelector(".vc-record").onclick = (e) =>
    toggleRecord(e.currentTarget, path);
  slot.querySelector(".vc-rec").onclick = () =>
    toggleRecordings(slot, path, video, status);
  startPlayer(path, video, status);
}

// Turn disk recording on/off for one live feed (operator+). Optimistically
// flips the button, reverting if the server rejects it.
async function toggleRecord(btn, path) {
  const enable = !btn.classList.contains("on");
  btn.disabled = true;
  try {
    const r = await fetch("/api/streams/record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, enabled: enable }),
    });
    if (!r.ok) {
      const msg = r.status === 403 ? "needs operator role"
        : r.status === 502 ? "media server unavailable" : `HTTP ${r.status}`;
      throw new Error(msg);
    }
    const j = await r.json();
    btn.classList.toggle("on", !!j.recording);
    btn.textContent = j.recording ? "● REC" : "○ Rec";
  } catch (err) {
    alert("Couldn't change recording: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

let popupRetryTimer = null;
function stopPopupRetry() {
  if (popupRetryTimer) { clearTimeout(popupRetryTimer); popupRetryTimer = null; }
}
function schedulePopupRetry(uid, d, host, gen) {
  stopPopupRetry();
  popupRetryTimer = setTimeout(() => {
    if (gen !== popupGen) return; // popup changed or closed
    updatePopupVideo(uid, d, host, gen);
  }, 3000);
}

async function toggleRecordings(slot, path, video, status) {
  const box = slot.querySelector(".rec-list");
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = "loading…";
  let segs = [];
  try { segs = await (await fetch(
    `/api/recordings?path=${encodeURIComponent(path)}`)).json(); }
  catch { /* ignore */ }
  if (!segs.length) { box.innerHTML = "no recordings yet"; return; }
  box.innerHTML = "";
  segs.slice(-20).reverse().forEach((seg) => {
    const a = document.createElement("button");
    a.className = "rec-item";
    const start = new Date(seg.start);
    a.textContent = `${start.toLocaleString()} · ${Math.round(seg.duration)}s`;
    a.onclick = () => {
      stopPlayer();
      const url = `${location.protocol}//${location.hostname}:9996/get` +
        `?path=${encodeURIComponent(path)}` +
        `&start=${encodeURIComponent(seg.start)}` +
        `&duration=${Math.min(seg.duration, 600)}`;
      video.srcObject = null;
      video.src = url;
      video.play().catch(() => {});
      if (status) status.textContent = "▶ recorded";
    };
    box.appendChild(a);
  });
}

function popupHtml(d) {
  const speed = d.speed != null ? `${(d.speed * 3.6).toFixed(1)} km/h` : "—";
  const seen = d.lastSeen ? `${Math.round((Date.now() - d.lastSeen) / 1000)} s ago` : "—";
  return `
    <div class="popup">
      <h3>${escapeHtml(d.callsign || d.uid)}</h3>
      <table>
        <tr><td>Team</td><td>${escapeHtml(d.team)} ${escapeHtml(d.role || "")}</td></tr>
        <tr><td>Position</td><td>${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}</td></tr>
        <tr><td>Speed</td><td>${speed}</td></tr>
        <tr><td>Device</td><td>${escapeHtml(d.device || d.platform)}</td></tr>
        <tr><td>Battery</td><td>${d.battery != null ? d.battery + " %" : "—"}</td></tr>
        <tr><td>Last seen</td><td>${seen}</td></tr>
      </table>
      <div id="popup-video-slot">
        <div class="video-slot">checking for live video…</div>
      </div>
    </div>`;
}

function escapeHtml(s) {
  return String(s ?? "—").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------------------------------------------------------------- roster

function renderRoster() {
  const ul = document.getElementById("roster-list");
  ul.innerHTML = "";
  const sorted = [...devices.values()].sort((a, b) =>
    (a.callsign || a.uid).localeCompare(b.callsign || b.uid));
  for (const d of sorted) {
    const li = document.createElement("li");
    if (isStale(d) && d.platform !== "camera") li.classList.add("stale");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = d.platform === "camera"
      ? CAMERA_COLOR : (TEAM_COLORS[d.team] || DEFAULT_COLOR);
    const cs = document.createElement("span");
    cs.className = "cs";
    cs.textContent = (hasVideo(d.uid) ? "📹 " : "") + (d.callsign || d.uid);
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = d.role || d.platform || "";
    li.append(dot, cs, meta);
    li.onclick = () => {
      map.flyTo({ center: [d.lon, d.lat], zoom: Math.max(map.getZoom(), 14) });
      openPopup(d.uid);
    };
    ul.appendChild(li);
  }
}

// ------------------------------------------------------------- controls

document.getElementById("fit").onclick = () => {
  const pts = [...devices.values()].filter((d) => d.lat != null);
  if (!pts.length) return;
  const bounds = new maplibregl.LngLatBounds();
  pts.forEach((d) => bounds.extend([d.lon, d.lat]));
  map.fitBounds(bounds, { padding: 80, maxZoom: 15 });
};

// ------------------------------------------------------------- playback

let playbackMode = false;
let pbHistory = [];      // {uid, ts, lat, lon} oldest->newest
let pbRange = [0, 0];    // [minTs, maxTs] in seconds
let pbTimer = null;

function ensureTrailLayer() {
  if (map.getSource("trails")) return;
  map.addSource("trails", { type: "geojson",
    data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "trail-lines", type: "line", source: "trails",
    paint: { "line-color": ["get", "color"], "line-width": 2.5,
             "line-opacity": 0.7 },
  }, "device-dots");
}

document.getElementById("playback-btn").onclick = async () => {
  if (playbackMode) return;
  let rows;
  try { rows = await (await fetch("/api/history?minutes=120")).json(); }
  catch { alert("Could not load history."); return; }
  if (!rows.length) { alert("No track history recorded yet."); return; }
  pbHistory = rows;
  pbRange = [rows[0].ts, rows[rows.length - 1].ts];
  playbackMode = true;
  ensureTrailLayer();
  document.getElementById("playback-bar").hidden = false;
  const slider = document.getElementById("pb-slider");
  slider.value = 1000;
  renderPlaybackAt(pbRange[1]);
};

function pbValueToTs(v) {
  return pbRange[0] + (pbRange[1] - pbRange[0]) * (v / 1000);
}

function renderPlaybackAt(t) {
  // latest position per uid at or before t; trail = points up to t
  const latest = new Map();
  const trailPts = new Map();
  for (const p of pbHistory) {
    if (p.ts > t) continue;
    latest.set(p.uid, p);
    if (!trailPts.has(p.uid)) trailPts.set(p.uid, []);
    trailPts.get(p.uid).push([p.lon, p.lat]);
  }
  // device dots at historical positions (reuse live metadata for color/name)
  const feats = [];
  for (const [uid, p] of latest) {
    const d = devices.get(uid) || {};
    const isCam = d.platform === "camera";
    feats.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.lon, p.lat] },
      properties: {
        uid, label: d.callsign || uid,
        color: isCam ? CAMERA_COLOR : (TEAM_COLORS[d.team] || DEFAULT_COLOR),
        stale: false, hasVideo: false, alerting: false,
      },
    });
  }
  map.getSource("devices").setData({ type: "FeatureCollection", features: feats });
  // trails
  const lines = [];
  for (const [uid, pts] of trailPts) {
    if (pts.length < 2) continue;
    const d = devices.get(uid) || {};
    lines.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: pts },
      properties: { color: TEAM_COLORS[d.team] || DEFAULT_COLOR },
    });
  }
  map.getSource("trails").setData({ type: "FeatureCollection", features: lines });
  document.getElementById("pb-time").textContent =
    new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

document.getElementById("pb-slider").oninput = (e) => {
  stopPbPlay();
  renderPlaybackAt(pbValueToTs(+e.target.value));
};

function stopPbPlay() {
  if (pbTimer) { clearInterval(pbTimer); pbTimer = null;
    document.getElementById("pb-play").textContent = "▶"; }
}

document.getElementById("pb-play").onclick = () => {
  if (pbTimer) { stopPbPlay(); return; }
  document.getElementById("pb-play").textContent = "⏸";
  const slider = document.getElementById("pb-slider");
  if (+slider.value >= 1000) slider.value = 0;
  pbTimer = setInterval(() => {
    let v = +slider.value + 8;   // ~ full sweep in ~12s
    if (v >= 1000) { v = 1000; stopPbPlay(); }
    slider.value = v;
    renderPlaybackAt(pbValueToTs(v));
  }, 100);
};

document.getElementById("pb-close").onclick = () => {
  stopPbPlay();
  playbackMode = false;
  document.getElementById("playback-bar").hidden = true;
  if (map.getSource("trails"))
    map.getSource("trails").setData({ type: "FeatureCollection", features: [] });
  refresh(); // back to live
};

// ------------------------------------------------------------- enrollment

// The phones must dial the server by an address THEY can reach — the Mac's
// LAN IP, never "localhost" (that would be the phone itself). We ask the
// server for its advertised address; if it isn't set (or is localhost), we
// prompt once and remember it.
async function resolveServerHost() {
  let host = "";
  try { host = (await (await fetch("/api/config")).json()).server_host || ""; }
  catch { /* ignore */ }
  if (!host) host = localStorage.getItem("takServerHost") || "";
  const looksLocal = !host || host === "localhost" || host === "127.0.0.1";
  const browserHost = location.hostname;
  if (looksLocal) {
    const guess = (browserHost && browserHost !== "localhost" &&
                   browserHost !== "127.0.0.1") ? browserHost : "";
    host = prompt(
      "Enter this server's address — the one phones use to reach it: the DDNS " +
      "hostname (e.g. rtak.ddns.net) or the LAN IP (e.g. 192.168.2.190)." +
      "\n\nNOT 'localhost'.", guess) || "";
    host = host.trim();
    if (host) localStorage.setItem("takServerHost", host);
  }
  return host;
}

document.getElementById("enroll-btn").onclick = async () => {
  const callsign = prompt("Callsign for the new device (e.g. VIPER-2):");
  if (!callsign) return;
  const host = await resolveServerHost();
  if (!host) { alert("Enrollment needs the server's LAN IP. Try again."); return; }
  const resp = await fetch("/api/enroll/tokens", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callsign, username: callsign, expires_hours: 24 }),
  });
  if (!resp.ok) {
    alert("Failed: " + JSON.stringify(await resp.json()));
    return;
  }
  const t = await resp.json();
  showEnrollPanel(t, callsign, host);
};

// ----------------------------------------------------- web users (admin only)

const usersBtn = document.getElementById("users-btn");
if (usersBtn) usersBtn.onclick = showUsersPanel;

const devicesBtn = document.getElementById("devices-btn");
if (devicesBtn) devicesBtn.onclick = showDevicesPanel;

async function showUsersPanel() {
  const ov = document.createElement("div");
  ov.id = "users-overlay";
  ov.innerHTML = `
    <div class="users-card">
      <h3>Web logins</h3>
      <p class="users-sub">People who can sign in to this page. Only
        <b>admins</b> can enroll devices and manage users; viewer/operator
        logins can watch the map but <b>cannot enroll devices</b>.</p>
      <div id="users-list">Loading…</div>
      <form id="user-add" autocomplete="off">
        <h4>Add a login</h4>
        <div class="user-add-row">
          <input id="nu-name" placeholder="username" required>
          <input id="nu-pass" type="password" placeholder="password" required>
          <select id="nu-role">
            <option value="viewer">viewer — view only</option>
            <option value="operator">operator — + chat &amp; cameras</option>
            <option value="admin">admin — full control</option>
          </select>
          <button type="submit">Add login</button>
        </div>
      </form>
      <p class="user-err" id="user-err"></p>
      <button id="users-close">Close</button>
    </div>`;
  document.body.appendChild(ov);
  const listEl = ov.querySelector("#users-list");
  const errEl = ov.querySelector("#user-err");

  function rowHtml(u) {
    const self = u.username === (me && me.username);
    const opts = ["viewer", "operator", "admin"].map((r) =>
      `<option value="${r}"${r === u.role ? " selected" : ""}>${r}</option>`).join("");
    return `<tr>
      <td>${escapeHtml(u.username)}${self ? ' <span class="you">you</span>' : ""}</td>
      <td><select data-user="${escapeHtml(u.username)}"${self ? " disabled title='You can\\'t change your own role'" : ""}>${opts}</select></td>
      <td>${self ? "" : `<button class="user-del" data-del="${escapeHtml(u.username)}" title="Delete login">🗑</button>`}</td>
    </tr>`;
  }

  async function refresh() {
    let users = [];
    try { users = await (await fetch("/api/users")).json(); } catch { /* */ }
    if (!Array.isArray(users)) { listEl.textContent = "Failed to load users."; return; }
    listEl.innerHTML = `<table class="users-table">
      <thead><tr><th>Username</th><th>Role</th><th></th></tr></thead>
      <tbody>${users.map(rowHtml).join("")}</tbody></table>`;
    listEl.querySelectorAll("select[data-user]").forEach((sel) => {
      sel.onchange = () => changeRole(sel.dataset.user, sel.value);
    });
    listEl.querySelectorAll("button[data-del]").forEach((btn) => {
      btn.onclick = () => delUser(btn.dataset.del);
    });
  }

  async function changeRole(username, role) {
    errEl.textContent = "";
    const r = await fetch("/api/users/role", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, role }) });
    if (!r.ok) errEl.textContent = ((await r.json()).error) || "role change failed";
    refresh();
  }

  async function delUser(username) {
    errEl.textContent = "";
    if (!confirm(`Delete the login "${username}"? They will no longer be able to sign in.`)) return;
    const r = await fetch("/api/users?username=" + encodeURIComponent(username),
                          { method: "DELETE" });
    if (!r.ok) errEl.textContent = ((await r.json()).error) || "delete failed";
    refresh();
  }

  ov.querySelector("#user-add").onsubmit = async (e) => {
    e.preventDefault();
    errEl.textContent = "";
    const username = ov.querySelector("#nu-name").value.trim();
    const password = ov.querySelector("#nu-pass").value;
    const role = ov.querySelector("#nu-role").value;
    if (!username || !password) return;
    const r = await fetch("/api/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }) });
    if (!r.ok) { errEl.textContent = ((await r.json()).error) || "add failed"; return; }
    ov.querySelector("#nu-name").value = "";
    ov.querySelector("#nu-pass").value = "";
    refresh();
  };

  ov.querySelector("#users-close").onclick = () => ov.remove();
  refresh();
}

// ------------------------------------------------------------ devices admin

function fmtAge(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

async function showDevicesPanel() {
  const ov = document.createElement("div");
  ov.id = "devices-overlay";
  ov.innerHTML = `
    <div class="devices-card">
      <h3>Connected devices</h3>
      <p class="devices-sub">Every device the server has seen. Removing a device
        clears it from the map and roster and deletes its track history — it
        returns only if it sends data again. <b>Admin only.</b></p>
      <div class="devices-toolbar">
        <span id="devices-count" class="devices-count"></span>
        <button id="devices-refresh" type="button">↻ Refresh</button>
        <button id="devices-clear" type="button" class="danger">🗑 Remove all</button>
      </div>
      <div class="devices-table-wrap"><div id="devices-list">Loading…</div></div>
      <p class="user-err" id="devices-err"></p>
      <button id="devices-close">Close</button>
    </div>`;
  document.body.appendChild(ov);
  const listEl = ov.querySelector("#devices-list");
  const errEl = ov.querySelector("#devices-err");
  const countEl = ov.querySelector("#devices-count");

  function rowHtml(d) {
    const name = d.callsign || d.uid || "?";
    const pos = (d.lat != null && d.lon != null)
      ? `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}` : "—";
    const batt = (d.battery != null && d.battery !== "") ? d.battery + "%" : "—";
    return `<tr>
      <td>${escapeHtml(name)}</td>
      <td class="mono uid" title="${escapeHtml(d.uid || "")}">${escapeHtml(d.uid || "")}</td>
      <td>${escapeHtml(d.team || "—")}</td>
      <td>${escapeHtml(d.role || "—")}</td>
      <td>${escapeHtml(d.platform || d.device || "—")}</td>
      <td>${escapeHtml(batt)}</td>
      <td>${escapeHtml(fmtAge(d.last_seen))}</td>
      <td class="mono">${escapeHtml(pos)}</td>
      <td><button class="dev-del" data-del="${escapeHtml(d.uid || "")}"
          title="Remove this device">🗑</button></td>
    </tr>`;
  }

  async function refresh() {
    let rows = [];
    try { rows = await (await fetch("/api/devices", { cache: "no-store" })).json(); }
    catch { /* */ }
    if (!Array.isArray(rows)) { listEl.textContent = "Failed to load devices."; return; }
    countEl.textContent = `${rows.length} device${rows.length === 1 ? "" : "s"}`;
    if (!rows.length) {
      listEl.innerHTML = `<p class="devices-empty">No devices connected.</p>`;
      return;
    }
    listEl.innerHTML = `<table class="devices-table">
      <thead><tr><th>Callsign</th><th>UID</th><th>Team</th><th>Role</th>
        <th>Platform</th><th>Battery</th><th>Last seen</th><th>Position</th>
        <th></th></tr></thead>
      <tbody>${rows.map(rowHtml).join("")}</tbody></table>`;
    listEl.querySelectorAll("button[data-del]").forEach((btn) => {
      btn.onclick = () => delOne(btn.dataset.del);
    });
  }

  async function delOne(uid) {
    errEl.textContent = "";
    if (!confirm(`Remove device "${uid}" from the map and delete its history?`)) return;
    const r = await fetch("/api/devices?uid=" + encodeURIComponent(uid),
                          { method: "DELETE" });
    if (!r.ok) errEl.textContent = ((await r.json()).error) || "remove failed";
    refresh();
  }

  async function delAll() {
    errEl.textContent = "";
    const n = devices.size;
    if (!confirm(`Remove ALL ${n} device${n === 1 ? "" : "s"} from the map and `
      + `delete their track history? They reappear only if they send data again.`))
      return;
    const r = await fetch("/api/devices?all=1", { method: "DELETE" });
    if (!r.ok) errEl.textContent = ((await r.json()).error) || "remove-all failed";
    refresh();
  }

  ov.querySelector("#devices-refresh").onclick = refresh;
  ov.querySelector("#devices-clear").onclick = delAll;
  const close = () => { devicesPanelRefresh = null; ov.remove(); };
  ov.querySelector("#devices-close").onclick = close;
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  devicesPanelRefresh = refresh;   // let SSE removals refresh this table live
  refresh();
}

function showEnrollPanel(t, callsign, host) {
  const httpPort = location.port || "8080";
  // ATAK onboarding, the proven-reliable path: the QR encodes the softcert
  // package DOWNLOAD url (not a tak://import). Scanned with the phone Camera it
  // makes the browser download TAK-Revamp_Connect.zip; the user imports it via
  // ATAK's Import Manager, where ATAK trusts and PERSISTS the bundled client
  // cert. Why not the "automatic" routes? On ATAK v5.6 here: a tak://import of a
  // softcert connects once then discards the URL-installed cert (won't persist);
  // a mode=enroll package never even added the server. A hand-picked file is the
  // only thing ATAK keeps. So: scan → download → Import Manager → tap the file →
  // connected & saved, no typing. (Token in the url only authorizes the
  // download; it is not consumed, so the QR can be re-scanned.)
  const pkgUrl = `http://${host}:${httpPort}/enroll.zip` +
    `?callsign=${encodeURIComponent(callsign)}` +
    `&username=${encodeURIComponent(t.username)}` +
    `&token=${encodeURIComponent(t.token)}`;
  const atakImportUrl = pkgUrl;  // plain download; the Camera opens it in a browser
  const itakText = `TAK-Revamp,${host},8089,ssl`;

  const ov = document.createElement("div");
  ov.id = "enroll-overlay";
  ov.innerHTML = `
    <div class="enroll-card">
      <h3>Enroll ${escapeHtml(callsign)}</h3>
      <p class="enroll-server">Server: <code>${escapeHtml(host)}:8089</code>
        &nbsp;·&nbsp; iTAK/manual credentials if asked:
        user <code>${escapeHtml(t.username)}</code>
        pass <code>${escapeHtml(t.token)}</code></p>
      <div class="qr-row">
        <div class="qr-block">
          <h4>ATAK (Android)</h4>
          <div class="qr" id="qr-atak"></div>
          <p>Scan with the phone <b>Camera</b> (not ATAK's scanner) → your
             browser downloads <b>TAK-Revamp_Connect.zip</b>. Then in ATAK:
             <b>☰ → Import Manager → Local SD → Download →</b> tap that file.
             It connects and <b>saves</b> the server — no typing.</p>
        </div>
        <div class="qr-block">
          <h4>iTAK ① — trust (first time only)</h4>
          <div class="qr" id="qr-ios-trust"></div>
          <p>Scan with the iPhone <b>camera</b> → install the profile →
             Settings → General → About →
             <b>Certificate Trust Settings</b> → enable the TAK-Revamp CA.
             Skip if this phone already did it.</p>
        </div>
        <div class="qr-block">
          <h4>iTAK ② — connect</h4>
          <div class="qr" id="qr-itak"></div>
          <p>iTAK → Settings → Servers → ⊕ → scan, then enter the
             credentials above when prompted.</p>
        </div>
      </div>
      <p class="enroll-note">
        Token valid 24 h. The ATAK package carries the CA, so no truststore
        copying is needed. Manual fallback:
        <a href="/truststore.p12" download>download truststore.p12</a>
        (password <code>atakatak</code>) and add the server by hand.
      </p>
      <button id="enroll-close">Close</button>
    </div>`;
  document.body.appendChild(ov);
  new QRCode(document.getElementById("qr-atak"),
    { text: atakImportUrl, width: 190, height: 190, correctLevel: QRCode.CorrectLevel.L });
  new QRCode(document.getElementById("qr-ios-trust"),
    { text: `http://${host}:${httpPort}/ca.mobileconfig`,
      width: 190, height: 190, correctLevel: QRCode.CorrectLevel.M });
  new QRCode(document.getElementById("qr-itak"),
    { text: itakText, width: 190, height: 190, correctLevel: QRCode.CorrectLevel.M });
  ov.querySelector("#enroll-close").onclick = () => ov.remove();
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
}

document.getElementById("add-camera").onclick = async () => {
  const name = prompt("Camera name (e.g. gate-north):");
  if (!name) return;
  const source = prompt("RTSP/RTMP source URL (e.g. rtsp://user:pass@10.0.0.5:554/stream):");
  if (!source) return;
  const place = confirm("Place this camera on the map at the current map center?");
  const body = { name, source };
  if (place) {
    const c = map.getCenter();
    body.lat = c.lat; body.lon = c.lng;
  }
  const resp = await fetch("/api/streams", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (resp.ok) { await pollStreams(); alert(`Camera registered (${(await resp.json()).path})`); }
  else alert("Failed: " + (await resp.text()));
};
