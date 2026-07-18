/* =========================================================================
   dashboard.js — live pit instrument.

   Stability contract (why this file looks the way it does):
     * WS frames arrive at ~18 Hz, but nothing re-animates per message.
     * Noisy channels (tyre temp, slip, power, torque, boost) pass through
       per-channel EMAs before display, and land in fixed-width tabular
       cells, so text changes never shift layout.
     * Continuous elements (RPM band, pedal ribbons, steering, G-ball, lap
       clock) render from a single rAF loop that lerps toward targets —
       smooth at any packet rate, frozen instantly when reduced-motion.
   ========================================================================= */
(function () {
  "use strict";
  const FH = window.FH;
  const { api, fmt, fmtLapTime, clamp, tempClass, toast } = FH;

  FH.shell("live", { status: true });
  FH.registerSW();

  // Recording state belongs in the top bar too (visible from every scroll
  // position and in landscape instrument mode where the card is hidden).
  const topRec = document.createElement("span");
  topRec.className = "pill rec hidden";
  topRec.textContent = "REC";
  const statusEl = document.querySelector(".topbar .status");
  if (statusEl) statusEl.prepend(topRec);

  const $ = (id) => document.getElementById(id);
  const el = {
    dot: $("connDot"), connText: $("connText"), pps: $("pps"),
    racePill: $("racePill"), carInfo: $("carInfo"),
    speed: $("speed"), speedUnitBtn: $("speedUnitBtn"), gear: $("gear"),
    rpm: $("rpm"), rpmMax: $("rpmMax"), rpmFill: $("rpmFill"),
    curLap: $("curLap"), lastLap: $("lastLap"), bestLap: $("bestLap"),
    lapNum: $("lapNum"), lapDelta: $("lapDelta"),
    tempUnitBtn: $("tempUnitBtn"),
    pods: [$("podFL"), $("podFR"), $("podRL"), $("podRR")],
    powerKw: $("powerKw"), torqueNm: $("torqueNm"), boostPsi: $("boostPsi"),
    steerFill: $("steerFill"), steerV: $("steerV"), gText: $("gText"),
    thrBar: $("thrBar"), thrV: $("thrV"), brkBar: $("brkBar"), brkV: $("brkV"),
    ribbonBrake: $("ribbonBrake"), ribbonThrottle: $("ribbonThrottle"),
    gball: $("gball"),
    recPill: $("recPill"), recFrames: $("recFrames"),
    btnStart: $("btnStart"), btnStop: $("btnStop"), btnMarker: $("btnMarker"),
    btnWake: $("btnWake"), wakeState: $("wakeState")
  };
  const podTemp = el.pods.map(p => p.querySelector(".pod-temp"));
  const podSlip = el.pods.map(p => p.querySelector(".pod-slip > i"));

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const CLASS_LETTER = ["D", "C", "B", "A", "S1", "S2", "X"];

  // ------------------------------------------------------------- smoothing
  const smTempC = FH.emaArray(0.18, 4);   // ~1 s settle at 18 Hz
  const smSlip = FH.emaArray(0.30, 4);
  const smPower = FH.ema(0.30);
  const smTorque = FH.ema(0.30);
  const smBoost = FH.ema(0.30);

  // Raw last-known values (for unit-toggle re-render without waiting a frame)
  let lastTempsC = null;
  let lastSpeedKmh = 0;

  // Targets for the rAF loop
  const tgt = { rpmPct: 0, thr: 0, brk: 0, steer: 0, gLat: 0, gLong: 0 };
  const cur = { rpmPct: 0, thr: 0, brk: 0, steer: 0, gLat: 0, gLong: 0 };

  // Lap clock interpolation
  let lapBase = 0, lapBaseAt = 0, lapTicking = false, lapVisible = false;

  // ============================================================ WebSocket
  let ws = null;
  let backoff = 500;
  const BACKOFF_MAX = 8000;
  let reconnectTimer = null;
  let everConnected = false;

  function wsURL() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws/live";
  }

  function connect() {
    clearTimeout(reconnectTimer);
    setConn("connecting");
    try { ws = new WebSocket(wsURL()); }
    catch (e) { scheduleReconnect(); return; }
    ws.onopen = () => { everConnected = true; backoff = 500; setConn("open"); };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.status) applyStatus(msg.status);
      if (msg.type === "telemetry") applyData(msg.data);
    };
    ws.onclose = () => { setConn("closed"); scheduleReconnect(); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(BACKOFF_MAX, Math.round(backoff * 1.7));
  }
  function setConn(state) {
    el.dot.className = "dot";
    if (state === "open") { el.dot.classList.add("ok"); el.connText.textContent = "Live"; }
    else if (state === "connecting") { el.dot.classList.add("warn"); el.connText.textContent = "Connecting"; }
    else { el.dot.classList.add("bad"); el.connText.textContent = everConnected ? "Reconnecting" : "Offline"; }
  }

  function applyStatus(s) {
    el.pps.textContent = fmt(s.pps, 0) + " pps";
    if (s.connected === false && el.dot.classList.contains("ok")) {
      el.dot.className = "dot warn";
      el.connText.textContent = "No packets";
    }
    // First-run helper: visible only while no packet has EVER arrived.
    const card = document.getElementById("setupCard");
    if (card) card.classList.toggle("hidden", (s.valid || 0) > 0);
  }

  // Fill the setup card with this machine's real addresses.
  api("/health").then((h) => {
    const ip = document.getElementById("setupIp");
    const port = document.getElementById("setupPort");
    if (ip && h.lan_ip) ip.textContent = h.lan_ip;
    if (port && h.udp_port) port.textContent = h.udp_port;
  }).catch(() => {});

  // ============================================================ rendering
  let lastPillState = "";
  function setPill(state) {
    if (state === lastPillState) return;
    lastPillState = state;
    if (state === "race") { el.racePill.textContent = "ON TRACK"; el.racePill.className = "pill live"; }
    else if (state === "roam") { el.racePill.textContent = "FREE ROAM"; el.racePill.className = "pill amber"; }
    else if (state === "paused") { el.racePill.textContent = "PAUSED"; el.racePill.className = "pill"; }
    else { el.racePill.textContent = "WAITING FOR PACKETS…"; el.racePill.className = "pill"; }
  }

  function applyData(d) {
    if (!d) { setPill("waiting"); return; }

    const racing = d.race_on === 1 || d.race_on === true;
    const inLap = (d.cur_lap || 0) > 0 || (d.lap_number || 0) > 0;
    setPill(racing ? (inLap ? "race" : "roam") : "paused");

    // hero
    lastSpeedKmh = d.speed_kmh || 0;
    renderSpeed();
    el.gear.textContent = d.gear != null ? d.gear : "–";
    el.rpm.textContent = fmt(d.rpm, 0);
    el.rpmMax.textContent = fmt(d.rpm_max, 0);
    tgt.rpmPct = clamp(d.rpm_pct || 0, 0, 100);

    // timing — clock interpolates between packets in the rAF loop
    lapBase = d.cur_lap || 0;
    lapBaseAt = performance.now();
    lapTicking = racing && lapBase > 0;
    lapVisible = lapBase > 0;
    if (!lapVisible) el.curLap.textContent = "--:--.---";
    el.lastLap.textContent = fmtLapTime(d.last_lap);
    el.bestLap.textContent = fmtLapTime(d.best_lap);
    el.lapNum.textContent = (d.lap_number != null && inLap) ? (d.lap_number + 1) : "–";
    renderDelta(d.last_lap, d.best_lap);

    // tyres (EMA over °C)
    if (d.tire_temp_c) {
      lastTempsC = smTempC(d.tire_temp_c);
      renderTemps();
    }
    if (d.combined_slip) {
      const s = smSlip(d.combined_slip);
      for (let i = 0; i < 4; i++) {
        const v = Math.abs(s[i] || 0);
        podSlip[i].style.width = clamp(v / 1.5 * 100, 0, 100).toFixed(0) + "%";
        podSlip[i].style.backgroundColor =
          v > 1.1 ? "var(--over)" : v > 0.8 ? "var(--amber)" : "var(--ink-mute)";
      }
    }

    // powertrain
    el.powerKw.textContent = fmt(smPower(d.power_kw), 0);
    el.torqueNm.textContent = fmt(smTorque(d.torque_nm), 0);
    el.boostPsi.textContent = fmt(smBoost(d.boost_psi), 1);

    // inputs / dynamics — targets only; rAF renders
    tgt.thr = clamp(d.throttle || 0, 0, 100);
    tgt.brk = clamp(d.brake || 0, 0, 100);
    tgt.steer = clamp(d.steer || 0, -1, 1);
    tgt.gLong = FH.toG(d.accel_long);
    tgt.gLat = FH.toG(d.accel_lat);
    el.thrV.textContent = fmt(tgt.thr, 0) + "%";
    el.brkV.textContent = fmt(tgt.brk, 0) + "%";
    el.steerV.textContent = (tgt.steer >= 0 ? "+" : "") + tgt.steer.toFixed(2);
    el.gText.textContent =
      (tgt.gLong >= 0 ? "+" : "") + tgt.gLong.toFixed(1) + " / " +
      (tgt.gLat >= 0 ? "+" : "") + tgt.gLat.toFixed(1) + " g";

    // car identity
    if (d.car_ordinal != null && d.car_ordinal !== 0) {
      const cls = CLASS_LETTER[d.car_class] || d.car_class;
      el.carInfo.textContent = [
        "#" + d.car_ordinal,
        cls + " " + (d.pi != null ? d.pi : ""),
        d.drivetrain || "",
        d.cylinders ? d.cylinders + "CYL" : ""
      ].filter(Boolean).join(" · ");
    }
  }

  function renderSpeed() {
    const out = FH.speedOut(lastSpeedKmh);
    el.speed.textContent = String(Math.round(out.v));
    el.speedUnitBtn.textContent = out.unit;
  }

  function renderTemps() {
    if (!lastTempsC) return;
    const useF = FH.settings.get("tempUnit", "c") === "f";
    el.tempUnitBtn.textContent = useF ? "°F" : "°C";
    for (let i = 0; i < 4; i++) {
      const c = lastTempsC[i];
      const shown = useF ? c * 9 / 5 + 32 : c;
      podTemp[i].textContent = fmt(shown, 0) + "°";
      const cls = tempClass(c);
      const pod = el.pods[i];
      if (!pod.classList.contains("t-" + cls)) {
        pod.classList.remove("t-cold", "t-optimal", "t-hot", "t-over");
        pod.classList.add("t-" + cls);
      }
    }
  }

  function renderDelta(last, best) {
    if (!last || !best || last <= 0 || best <= 0) {
      el.lapDelta.textContent = "–";
      el.lapDelta.className = "mono t-small";
      return;
    }
    const d = last - best;
    el.lapDelta.textContent = (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(3);
    el.lapDelta.className = "mono t-small " + (d > 0.0005 ? "delta-pos" : "delta-neg");
  }

  // ============================================================ rAF loop
  const gctx = el.gball.getContext("2d");

  function drawGBall() {
    const P = FH.palette();
    const dpr = window.devicePixelRatio || 1;
    const size = 108;
    if (el.gball.width !== size * dpr) {
      el.gball.width = size * dpr; el.gball.height = size * dpr;
    }
    gctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    gctx.clearRect(0, 0, size, size);
    const cx = size / 2, cy = size / 2;
    const gMax = 1.8, rMax = size / 2 - 6;
    gctx.strokeStyle = P.line;
    gctx.lineWidth = 1;
    for (const g of [0.5, 1.0, 1.5]) {
      gctx.beginPath(); gctx.arc(cx, cy, g / gMax * rMax, 0, Math.PI * 2); gctx.stroke();
    }
    gctx.beginPath(); gctx.moveTo(cx, 4); gctx.lineTo(cx, size - 4); gctx.stroke();
    gctx.beginPath(); gctx.moveTo(4, cy); gctx.lineTo(size - 4, cy); gctx.stroke();
    const x = cx + clamp(cur.gLat / gMax, -1, 1) * rMax;
    const y = cy - clamp(cur.gLong / gMax, -1, 1) * rMax;
    const mag = Math.hypot(cur.gLat, cur.gLong);
    gctx.fillStyle = mag > 1.2 ? P.over : mag > 0.7 ? P.amber : P.optimal;
    gctx.beginPath(); gctx.arc(x, y, 5, 0, Math.PI * 2); gctx.fill();
  }

  let lastFrame = performance.now();
  function frame(now) {
    const dt = Math.min(0.1, (now - lastFrame) / 1000);
    lastFrame = now;
    const k = reducedMotion ? 1 : Math.min(1, dt * 14);

    for (const key of Object.keys(tgt)) {
      cur[key] += (tgt[key] - cur[key]) * k;
    }

    // rpm band
    el.rpmFill.style.width = cur.rpmPct.toFixed(1) + "%";
    const rc = cur.rpmPct > 92 ? "max" : cur.rpmPct > 80 ? "warn" : "";
    if (el.rpmFill.dataset.zone !== rc) {
      el.rpmFill.dataset.zone = rc;
      el.rpmFill.className = "rpm-fill" + (rc ? " " + rc : "");
    }

    // signature ribbons + desktop bars
    el.ribbonBrake.style.height = cur.brk.toFixed(1) + "%";
    el.ribbonThrottle.style.height = cur.thr.toFixed(1) + "%";
    el.thrBar.style.width = cur.thr.toFixed(1) + "%";
    el.brkBar.style.width = cur.brk.toFixed(1) + "%";

    // steering (centre-out)
    const st = cur.steer;
    el.steerFill.style.width = (Math.abs(st) * 50).toFixed(1) + "%";
    if (st >= 0) { el.steerFill.style.left = "50%"; el.steerFill.style.right = "auto"; }
    else { el.steerFill.style.left = "auto"; el.steerFill.style.right = "50%"; }

    drawGBall();

    // lap clock — extrapolate between packets so milliseconds actually tick
    if (lapVisible) {
      const t = lapTicking && !reducedMotion
        ? lapBase + (now - lapBaseAt) / 1000
        : lapBase;
      el.curLap.textContent = fmtLapTime(t);
    }

    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // ============================================================ unit toggles
  el.speedUnitBtn.addEventListener("click", () => {
    FH.settings.set("speedUnit",
      FH.settings.get("speedUnit", "kmh") === "kmh" ? "mph" : "kmh");
    renderSpeed();
  });
  el.tempUnitBtn.addEventListener("click", () => {
    FH.settings.set("tempUnit",
      FH.settings.get("tempUnit", "c") === "c" ? "f" : "c");
    renderTemps();
  });

  // ============================================================ recording
  async function refreshRecording() {
    try { renderRecording(await api("/api/recording/status")); }
    catch (e) { /* transient */ }
  }
  function renderRecording(r) {
    const rec = !!r.recording;
    el.recPill.textContent = rec ? "REC" : "IDLE";
    el.recPill.className = "pill" + (rec ? " rec" : "");
    el.recFrames.textContent = rec
      ? (fmt(r.frame_count, 0) + " frames · session #" + r.session_id)
      : "not recording";
    el.btnStart.disabled = rec;
    el.btnStop.disabled = !rec;
    el.btnMarker.disabled = !rec;
    topRec.classList.toggle("hidden", !rec);
    if (r.record_mode) setModeUI(r.record_mode, r.stationary_timeout_s);
  }

  // ---------------------------------------------------- recording mode
  const MODE_HINTS = {
    event: "Records automatically when an event starts (staged at a start line " +
      "or lap timing live) and ends with the event — or after {S} s stationary/menu.",
    motion: "Records any driving. Stops after {S} s stationary or in menus.",
    manual: "Records only when you press ● Record. Still stops after {S} s " +
      "stationary as a walk-away net — ■ Stop is always instant."
  };
  function setModeUI(mode, stationaryS) {
    document.querySelectorAll(".mode-btn").forEach((b) =>
      b.classList.toggle("on", b.dataset.mode === mode));
    const hint = document.getElementById("recHint");
    if (hint) hint.textContent =
      (MODE_HINTS[mode] || "").replace("{S}", fmt(stationaryS || 30, 0));
  }
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        const s = await api("/api/settings", { method: "PUT", json: { record_mode: b.dataset.mode } });
        setModeUI(s.record_mode, s.stationary_timeout_s);
        toast("Auto-record: " + (b.dataset.mode === "event" ? "events only"
          : b.dataset.mode === "motion" ? "any driving" : "off (manual)"));
      } catch (e) { toast("Could not change mode: " + e.message); }
    });
  });
  el.btnStart.addEventListener("click", async () => {
    try { renderRecording(await api("/api/recording/start", { json: {} })); }
    catch (e) { toast("Start failed: " + e.message); }
  });
  el.btnStop.addEventListener("click", async () => {
    try { renderRecording(await api("/api/recording/stop", { json: {} })); toast("Recording saved"); }
    catch (e) { toast("Stop failed: " + e.message); }
  });
  el.btnMarker.addEventListener("click", async () => {
    const label = prompt("Marker label:", "marker");
    if (label === null) return;
    try { await api("/api/recording/marker", { json: { label: label || "marker" } }); toast("Marker added"); }
    catch (e) { toast("Marker failed: " + e.message); }
  });

  // ============================================================ wake lock
  let wakeLock = null;
  let wakeWanted = false;
  const wakeSupported = ("wakeLock" in navigator);
  async function requestWake() {
    if (!wakeSupported) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => {
        if (wakeWanted) el.wakeState.textContent = "paused";
      });
      el.wakeState.textContent = "on";
      el.btnWake.classList.add("on");
    } catch (e) { el.wakeState.textContent = "error"; }
  }
  async function releaseWake() {
    try { if (wakeLock) await wakeLock.release(); } catch (e) {}
    wakeLock = null;
    el.wakeState.textContent = "off";
    el.btnWake.classList.remove("on");
  }
  el.btnWake.addEventListener("click", () => {
    if (!wakeSupported) { toast("Wake Lock not supported on this device"); return; }
    wakeWanted = !wakeWanted;
    if (wakeWanted) requestWake(); else releaseWake();
  });
  document.addEventListener("visibilitychange", () => {
    if (wakeWanted && document.visibilityState === "visible" && !wakeLock) requestWake();
  });
  if (!wakeSupported) { el.wakeState.textContent = "n/a"; el.btnWake.disabled = true; }

  // ============================================================ init
  renderSpeed();
  connect();
  refreshRecording();
  setInterval(refreshRecording, 3000);
})();
