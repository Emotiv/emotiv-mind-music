// Frontend. Talks to Python through window.pywebview.api.* and receives backend
// events through window.pushEvent(), which the engine calls via evaluate_js.
//
// The device, sensor-quality and training code is carried over from
// emotiv-brain-light, where it was built and checked against Cortex. What is
// new here is everything about music: the setup guide, now playing, and the
// controls card that says which thought does what.

const STEPS = ["credentials", "cortex", "access", "headset", "session", "profile", "stream", "spotify"];

const PLAYER_ACTIONS = ["none", "play_pause", "play", "pause", "next", "previous",
  "volume_up", "volume_down", "shuffle"];

const state = {
  settings: {},
  running: false,
  redirectUri: "",
  triggerDefaults: { threshold: 0.6, hold: 0.5, cooldown: 2.0 },

  // Cortex
  profiles: [],
  headsets: [],
  selectedHeadset: "",
  loadedProfile: "",
  actionColors: {},
  sensitivity: [],
  steps: {},
  commands: { enabled: [], disabled: [], trained: {}, available: [], max_active: 4 },
  quality: {},
  qualityView: "cq",
  trainingResult: null,
  training: null, // { action, phase, started, score, threshold }

  // Music
  spotify: { signed_in: false, client_id_set: false, now_playing: null, devices: [], preferred_device: "",
    error: "", error_params: {} },
  bindings: {},
  bindingCommands: [],
  mindControl: true,
  live: null,            // latest `live` event
  lastFired: null,       // { command, action, ok, code, params, at }

  // Folding: a card auto-folds once when its choice is made, then it is the
  // user's to open and close.
  changingProfile: false,
  autoFoldedFor: "",
};

// Contact quality and EEG quality share one 0-4 grading per sensor.
const QUALITY_COLORS = ["#4a4f5a", "#ff5468", "#f2974e", "#e9cc40", "#50e17d"];

// Below this overall quality, detections are more noise than intent.
const POOR_QUALITY = 50;

const TRAINING_SECONDS = 8;

// Electrode positions on a head seen from above, nose up, on a unit circle.
const SENSOR_POSITIONS = {
  Nz: [0, -1], Fp1: [-0.309, -0.951], Fp2: [0.309, -0.951],
  AF7: [-0.55, -0.75], AF3: [-0.34, -0.76], AF4: [0.34, -0.76], AF8: [0.55, -0.75],
  F7: [-0.809, -0.587], F3: [-0.4, -0.52], Fz: [0, -0.5], F4: [0.4, -0.52], F8: [0.809, -0.587],
  FC5: [-0.63, -0.28], FC1: [-0.22, -0.26], FC2: [0.22, -0.26], FC6: [0.63, -0.28],
  T7: [-0.98, 0], C3: [-0.5, 0], Cz: [0, 0], C4: [0.5, 0], T8: [0.98, 0],
  T9: [-1.0, 0.18], T10: [1.0, 0.18],
  CP5: [-0.63, 0.28], CP1: [-0.22, 0.26], CP2: [0.22, 0.26], CP6: [0.63, 0.28],
  P7: [-0.809, 0.587], P3: [-0.4, 0.52], Pz: [0, 0.5], P4: [0.4, 0.52], P8: [0.809, 0.587],
  PO3: [-0.34, 0.76], PO4: [0.34, 0.76],
  O1: [-0.309, 0.951], Oz: [0, 1], O2: [0.309, 0.951],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const api = () => window.pywebview.api;

// ─── i18n for static elements ─────────────────────────────────────────────
function applyStaticI18n() {
  $$("[data-i18n]").forEach((el) => (el.textContent = t(el.getAttribute("data-i18n"))));
  $$(".lang-chip").forEach((b) => b.classList.toggle("active", b.dataset.lang === LANG));
  renderAll();
}

function renderAll() {
  renderRunButton();
  renderRunningPill();
  renderSteps();
  renderSetup();
  renderHeadsets();
  renderProfiles();
  renderQuality();
  renderNowPlaying();
  renderMindSwitch();
  renderBindings();
  renderLastAction();
  renderTuning();
  renderSensitivity();
  renderCommands();
  renderBrainmap();
  renderTrainingCard();
  renderSummaries();
}

// ─── Buttons / status ─────────────────────────────────────────────────────
function renderRunButton() {
  const btn = $("#btn-run");
  btn.textContent = state.running ? t("btn.stop") : t("btn.start");
  btn.classList.toggle("stop", state.running);
}

function renderRunningPill() {
  const pill = $("#running-pill");
  pill.textContent = state.running ? t("running.yes") : t("running.no");
  pill.classList.toggle("on", state.running);
  $("#brand-dot").classList.toggle("live", state.running);
}

function renderSteps() {
  const host = $("#steps");
  host.innerHTML = "";
  for (const step of STEPS) {
    const info = state.steps[step] || { state: "idle" };
    const el = document.createElement("div");
    el.className = "step " + (info.state || "idle");
    const dot = document.createElement("div");
    dot.className = "step-dot";
    const body = document.createElement("div");
    const name = document.createElement("div");
    name.className = "step-name";
    name.textContent = t("step." + step);
    body.appendChild(name);
    if (info.code) {
      const msg = document.createElement("div");
      msg.className = "step-msg";
      msg.textContent = t(info.code, info.params);
      body.appendChild(msg);
    }
    el.appendChild(dot);
    el.appendChild(body);
    host.appendChild(el);
  }
}

// ─── Log ──────────────────────────────────────────────────────────────────
const logEntries = [];

function addLog(level, code, params) {
  logEntries.push({ level, code, params, time: new Date() });
  if (logEntries.length > 300) logEntries.shift();
  renderLog();
}

function renderLog() {
  const host = $("#log");
  if (!logEntries.length) {
    host.innerHTML = "";
    const line = document.createElement("div");
    line.className = "log-line info";
    const text = document.createElement("span");
    text.className = "log-text";
    text.textContent = t("log.empty");
    line.appendChild(text);
    host.appendChild(line);
    return;
  }
  const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 40;
  host.innerHTML = "";
  for (const e of logEntries) {
    const line = document.createElement("div");
    line.className = "log-line " + e.level;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = e.time.toTimeString().slice(0, 8);
    const text = document.createElement("span");
    text.className = "log-text";
    text.textContent = t(e.code, localiseParams(e.params));
    line.appendChild(time);
    line.appendChild(text);
    host.appendChild(line);
  }
  if (atBottom) host.scrollTop = host.scrollHeight;
}

/** Log parameters that name a command or a player action read in the user's language. */
function localiseParams(params) {
  if (!params) return params;
  const out = { ...params };
  if (out.command) out.command = actionLabel(out.command);
  if (out.action && PLAYER_ACTIONS.includes(out.action)) out.action = playerLabel(out.action);
  else if (out.action) out.action = actionLabel(out.action);
  return out;
}

// ─── Getting started ──────────────────────────────────────────────────────
// Four things stand between a new user and music that follows their mind. The
// card lists them in the order they have to happen, ticks each one off as it
// becomes true, and disappears once nothing is left — it is a guide, not a
// dashboard, so it has no reason to stay.

function setupProgress() {
  const s = state.settings;
  return {
    cortex: !!(s.client_id && s.client_secret_set),
    spotify: !!state.spotify.signed_in,
    headset: !!state.selectedHeadset,
    commands: state.commands.enabled.length > 0 &&
      state.commands.enabled.some((c) => (state.commands.trained[c] || 0) > 0),
  };
}

function renderSetup() {
  const done = setupProgress();
  const keys = ["cortex", "spotify", "headset", "commands"];
  const count = keys.filter((k) => done[k]).length;
  // The first step not yet done is the one worth pointing at.
  const next = keys.find((k) => !done[k]);

  $("#setup-card").classList.toggle("hidden", count === keys.length);
  $("#setup-progress").textContent = t("setup_guide.progress", { done: count, total: keys.length });
  for (const key of keys) {
    const el = $("#setup-" + key);
    el.classList.toggle("done", done[key]);
    el.classList.toggle("next", key === next);
  }
}

// ─── Devices: headsets and profiles ───────────────────────────────────────
function pickItem({ id, label, tag, tagClass, selected, onClick }) {
  const btn = document.createElement("button");
  btn.className = "pick-item" + (selected ? " selected" : "");
  btn.dataset.id = id;
  const radio = document.createElement("i");
  radio.className = "pick-radio";
  const main = document.createElement("span");
  main.className = "pick-main";
  main.textContent = label;
  btn.appendChild(radio);
  btn.appendChild(main);
  if (tag) {
    const tagEl = document.createElement("span");
    tagEl.className = "pick-tag " + (tagClass || "");
    tagEl.textContent = tag;
    btn.appendChild(tagEl);
  }
  btn.addEventListener("click", () => onClick(btn));
  return btn;
}

function renderHeadsets() {
  const host = $("#headset-list");
  host.innerHTML = "";
  host.classList.toggle("scroll", state.headsets.length > 4);
  const empty = $("#headset-empty");
  empty.classList.toggle("hidden", state.headsets.length > 0);
  empty.textContent = state.running ? t("panel.no_headsets") : t("panel.press_start");

  for (const h of state.headsets) {
    const connected = h.status === "connected";
    host.appendChild(
      pickItem({
        id: h.id,
        label: h.id,
        tag: h.virtual ? t("tag.virtual")
          : connected ? t("tag.connected")
          : tHas("headset.status." + h.status) ? t("headset.status." + h.status) : h.status,
        tagClass: h.virtual ? "virtual" : connected ? "connected" : "",
        selected: h.id === state.selectedHeadset,
        onClick: async () => {
          if (!state.running) return addLog("error", "err.not_running", {});
          $$("#headset-list .pick-item").forEach((b) => (b.disabled = true));
          const res = await api().select_headset(h.id);
          $$("#headset-list .pick-item").forEach((b) => (b.disabled = false));
          if (res && res.ok === false) addLog("error", res.code, res.params || {});
        },
      })
    );
  }
}

function renderProfiles() {
  const host = $("#profile-list");
  host.innerHTML = "";
  host.classList.toggle("scroll", state.profiles.length > 4);

  const ready = !!state.selectedHeadset;
  const empty = $("#profile-empty");
  empty.classList.toggle("hidden", ready && state.profiles.length > 0);
  empty.textContent = ready ? t("setup.no_profiles") : t("panel.connect_first");
  $("#btn-new-profile-devices").classList.toggle("hidden", !ready || !state.running);
  if (!ready) {
    $("#btn-change-profile").classList.add("hidden");
    return;
  }

  // Once a profile is loaded the list has done its job: show the answer and
  // keep the rest behind a button.
  const settled = state.loadedProfile && !state.changingProfile;
  $("#btn-change-profile").classList.toggle("hidden", !settled);
  const listed = settled ? [state.loadedProfile] : state.profiles;

  for (const name of listed) {
    host.appendChild(
      pickItem({
        id: name,
        label: name,
        tag: name === state.loadedProfile ? t("tag.loaded") : "",
        tagClass: "connected",
        selected: name === state.loadedProfile,
        onClick: async () => {
          if (!state.running) return addLog("error", "err.not_running", {});
          $$("#profile-list .pick-item").forEach((b) => (b.disabled = true));
          const res = await api().select_profile(name);
          $$("#profile-list .pick-item").forEach((b) => (b.disabled = false));
          if (res && res.ok === false) addLog("error", res.code, res.params || {});
        },
      })
    );
  }
}

// ─── Sensor quality ───────────────────────────────────────────────────────
// Contact quality asks whether each electrode touches skin well enough to read
// anything; EEG quality asks whether what arrives is brain signal rather than
// jaw, movement or mains hum. A command detected on a bad signal is a coin
// toss, so both sit next to the headset — and the controls card warns too.

function qualityColor(grade) {
  return QUALITY_COLORS[Math.max(0, Math.min(4, Math.round(grade || 0)))];
}

function qualityGrade(percent) {
  return percent >= 80 ? "good" : percent >= POOR_QUALITY ? "fair" : "poor";
}

function overallQuality() {
  const q = state.quality || {};
  const both = [q.cq_overall, q.eq_overall].filter((v) => typeof v === "number");
  return both.length ? Math.min(...both) : null;
}

function renderQuality() {
  const q = state.quality || {};
  const has = typeof q.cq_overall === "number" || typeof q.eq_overall === "number";
  $("#quality-block").classList.toggle("hidden", !state.selectedHeadset);

  const shown = state.qualityView === "eq" ? q.eq_overall : q.cq_overall;
  $("#q-big").textContent = typeof shown === "number" ? Math.round(shown) + "%" : "—";
  $("#q-big").style.color = typeof shown === "number"
    ? qualityColor((Math.max(0, Math.min(100, shown)) / 100) * 4) : "";

  drawHeadmap($("#headmap"), q[state.qualityView] || {}, { radius: 62, cx: 80, cy: 92, dot: 7, labels: true });

  const verdict = $("#q-verdict");
  const help = $("#q-help");
  const worst = overallQuality();
  if (!has || worst === null) {
    verdict.textContent = t("quality.waiting");
    verdict.className = "small muted";
    help.textContent = "";
  } else {
    const grade = qualityGrade(worst);
    verdict.textContent = t("quality.verdict." + grade);
    verdict.className = "small " + grade;
    // Say what to do about it, not just that it is bad.
    help.textContent = grade === "good" ? "" : t("quality.help." + state.qualityView);
  }
  renderDevicePill();
  renderControlsWarnings();
}

function renderDevicePill() {
  const pill = $("#device-pill");
  pill.classList.toggle("hidden", !state.running || !state.selectedHeadset);
  if (!state.selectedHeadset) return;
  $("#pill-name").textContent = state.selectedHeadset;
  drawHeadmap($("#pill-head"), (state.quality || {}).cq || {}, { radius: 17, cx: 22, cy: 26, dot: 3, labels: false });
  const badge = $("#pill-quality");
  const worst = overallQuality();
  badge.textContent = worst === null ? "—" : Math.round(worst);
  badge.className = "quality-badge" + (worst === null ? "" : " " + qualityGrade(worst));
}

/** Head seen from above, nose up — so left and right match the wearer's own head. */
function drawHeadmap(svg, grades, opts) {
  if (!svg) return;
  const { radius: r, cx, cy, dot, labels } = opts;
  const names = Object.keys(grades);
  const noseHalf = Math.PI / 18;
  const bx = r * Math.sin(noseHalf);
  const by = r * Math.cos(noseHalf);

  let out =
    '<circle class="head-outline" cx="' + cx + '" cy="' + cy + '" r="' + r + '" />' +
    '<path class="head-nose" d="M ' + (cx - bx) + " " + (cy - by) +
    " L " + cx + " " + (cy - r - r * 0.15) + " L " + (cx + bx) + " " + (cy - by) + '" />';

  names.forEach((name, i) => {
    let pos = SENSOR_POSITIONS[name];
    if (!pos) {
      const angle = (i / Math.max(names.length, 1)) * Math.PI * 2;
      pos = [Math.sin(angle) * 0.8, -Math.cos(angle) * 0.8];
    }
    const x = cx + pos[0] * r * 0.82;
    const y = cy + pos[1] * r * 0.82;
    const grade = Math.max(0, Math.min(4, Math.round(grades[name] || 0)));
    out += '<circle class="sensor" cx="' + x + '" cy="' + y + '" r="' + dot + '" fill="' + qualityColor(grade) + '">';
    if (labels) out += "<title>" + escapeText(name + " — " + t("quality.grade." + grade)) + "</title>";
    out += "</circle>";
    if (labels) {
      out += '<text class="sensor-label" x="' + x + '" y="' + (y + dot + 8) + '">' + escapeText(name) + "</text>";
    }
  });
  svg.innerHTML = out;
}

// ─── Now playing ──────────────────────────────────────────────────────────
function renderNowPlaying() {
  const sp = state.spotify;
  const now = sp.now_playing;
  const signedIn = !!sp.signed_in;

  $("#now-connect").classList.toggle("hidden", signedIn);
  $("#now-art").classList.toggle("hidden", !signedIn);
  $(".now-info").classList.toggle("hidden", !signedIn);

  if (!signedIn) {
    // Never a disabled button here: without a Client ID it opens the setup
    // instructions instead (connectSpotify decides), which is the next step.
    $("#now-connect-text").textContent = sp.client_id_set
      ? t("now.connect_hint") : t("now.setup_hint");
    return;
  }

  const art = $("#now-art");
  const track = $("#now-track");
  const artist = $("#now-artist");
  const label = $("#now-label");
  const open = $("#now-open");
  const progress = $("#now-progress");

  if (sp.error) {
    label.textContent = t("now.problem");
    track.textContent = t(sp.error, sp.error_params);
    track.classList.add("small-track");
  } else if (!now || !now.track) {
    label.textContent = t("now.idle");
    track.textContent = t("now.idle_hint");
    track.classList.add("small-track");
  } else {
    label.textContent = now.is_playing ? t("now.playing") : t("now.paused");
    track.textContent = now.track;
    track.classList.remove("small-track");
  }
  artist.textContent = now && now.track && !sp.error ? [now.artists, now.album].filter(Boolean).join(" · ") : "";

  const image = now && now.image && !sp.error ? now.image : "";
  if (art.dataset.image !== image) {
    art.dataset.image = image;
    art.style.backgroundImage = image ? 'url("' + image.replace(/"/g, "") + '")' : "";
    art.classList.toggle("has-image", !!image);
  }
  art.classList.toggle("playing", !!(now && now.is_playing && !sp.error));

  // Spotify's design guidelines: metadata always links back to Spotify.
  open.classList.toggle("hidden", !(now && now.url && !sp.error));
  open.dataset.url = now && now.url ? now.url : "";

  renderDevicePicker();
  $("#t-volume").textContent = now && typeof now.volume === "number" && !sp.error ? now.volume + "%" : "";
  $("#t-shuffle").classList.toggle("on", !!(now && now.shuffle));

  const playing = !!(now && now.is_playing && !sp.error);
  $("#t-play-icon").innerHTML = playing
    ? '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" class="fill"/>'
    : '<path d="M8 5l11 7-11 7z" class="fill"/>';
  $("#t-play").title = playerLabel(playing ? "pause" : "play");

  const hasProgress = now && now.duration_ms > 0 && !sp.error;
  progress.classList.toggle("hidden", !hasProgress);
  if (hasProgress) {
    $("#now-progress-fill").style.width = Math.min(100, (100 * now.progress_ms) / now.duration_ms).toFixed(1) + "%";
  }

  $$(".tbtn").forEach((b) => (b.title = b.title || playerLabel(b.dataset.player)));
}

// ─── Where the music plays ────────────────────────────────────────────────
// Spotify only lists devices that have had Spotify open recently, and the list
// is the first place anyone looks when their speaker "is missing" — so an empty
// or short list always comes with the reason.

function deviceLabel(device) {
  const type = tHas("device.type." + device.type) ? t("device.type." + device.type) : "";
  const name = type ? device.name + " · " + type : device.name;
  return device.is_restricted ? t("now.device_restricted", { device: name }) : name;
}

function renderDevicePicker() {
  const select = $("#now-device");
  if (!select || select.classList.contains("busy")) return;
  // Rebuilding the options while the list is open closes it under the pointer.
  if (document.activeElement === select) return;

  const sp = state.spotify;
  const devices = sp.devices || [];
  const active = devices.find((d) => d.is_active);
  const preferred = devices.find((d) => d.id === sp.preferred_device);

  select.innerHTML = "";
  if (!devices.length) {
    const none = document.createElement("option");
    none.value = "";
    none.textContent = t("now.no_devices");
    select.appendChild(none);
  } else if (!active) {
    // Nothing is playing anywhere. Say what a choice will do rather than
    // pretending one is already made.
    const pick = document.createElement("option");
    pick.value = "";
    pick.textContent = preferred ? t("now.device_preferred", { device: preferred.name }) : t("now.choose_device");
    select.appendChild(pick);
  }
  for (const device of devices) {
    const opt = document.createElement("option");
    opt.value = device.id;
    opt.textContent = deviceLabel(device);
    opt.disabled = device.is_restricted;
    select.appendChild(opt);
  }
  select.value = active ? active.id : "";
  $(".device-picker").classList.toggle("active", !!active);
  // Shown until there is more than one place to choose from.
  $("#now-device-hint").classList.toggle("hidden", devices.filter((d) => !d.is_restricted).length > 1);
}

let lastDeviceRefresh = 0;
async function refreshDevices() {
  // Opening the list is the moment someone looks for a device they just woke
  // up; re-read it then, but not on every flick of the pointer.
  if (Date.now() - lastDeviceRefresh < 3000) return;
  lastDeviceRefresh = Date.now();
  const res = await api().spotify_devices();
  if (res && res.ok === false) addLog("error", res.code, res.params || {});
}

async function playOn(deviceId) {
  const select = $("#now-device");
  if (!deviceId) return renderDevicePicker();
  select.classList.add("busy");
  select.disabled = true;
  try {
    const res = await api().spotify_play_on(deviceId);
    if (res && res.ok === false) addLog("error", res.code, res.params || {});
  } finally {
    select.classList.remove("busy");
    select.disabled = false;
    select.blur();
    renderDevicePicker();
  }
}

async function playerClick(action, button) {
  button.disabled = true;
  button.classList.add("pressed");
  try {
    const res = await api().player_action(action);
    if (res && res.ok === false) addLog("error", res.code, res.params || {});
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.classList.remove("pressed");
    }, 250);
  }
}

// ─── What your mind controls ──────────────────────────────────────────────
// The centre of the app. One row per trained command, and each row answers the
// three questions a user actually has, left to right: which thought is this,
// how close is it to firing right now, and what will it do when it does.

function renderMindSwitch() {
  const on = state.mindControl;
  $("#mind-toggle").classList.toggle("on", on);
  $("#mind-toggle").setAttribute("aria-checked", on ? "true" : "false");
  $("#mind-switch-label").textContent = on ? t("controls.mind_on") : t("controls.mind_off");
  $("#controls-card").classList.toggle("disarmed", !on);
}

function renderBindings() {
  const host = $("#binding-list");
  host.innerHTML = "";
  const commands = state.bindingCommands;
  const empty = $("#controls-empty");

  if (!commands.length) {
    empty.classList.remove("hidden");
    empty.textContent = !state.running ? t("controls.empty.start")
      : !state.selectedHeadset ? t("controls.empty.headset")
      : !state.loadedProfile ? t("controls.empty.profile")
      : t("controls.empty.train");
    renderControlsWarnings();
    return;
  }
  empty.classList.add("hidden");

  // Header row, so the three columns explain themselves.
  const head = document.createElement("div");
  head.className = "bind-row bind-head";
  for (const key of ["controls.col.thought", "controls.col.strength", "controls.col.does"]) {
    const cell = document.createElement("div");
    cell.textContent = t(key);
    head.appendChild(cell);
  }
  host.appendChild(head);

  for (const command of commands) host.appendChild(bindingRow(command));
  renderControlsWarnings();
}

/** Whether a command has training behind it.
 *
 *  Two sources say so, and either is enough: the roster's count of accepted
 *  recordings, and the brain map, which only places a command away from
 *  Neutral once Cortex has a signature for it. They arrive as separate events,
 *  so asking only one of them left a trained command labelled "Not trained
 *  yet" until the other caught up. */
function isTrained(command) {
  if ((state.commands.trained[command] || 0) > 0) return true;
  const map = (state.trainingResult && state.trainingResult.brain_map) || [];
  const point = map.find((p) => p.action === command);
  if (!point || !Array.isArray(point.coordinates)) return false;
  const [x, y] = point.coordinates;
  return Math.hypot(x || 0, y || 0) > 0;
}

function bindingRow(command) {
  const color = state.actionColors[command] || "#5ab0ee";
  const trained = isTrained(command);
  const enabled = state.commands.enabled.includes(command);

  const row = document.createElement("div");
  row.className = "bind-row" + (enabled ? "" : " off") + (trained ? "" : " untrained");
  row.dataset.command = command;
  row.style.setProperty("--slot", color);

  // Which thought
  const name = document.createElement("div");
  name.className = "bind-name";
  const sw = document.createElement("i");
  sw.className = "swatch";
  const label = document.createElement("span");
  label.textContent = actionLabel(command);
  name.appendChild(sw);
  name.appendChild(label);
  if (!trained || !enabled) {
    const note = document.createElement("span");
    note.className = "bind-note";
    note.textContent = !trained ? t("controls.untrained") : t("controls.disabled");
    name.appendChild(note);
  }

  // How close to firing: strength against the trigger line, and the hold
  // filling underneath once the line is crossed.
  const meter = document.createElement("div");
  meter.className = "bind-meter";
  const track = document.createElement("div");
  track.className = "meter-track";
  const fill = document.createElement("div");
  fill.className = "meter-fill";
  const line = document.createElement("div");
  line.className = "meter-line";
  line.style.left = (thresholdValue() * 100).toFixed(1) + "%";
  line.title = t("controls.threshold_line");
  const hold = document.createElement("div");
  hold.className = "meter-hold";
  track.appendChild(fill);
  track.appendChild(line);
  meter.appendChild(track);
  meter.appendChild(hold);

  // What it does
  const does = document.createElement("div");
  does.className = "bind-does";
  const arrow = document.createElement("span");
  arrow.className = "bind-arrow";
  arrow.textContent = "→";
  const select = document.createElement("select");
  select.className = "bind-select";
  for (const action of PLAYER_ACTIONS) {
    const opt = document.createElement("option");
    opt.value = action;
    opt.textContent = playerLabel(action);
    select.appendChild(opt);
  }
  select.value = state.bindings[command] || "none";
  select.classList.toggle("none", select.value === "none");
  select.addEventListener("change", async () => {
    select.classList.toggle("none", select.value === "none");
    const res = await api().set_binding(command, select.value);
    if (res && res.ok === false) addLog("error", res.code, res.params || {});
  });
  does.appendChild(arrow);
  does.appendChild(select);

  row.appendChild(name);
  row.appendChild(meter);
  row.appendChild(does);
  return row;
}

function thresholdValue() {
  const v = Number(state.settings.trigger_threshold);
  return Number.isFinite(v) ? v : state.triggerDefaults.threshold;
}

/** One `live` sample: move the active row, let the others fall back to rest. */
function applyLive(data) {
  state.live = data;
  const threshold = thresholdValue();
  $$("#binding-list .bind-row[data-command]").forEach((row) => {
    const mine = row.dataset.command === data.action;
    const power = mine ? data.power : 0;
    row.querySelector(".meter-fill").style.width = (power * 100).toFixed(1) + "%";
    row.querySelector(".meter-hold").style.width =
      mine ? (data.hold_progress * 100).toFixed(1) + "%" : "0%";
    row.classList.toggle("active", mine && power >= threshold);
    row.classList.toggle("latched", mine && !!data.latched);
    row.classList.toggle("cooling", !!data.cooling);
  });
}

function onFired(data) {
  state.lastFired = { ...data, at: Date.now() };
  renderLastAction();
  if (!data.command) return; // a click, not a thought: nothing to flash
  const row = $(`#binding-list .bind-row[data-command="${cssEscape(data.command)}"]`);
  if (!row) return;
  row.classList.remove("fired", "failed");
  // Force a reflow so the same row can flash twice in a row.
  void row.offsetWidth;
  row.classList.add(data.ok ? "fired" : "failed");
}

function renderLastAction() {
  const box = $("#last-action");
  const f = state.lastFired;
  box.classList.toggle("hidden", !f);
  if (!f) return;
  const dot = $("#last-dot");
  dot.style.background = f.command ? state.actionColors[f.command] || "var(--fg-dim)" : "var(--fg-dim)";
  box.classList.toggle("failed", !f.ok);

  const text = f.ok
    ? f.command
      ? t("controls.last.thought", { command: actionLabel(f.command), action: playerLabel(f.action) })
      : t("controls.last.click", { action: playerLabel(f.action) })
    : t("controls.last.failed", { action: playerLabel(f.action), reason: t(f.code, f.params) });
  $("#last-action-text").textContent = text;
  renderLastActionWhen();
}

function renderLastActionWhen() {
  const f = state.lastFired;
  if (!f) return;
  const seconds = Math.round((Date.now() - f.at) / 1000);
  $("#last-action-when").textContent = seconds < 2 ? t("time.just_now")
    : seconds < 60 ? t("time.seconds_ago", { n: seconds })
    : t("time.minutes_ago", { n: Math.round(seconds / 60) });
}

function renderControlsWarnings() {
  const quality = $("#controls-quality-warning");
  const worst = overallQuality();
  const poor = state.bindingCommands.length > 0 && worst !== null && worst < POOR_QUALITY;
  quality.classList.toggle("hidden", !poor);
  if (poor) quality.textContent = t("controls.warn.quality", { percent: Math.round(worst) });

  const spotify = $("#controls-spotify-warning");
  const needsSpotify = state.bindingCommands.length > 0 && !state.spotify.signed_in;
  spotify.classList.toggle("hidden", !needsSpotify);
  if (needsSpotify) spotify.textContent = t("controls.warn.spotify");
}

// ─── Fine-tuning: when a thought counts ───────────────────────────────────
function renderTuning() {
  const s = state.settings;
  const d = state.triggerDefaults;
  const pick = (v, fallback) => (Number.isFinite(Number(v)) && v !== null && v !== "" ? Number(v) : fallback);
  $("#in-threshold").value = pick(s.trigger_threshold, d.threshold);
  $("#in-hold").value = pick(s.trigger_hold, d.hold);
  $("#in-cooldown").value = pick(s.trigger_cooldown, d.cooldown);
  syncTuningLabels();
}

function syncTuningLabels() {
  $("#lbl-threshold").textContent = Math.round(Number($("#in-threshold").value) * 100) + "%";
  $("#lbl-hold").textContent = t("unit.seconds", { n: Number($("#in-hold").value).toFixed(2).replace(/0$/, "") });
  $("#lbl-cooldown").textContent = t("unit.seconds", { n: Number($("#in-cooldown").value).toFixed(1) });
}

async function commitTuning() {
  state.settings = await api().save_settings({
    trigger_threshold: Number($("#in-threshold").value),
    trigger_hold: Number($("#in-hold").value),
    trigger_cooldown: Number($("#in-cooldown").value),
  });
  // The trigger line on every meter moves with the threshold.
  renderBindings();
}

// Cortex keeps one sensitivity per trainable command, 1 to 10, aligned with
// the active order and skipping neutral.
function renderSensitivity() {
  const host = $("#sensitivity-list");
  const block = $("#sensitivity-block");
  host.innerHTML = "";
  const actions = state.commands.enabled;
  block.classList.toggle("hidden", actions.length === 0);
  if (!actions.length) return;

  actions.forEach((action, i) => {
    const colour = state.actionColors[action] || "#666";
    const value = state.sensitivity[i] ?? 5;
    const row = document.createElement("div");
    row.className = "sens-row";
    const name = document.createElement("div");
    name.className = "sens-name";
    const sw = document.createElement("i");
    sw.className = "swatch";
    sw.style.background = colour;
    const label = document.createElement("span");
    label.textContent = actionLabel(action);
    name.appendChild(sw);
    name.appendChild(label);
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "1";
    slider.max = "10";
    slider.step = "1";
    slider.value = String(value);
    slider.style.accentColor = colour;
    const readout = document.createElement("div");
    readout.className = "sens-value";
    readout.textContent = String(value);
    // Talk to Cortex on release only: a request per pixel floods the session.
    slider.addEventListener("input", () => (readout.textContent = slider.value));
    slider.addEventListener("change", () => commitSensitivity(i, Number(slider.value), row));
    row.appendChild(name);
    row.appendChild(slider);
    row.appendChild(readout);
    host.appendChild(row);
  });
}

async function commitSensitivity(index, value, row) {
  if (!state.running) {
    addLog("error", "err.not_running", {});
    renderSensitivity();
    return;
  }
  const next = state.commands.enabled.map((_, i) => state.sensitivity[i] ?? 5);
  next[index] = value;
  row.classList.add("busy");
  try {
    const res = await api().set_sensitivity(next);
    if (res && res.ok === false) {
      addLog("error", res.code, res.params || {});
      renderSensitivity();
    }
  } finally {
    row.classList.remove("busy");
  }
}

// ─── Folding ──────────────────────────────────────────────────────────────
function setCollapsed(id, collapsed) {
  const card = document.getElementById(id);
  if (card) card.classList.toggle("collapsed", collapsed);
}

function isCollapsed(id) {
  const card = document.getElementById(id);
  return !!card && card.classList.contains("collapsed");
}

/** Fold the pickers away once the choice behind them is made. Once per profile. */
function autoFold() {
  const key = state.loadedProfile;
  if (!key || state.autoFoldedFor === key) return;
  state.autoFoldedFor = key;
  setCollapsed("devices-card", true);
  // An untrained profile is the one case where the training panel is the point.
  const trained = Object.keys(state.commands.trained || {}).filter((a) => a !== "neutral").length;
  setCollapsed("training-card", trained > 0);
}

function renderSummaries() {
  const parts = [];
  if (state.selectedHeadset) parts.push(state.selectedHeadset);
  if (state.loadedProfile) parts.push(state.loadedProfile);
  $("#devices-summary").textContent = parts.length ? parts.join("  ·  ") : t("status.pick_headset");

  const trained = state.commands.trained || {};
  const count = Object.keys(trained).filter((a) => a !== "neutral").length;
  const skill = state.trainingResult && typeof state.trainingResult.skill === "number"
    ? "  ·  " + t("stat.skill") + " " + Math.round(state.trainingResult.skill * 100) + "%" : "";
  $("#training-summary").textContent = count ? t("summary.commands", { count }) + skill : t("summary.untrained");
}

// ─── Training: the command roster ─────────────────────────────────────────
function renderCommands() {
  const host = $("#command-list");
  host.innerHTML = "";
  const { enabled, disabled } = state.commands;
  for (const action of ["neutral", ...enabled, ...disabled]) {
    host.appendChild(commandRow(action, enabled.includes(action)));
  }
  renderAddCommand();
}

function commandRow(action, on) {
  const row = document.createElement("div");
  row.className = "cmd-row" + (action !== "neutral" && !on ? " off" : "");
  row.dataset.action = action;
  const isNeutral = action === "neutral";
  const color = state.actionColors[action] || (isNeutral ? "#ff0066" : "#666");
  const times = state.commands.trained[action] || 0;

  const toggleCell = document.createElement("div");
  if (!isNeutral) {
    const toggle = document.createElement("button");
    toggle.className = "toggle" + (on ? " on" : "");
    toggle.title = t(on ? "cmd.disable" : "cmd.enable");
    toggle.addEventListener("click", () => toggleCommand(action, !on, row));
    toggleCell.appendChild(toggle);
  }

  const name = document.createElement("div");
  name.className = "cmd-name";
  const sw = document.createElement("i");
  sw.className = "swatch";
  sw.style.background = color;
  const label = document.createElement("span");
  label.textContent = actionLabel(action);
  name.appendChild(sw);
  name.appendChild(label);
  // Say what the command is for right where it is trained.
  if (!isNeutral && state.bindings[action] && state.bindings[action] !== "none") {
    const does = document.createElement("span");
    does.className = "cmd-does";
    does.textContent = "→ " + playerLabel(state.bindings[action]);
    name.appendChild(does);
  }

  const count = document.createElement("div");
  count.className = "cmd-count" + (times ? "" : " none");
  count.textContent = times || "—";
  count.title = t("cmd.times", { count: times });

  const actions = document.createElement("div");
  actions.className = "cmd-actions";
  const train = document.createElement("button");
  train.className = "btn tiny primary";
  train.textContent = t(times ? "btn.retrain" : "btn.train");
  train.addEventListener("click", () => startTraining(action));
  actions.appendChild(train);

  if (times) {
    const erase = document.createElement("button");
    erase.className = "btn tiny ghost danger";
    erase.textContent = t("btn.erase");
    erase.title = t("cmd.erase.hint");
    erase.addEventListener("click", async () => {
      if (!confirm(t("confirm.erase", { action: actionLabel(action) }))) return;
      await callTraining(() => api().erase_training(action), erase, row);
    });
    actions.appendChild(erase);
  }

  row.appendChild(toggleCell);
  row.appendChild(name);
  row.appendChild(count);
  row.appendChild(actions);
  return row;
}

function renderAddCommand() {
  const select = $("#in-add-command");
  const button = $("#btn-add-command");
  const hint = $("#add-command-hint");
  const { enabled, disabled, available, max_active } = state.commands;
  const taken = new Set([...enabled, ...disabled]);
  const free = (available || []).filter((a) => !taken.has(a));
  const full = enabled.length >= max_active;

  select.innerHTML = "";
  for (const action of free) {
    const opt = document.createElement("option");
    opt.value = action;
    opt.textContent = actionLabel(action);
    select.appendChild(opt);
  }
  select.disabled = full || !free.length;
  button.disabled = full || !free.length;
  hint.textContent = full ? t("cmd.slots_full", { max: max_active })
    : t("cmd.slots_left", { left: max_active - enabled.length });
}

async function toggleCommand(action, on, row) {
  const { enabled, max_active } = state.commands;
  if (on && enabled.length >= max_active) {
    addLog("error", "err.too_many_actions", { max: max_active });
    return;
  }
  const next = on ? [...enabled, action] : enabled.filter((a) => a !== action);
  await callTraining(() => api().set_active_actions(next), null, row);
}

async function callTraining(fn, button, row) {
  if (!state.running) return addLog("error", "err.not_running", {});
  if (button) button.disabled = true;
  if (row) row.classList.add("busy");
  try {
    const res = await fn();
    if (res && res.ok === false) addLog("error", res.code, res.params || {});
    return res;
  } finally {
    if (button) button.disabled = false;
    if (row) row.classList.remove("busy");
  }
}

function openNewProfile() {
  if (!state.running || !state.selectedHeadset) return addLog("error", "err.no_headset_selected", {});
  $("#in-new-profile").value = "";
  $("#profile-overlay").classList.remove("hidden");
  $("#in-new-profile").focus();
}

async function createProfile() {
  const name = $("#in-new-profile").value.trim();
  if (!name) return;
  const button = $("#btn-create-profile");
  button.disabled = true;
  try {
    const res = await api().create_profile(name);
    if (res && res.ok === false) {
      addLog("error", res.code, res.params || {});
      return;
    }
    $("#profile-overlay").classList.add("hidden");
    setCollapsed("training-card", false);
  } finally {
    button.disabled = false;
  }
}

function renderTrainingCard() {
  $("#training-card").classList.toggle("hidden", !(state.running && state.loadedProfile));
}

// ─── Training: the brain map ──────────────────────────────────────────────
// Neutral sits at the origin and each command at its distance from it: a
// command drawn on top of neutral is one the detector cannot tell apart from
// doing nothing, and it will never fire reliably.
function renderBrainmap() {
  const svg = $("#brainmap");
  const points = (state.trainingResult && state.trainingResult.brain_map) || [];
  const cx = 140, cy = 156, r = 128;

  let out = "";
  for (const ring of [0.25, 0.5, 0.75, 1]) {
    const rr = r * ring;
    out += '<path class="bm-arc" d="M ' + (cx - rr) + " " + cy + " A " + rr + " " + rr + " 0 0 1 " + (cx + rr) + " " + cy + '" />';
  }
  out += '<line class="bm-axis" x1="' + (cx - r) + '" y1="' + cy + '" x2="' + (cx + r) + '" y2="' + cy + '" />';

  const legend = [];
  for (const point of points) {
    const [px, py] = point.coordinates || [0, 0];
    const distance = Math.min(1, Math.hypot(px, py));
    const x = cx + px * r;
    const y = cy - Math.abs(py) * r;
    const color = state.actionColors[point.action] || "#ff0066";
    out += '<circle class="bm-halo" cx="' + x + '" cy="' + y + '" r="13" fill="' + color + '" />' +
      '<circle class="bm-dot" cx="' + x + '" cy="' + y + '" r="6.5" fill="' + color + '" />';
    legend.push({ action: point.action, color, distance });
  }
  svg.innerHTML = out;

  const host = $("#brainmap-legend");
  host.innerHTML = "";
  for (const item of legend) {
    const row = document.createElement("div");
    row.className = "bm-legend-row";
    const sw = document.createElement("i");
    sw.className = "swatch";
    sw.style.background = item.color;
    const label = document.createElement("span");
    label.textContent = actionLabel(item.action);
    const dist = document.createElement("span");
    dist.className = "dist";
    dist.textContent = item.action === "neutral" ? "—" : item.distance.toFixed(2);
    row.appendChild(sw);
    row.appendChild(label);
    row.appendChild(dist);
    host.appendChild(row);
  }
  if (!legend.length) {
    const none = document.createElement("div");
    none.className = "muted small";
    none.textContent = t("panel.brainmap.empty");
    host.appendChild(none);
  }
  renderTrainingStats();
}

function renderTrainingStats() {
  const host = $("#training-stats");
  const result = state.trainingResult || {};
  host.innerHTML = "";
  const rows = [
    ["stat.skill", typeof result.skill === "number" ? Math.round(result.skill * 100) + "%" : null],
    ["stat.threshold", typeof result.threshold === "number" ? result.threshold.toFixed(2) : null],
    ["stat.last_score", typeof result.last_score === "number" ? result.last_score.toFixed(2) : null],
  ];
  for (const [key, value] of rows) {
    if (value === null) continue;
    const row = document.createElement("div");
    row.className = "stat-row";
    const label = document.createElement("span");
    label.textContent = t(key);
    const b = document.createElement("b");
    b.textContent = value;
    row.appendChild(label);
    row.appendChild(b);
    host.appendChild(row);
  }
}

// ─── Training: the eight-second window ────────────────────────────────────
// Cortex opens and closes the window on the `sys` stream; the countdown only
// fills the gap between MC_Started and MC_Succeeded, so it cannot disagree
// with what is actually being recorded.
let trainingTimer = null;

async function startTraining(action) {
  if (!state.running) return addLog("error", "err.not_running", {});
  state.training = { action, phase: "arming", started: 0, score: null, threshold: null };
  renderTrainingOverlay();
  $("#training-overlay").classList.remove("hidden");
  const res = await api().start_training(action);
  if (res && res.ok === false) {
    addLog("error", res.code, res.params || {});
    closeTrainingOverlay();
  }
}

function onTrainingEvent(data) {
  const event = data.event;
  if (!state.training && event !== "MC_Started") return;
  if (event === "MC_Started") {
    if (!state.training) state.training = { action: data.action, score: null };
    state.training.phase = "recording";
    state.training.started = Date.now();
    startCountdown();
  } else if (event === "MC_Succeeded") {
    stopCountdown();
    state.training.phase = "review";
  } else if (event === "MC_Failed") {
    stopCountdown();
    state.training.phase = "failed";
  } else if (event === "MC_Completed" || event === "MC_Rejected") {
    closeTrainingOverlay();
    return;
  }
  renderTrainingOverlay();
}

function startCountdown() {
  stopCountdown();
  trainingTimer = setInterval(renderTrainingOverlay, 100);
}

function stopCountdown() {
  if (trainingTimer) clearInterval(trainingTimer);
  trainingTimer = null;
}

function closeTrainingOverlay() {
  stopCountdown();
  state.training = null;
  $("#training-overlay").classList.add("hidden");
}

function renderTrainingOverlay() {
  const training = state.training;
  if (!training) return;
  const action = training.action || "neutral";
  const color = state.actionColors[action] || "#ff0066";
  $("#train-swatch").style.background = color;
  $("#train-action-name").textContent = actionLabel(action);

  // Remind the user what this thought is going to do once it is trained.
  const binding = state.bindings[action];
  $("#train-binding").textContent = action !== "neutral" && binding && binding !== "none"
    ? t("train.will_do", { action: playerLabel(binding) })
    : action === "neutral" ? t("train.neutral_role") : "";

  const ring = $("#train-ring-fill");
  const count = $("#train-count");
  const phase = $("#train-phase");
  const instruction = $("#train-instruction");
  const circumference = 2 * Math.PI * 52;
  let progress = 0;
  count.classList.remove("small");

  if (training.phase === "arming") {
    phase.className = "train-phase";
    phase.textContent = t("train.phase.arming");
    count.textContent = "…";
    instruction.textContent = t("train.getready");
  } else if (training.phase === "recording") {
    const elapsed = (Date.now() - training.started) / 1000;
    progress = Math.min(1, elapsed / TRAINING_SECONDS);
    phase.className = "train-phase";
    phase.textContent = t("train.phase.recording");
    count.textContent = Math.max(0, Math.ceil(TRAINING_SECONDS - elapsed));
    instruction.textContent = instructionFor(action);
  } else if (training.phase === "review") {
    progress = 1;
    phase.className = "train-phase ok";
    phase.textContent = t("train.phase.review");
    count.classList.add("small");
    count.textContent = t("train.keep");
    instruction.textContent = t("train.keep.hint");
  } else if (training.phase === "failed") {
    progress = 1;
    phase.className = "train-phase err";
    phase.textContent = t("train.phase.failed");
    count.classList.add("small");
    count.textContent = t("train.failed");
    instruction.textContent = t("train.failed.hint");
  }

  ring.style.strokeDashoffset = String(circumference * (1 - progress));
  ring.style.stroke = training.phase === "failed" ? "var(--err)" : color;
  renderTrainingScore();
  renderTrainingButtons();
}

function renderTrainingScore() {
  const training = state.training;
  const box = $("#train-score");
  const show = training.phase === "review" && typeof training.score === "number";
  box.classList.toggle("hidden", !show);
  if (!show) return;
  const score = Math.max(0, Math.min(1, training.score));
  $("#train-score-value").textContent = score.toFixed(2);
  const bar = $("#train-score-bar");
  bar.style.width = (score * 100).toFixed(0) + "%";
  // Judged against the profile's own threshold, not an invented number.
  const threshold = typeof training.threshold === "number" ? training.threshold : 0.75;
  bar.style.background = score >= threshold ? "var(--ok)" : "var(--warn)";
}

function renderTrainingButtons() {
  const host = $("#train-buttons");
  const training = state.training;
  host.innerHTML = "";
  const add = (label, className, onClick) => {
    const button = document.createElement("button");
    button.className = "btn " + className;
    button.textContent = label;
    button.addEventListener("click", async () => {
      $$("#train-buttons .btn").forEach((b) => (b.disabled = true));
      await onClick();
    });
    host.appendChild(button);
  };

  if (training.phase === "review") {
    add(t("btn.discard"), "ghost", async () => {
      const res = await api().reject_training();
      if (res && res.ok === false) addLog("error", res.code, res.params || {});
      closeTrainingOverlay();
    });
    add(t("btn.accept"), "primary", async () => {
      const res = await api().accept_training();
      if (res && res.ok === false) {
        addLog("error", res.code, res.params || {});
        closeTrainingOverlay();
      }
    });
  } else if (training.phase === "failed") {
    add(t("btn.close"), "ghost", async () => closeTrainingOverlay());
    add(t("btn.retry"), "primary", async () => {
      const action = training.action;
      closeTrainingOverlay();
      await startTraining(action);
    });
  } else {
    add(t("btn.cancel"), "ghost", async () => {
      await api().reject_training();
      closeTrainingOverlay();
    });
  }
}

// ─── Labels ───────────────────────────────────────────────────────────────
function actionLabel(action) {
  return tHas("action." + action) ? t("action." + action) : action;
}

function playerLabel(action) {
  return tHas("player." + action) ? t("player." + action) : action;
}

function instructionFor(action) {
  return tHas("train.instr." + action) ? t("train.instr." + action)
    : t("train.instr.generic", { action: actionLabel(action) });
}

function escapeText(value) {
  return String(value).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/"/g, '\\"');
}

// ─── Settings ─────────────────────────────────────────────────────────────
function fillSettingsForm() {
  const s = state.settings;
  $("#in-client-id").value = s.client_id || "";
  $("#in-client-secret").value = "";
  $("#secret-saved-note").classList.toggle("hidden", !s.client_secret_set);
  $("#in-spotify-client-id").value = s.spotify_client_id || "";
  $("#redirect-uri").textContent = state.redirectUri;
  renderSpotifyAccount();
}

function renderSpotifyAccount() {
  const signedIn = state.spotify.signed_in;
  $("#spotify-account-status").textContent = signedIn ? t("setup.spotify.connected") : t("setup.spotify.not_connected");
  $("#spotify-account-status").className = "small " + (signedIn ? "good" : "muted");
  $("#btn-spotify-logout").classList.toggle("hidden", !signedIn);
}

function openSettings(section) {
  fillSettingsForm();
  $("#settings").classList.remove("hidden");
  const target = section ? document.getElementById("settings-" + section) : null;
  if (target) target.scrollIntoView({ block: "start" });
}

async function saveSettings() {
  const payload = {
    client_id: $("#in-client-id").value.trim(),
    spotify_client_id: $("#in-spotify-client-id").value.trim(),
  };
  // Blank secret = keep whatever is stored, so it is never wiped by accident.
  const secret = $("#in-client-secret").value.trim();
  if (secret) payload.client_secret = secret;
  state.settings = await api().save_settings(payload);
  fillSettingsForm();
  renderSetup();
  $("#settings").classList.add("hidden");
}

async function connectSpotify() {
  if (!state.settings.spotify_client_id) return openSettings("spotify");
  $("#spotify-login-overlay").classList.remove("hidden");
  const res = await api().spotify_login();
  if (res && res.ok === false) {
    $("#spotify-login-overlay").classList.add("hidden");
    addLog("error", res.code, res.params || {});
  }
}

// ─── Events coming from Python ────────────────────────────────────────────
window.pushEvent = function (event, data) {
  switch (event) {
    case "status":
      state.steps[data.step] = { state: data.state, code: data.code, params: data.params };
      renderSteps();
      if (data.state === "error" && data.code) addLog("error", data.code, data.params);
      if (data.step === "spotify" && data.state !== "pending") {
        $("#spotify-login-overlay").classList.add("hidden");
      }
      break;
    case "log":
      addLog(data.level, data.code, data.params);
      break;
    case "profiles":
      state.profiles = data.items || [];
      renderProfiles();
      break;
    case "headsets":
      state.headsets = data.items || [];
      state.selectedHeadset = data.selected || "";
      renderHeadsets();
      renderProfiles();
      renderQuality();
      renderSummaries();
      renderSetup();
      renderBindings();
      break;
    case "actions":
      state.actionColors = data.colors || state.actionColors;
      state.loadedProfile = data.profile || state.loadedProfile;
      state.changingProfile = false;
      renderProfiles();
      renderSummaries();
      break;
    case "sensitivity":
      state.sensitivity = data.values || [];
      renderSensitivity();
      break;
    case "commands":
      state.changingProfile = false;
      state.commands = {
        enabled: data.enabled || [],
        disabled: data.disabled || [],
        trained: data.trained || {},
        available: data.available || [],
        max_active: data.max_active || 4,
      };
      state.loadedProfile = data.profile || state.loadedProfile;
      renderCommands();
      renderSensitivity();
      renderTrainingCard();
      renderSummaries();
      renderSetup();
      // The controls card labels untrained commands from these counts.
      renderBindings();
      autoFold();
      break;
    case "bindings":
      state.bindings = data.bindings || {};
      state.bindingCommands = [...(data.commands || []), ...(data.disabled || [])];
      state.actionColors = { ...state.actionColors, ...(data.colors || {}) };
      renderBindings();
      renderCommands();
      break;
    case "live":
      applyLive(data);
      break;
    case "fired":
      onFired(data);
      break;
    case "mind_control":
      state.mindControl = !!data.on;
      renderMindSwitch();
      break;
    case "spotify":
      state.spotify = data;
      renderNowPlaying();
      renderSetup();
      renderControlsWarnings();
      renderSpotifyAccount();
      if (data.signed_in) $("#spotify-login-overlay").classList.add("hidden");
      break;
    case "quality":
      state.quality = data || {};
      renderQuality();
      break;
    case "training":
      onTrainingEvent(data);
      break;
    case "training_score":
      if (state.training) {
        state.training.score = data.score;
        state.training.threshold = data.threshold;
        renderTrainingOverlay();
      }
      break;
    case "training_result":
      state.trainingResult = data;
      renderBrainmap();
      renderSummaries();
      renderBindings();
      break;
    case "running":
      state.running = data.running;
      if (!data.running) {
        state.selectedHeadset = "";
        state.loadedProfile = "";
        state.sensitivity = [];
        state.quality = {};
        state.trainingResult = null;
        state.commands = { enabled: [], disabled: [], trained: {}, available: [], max_active: 4 };
        state.bindingCommands = [];
        state.live = null;
        state.changingProfile = false;
        state.autoFoldedFor = "";
        setCollapsed("devices-card", false);
        closeTrainingOverlay();
      }
      renderAll();
      break;
  }
};

// ─── Bootstrap ────────────────────────────────────────────────────────────
async function boot() {
  const info = await api().get_state();
  state.settings = info.settings;
  state.running = info.running;
  state.redirectUri = info.redirect_uri;
  state.triggerDefaults = info.trigger_defaults || state.triggerDefaults;
  state.mindControl = info.settings.mind_control !== false;
  state.spotify.signed_in = !!info.spotify_signed_in;
  state.spotify.client_id_set = !!info.settings.spotify_client_id;

  if (!info.settings.language) {
    $("#welcome").classList.remove("hidden");
  } else {
    setLang(info.settings.language);
    $("#app").classList.remove("hidden");
  }
  applyStaticI18n();
  fillSettingsForm();
  renderLog();
  // Only now is pushEvent in place to receive what the backend has to say.
  await api().page_ready();
}

function wire() {
  $$("#welcome .lang-btn").forEach((btn) =>
    btn.addEventListener("click", async () => {
      setLang(btn.dataset.lang);
      state.settings = await api().save_settings({ language: btn.dataset.lang });
      $("#welcome").classList.add("hidden");
      $("#app").classList.remove("hidden");
      applyStaticI18n();
      renderLog();
    })
  );

  $$(".lang-chip").forEach((btn) =>
    btn.addEventListener("click", async () => {
      setLang(btn.dataset.lang);
      state.settings = await api().save_settings({ language: btn.dataset.lang });
      applyStaticI18n();
      renderLog();
    })
  );

  $("#btn-run").addEventListener("click", async () => {
    $("#btn-run").disabled = true;
    try {
      const res = state.running ? await api().stop() : await api().start();
      if (res && res.ok === false && res.code) {
        addLog("error", res.code, res.params || {});
        if (res.code === "err.no_credentials") openSettings("cortex");
      }
    } finally {
      $("#btn-run").disabled = false;
    }
  });

  $("#btn-settings").addEventListener("click", () => openSettings());
  $("#btn-close-settings").addEventListener("click", () => $("#settings").classList.add("hidden"));
  $("#btn-save").addEventListener("click", saveSettings);
  $$("[data-open-settings]").forEach((b) => b.addEventListener("click", () => openSettings(b.dataset.openSettings)));
  $$("[data-open-url]").forEach((b) => b.addEventListener("click", () => api().open_url(b.dataset.openUrl)));

  $("#btn-copy-redirect").addEventListener("click", async () => {
    const button = $("#btn-copy-redirect");
    try {
      await navigator.clipboard.writeText(state.redirectUri);
    } catch (e) {
      // Some WebViews refuse the async clipboard; select the text instead so a
      // manual copy is one keystroke away.
      const range = document.createRange();
      range.selectNodeContents($("#redirect-uri"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    button.textContent = t("btn.copied");
    setTimeout(() => (button.textContent = t("btn.copy")), 1500);
  });

  $("#btn-clear-log").addEventListener("click", () => {
    logEntries.length = 0;
    renderLog();
  });

  $("#btn-refresh-headsets").addEventListener("click", async () => {
    const btn = $("#btn-refresh-headsets");
    btn.disabled = true;
    try {
      const res = await api().refresh_headsets();
      if (res && res.ok === false) addLog("error", res.code, res.params || {});
    } finally {
      btn.disabled = false;
    }
  });

  $$(".card-head").forEach((head) =>
    head.addEventListener("click", () => setCollapsed(head.dataset.collapse, !isCollapsed(head.dataset.collapse)))
  );

  $("#device-pill").addEventListener("click", () => {
    const open = isCollapsed("devices-card");
    setCollapsed("devices-card", !open);
    if (open) $("#devices-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  $("#btn-change-profile").addEventListener("click", () => {
    state.changingProfile = true;
    renderProfiles();
  });

  $$(".q-tab").forEach((tab) =>
    tab.addEventListener("click", () => {
      state.qualityView = tab.dataset.quality;
      $$(".q-tab").forEach((b) => b.classList.toggle("active", b === tab));
      renderQuality();
    })
  );

  // ── Music ──
  $("#btn-connect-spotify").addEventListener("click", connectSpotify);
  $("#btn-setup-spotify").addEventListener("click", connectSpotify);
  $("#btn-cancel-login").addEventListener("click", async () => {
    await api().spotify_cancel_login();
    $("#spotify-login-overlay").classList.add("hidden");
  });
  $("#btn-spotify-logout").addEventListener("click", async () => {
    await api().spotify_logout();
  });
  $("#now-device").addEventListener("mousedown", refreshDevices);
  $("#now-device").addEventListener("focus", refreshDevices);
  $("#now-device").addEventListener("change", (e) => playOn(e.target.value));
  $("#now-open").addEventListener("click", (e) => {
    e.preventDefault();
    const url = e.currentTarget.dataset.url;
    if (url) api().open_url(url);
  });
  $$(".tbtn").forEach((b) => {
    b.title = playerLabel(b.dataset.player);
    b.addEventListener("click", () => playerClick(b.dataset.player, b));
  });

  $("#mind-toggle").addEventListener("click", async (e) => {
    e.preventDefault();
    const res = await api().set_mind_control(!state.mindControl);
    if (res && res.ok === false) addLog("error", res.code, res.params || {});
  });

  ["#in-threshold", "#in-hold", "#in-cooldown"].forEach((sel) => {
    $(sel).addEventListener("input", syncTuningLabels);
    $(sel).addEventListener("change", commitTuning);
  });
  $("#btn-tune-reset").addEventListener("click", async () => {
    const d = state.triggerDefaults;
    $("#in-threshold").value = d.threshold;
    $("#in-hold").value = d.hold;
    $("#in-cooldown").value = d.cooldown;
    syncTuningLabels();
    await commitTuning();
  });

  // ── Training ──
  $("#btn-add-command").addEventListener("click", async () => {
    const action = $("#in-add-command").value;
    if (!action) return;
    await callTraining(() => api().set_active_actions([...state.commands.enabled, action]), $("#btn-add-command"));
  });
  $("#btn-new-profile").addEventListener("click", openNewProfile);
  $("#btn-new-profile-devices").addEventListener("click", openNewProfile);
  $("#btn-cancel-profile").addEventListener("click", () => $("#profile-overlay").classList.add("hidden"));
  $("#btn-create-profile").addEventListener("click", createProfile);
  $("#in-new-profile").addEventListener("keydown", (e) => {
    if (e.key === "Enter") createProfile();
  });
  $("#btn-reset-all").addEventListener("click", async () => {
    if (!confirm(t("confirm.reset_all", { profile: state.loadedProfile }))) return;
    await callTraining(() => api().reset_training(), $("#btn-reset-all"));
  });

  // "3 s ago" has to keep counting without an event to prompt it.
  setInterval(renderLastActionWhen, 1000);
}

// A blank window with no explanation is the worst failure mode, so a bootstrap
// error is painted on screen and stashed for the Python side to print.
function fatalScreen(message) {
  window.__bootError = String(message);
  let box = document.getElementById("fatal-notice");
  if (!box) {
    box = document.createElement("div");
    box.id = "fatal-notice";
    box.className = "overlay";
    document.body.appendChild(box);
  }
  box.innerHTML =
    '<div class="welcome-card" style="text-align:left">' +
    '<h1 style="font-size:18px;margin-bottom:12px">The interface failed to start</h1>' +
    '<pre style="white-space:pre-wrap;color:#ff5468;font-size:12px;margin:0">' +
    escapeText(window.__bootError) + "</pre></div>";
}

function clearFatalScreen() {
  const box = document.getElementById("fatal-notice");
  if (box) box.remove();
  window.__bootError = "";
}

window.addEventListener("error", (e) => fatalScreen(e.message + " @ " + e.filename + ":" + e.lineno));
window.addEventListener("unhandledrejection", (e) => fatalScreen(e.reason));

window.addEventListener("pywebviewready", () => {
  clearFatalScreen();
  try {
    wire();
    boot();
  } catch (e) {
    fatalScreen(e && e.stack ? e.stack : e);
  }
});

setTimeout(() => {
  if (!window.pywebview || !window.pywebview.api) {
    fatalScreen("window.pywebview.api was never injected (pywebviewready did not fire).");
  }
}, 8000);
