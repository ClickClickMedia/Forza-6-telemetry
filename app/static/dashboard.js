/* =========================================================================
   dashboard.js — live dashboard logic.
   - Reconnecting WebSocket (exponential backoff) to /ws/live
   - Screen Wake Lock toggle (graceful when unsupported)
   - Renders speed/gear/rpm gauges, input bars, tyres, slip, suspension
   - Recording controls (start/stop/marker) + status
   - Registers the service worker (PWA install)
   ========================================================================= */
(function () {
  "use strict";
  const { api, fmtLapTime, fmt, toG, esc, clamp, tyreColour, rampRGB } = window.FH;

  // Register service worker so the app is installable / offline-capable.
  window.FH.registerSW();

  // ------------------------------------------------------------- element refs
  const $ = (id) => document.getElementById(id);
  const el = {
    dot: $("connDot"), connText: $("connText"), pps: $("pps"),
    raceOn: $("raceOn"),
    speed: $("speed"), gear: $("gear"),
    rpm: $("rpm"), rpmMax: $("rpmMax"), rpmArc: $("rpmArc"), rpmPctFill: $("rpmPctFill"), rpmPctText: $("rpmPctText"),
    throttle: $("throttle"), brake: $("brake"), clutch: $("clutch"), handbrake: $("handbrake"),
    throttleV: $("throttleV"), brakeV: $("brakeV"), clutchV: $("clutchV"), handbrakeV: $("handbrakeV"),
    steerFill: $("steerFill"), steerV: $("steerV"),
    powerKw: $("powerKw"), torqueNm: $("torqueNm"), boostBar: $("boostBar"), boostPsi: $("boostPsi"),
    curLap: $("curLap"), lastLap: $("lastLap"), bestLap: $("bestLap"), lapNum: $("lapNum"),
    accelLong: $("accelLong"), accelLat: $("accelLat"), yaw: $("yaw"),
    car: $("carInfo"),
    // recording
    recPill: $("recPill"), recFrames: $("recFrames"),
    btnStart: $("btnStart"), btnStop: $("btnStop"), btnMarker: $("btnMarker"),
    // wake lock
    btnWake: $("btnWake"), wakeState: $("wakeState")
  };
  const tyreCells = {
    temp: qsa(".tyre-temp"), slipR: qsa(".tyre-slipr"), slipA: qsa(".tyre-slipa"),
    comb: qsa(".tyre-comb"), susp: qsa(".tyre-susp")
  };
  function qsa(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  // ============================================================ WebSocket ==
  let ws = null;
  let backoff = 500;          // ms, grows to a cap
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
    try {
      ws = new WebSocket(wsURL());
    } catch (e) {
      scheduleReconnect();
      return;
    }
    ws.onopen = function () {
      everConnected = true;
      backoff = 500;               // reset backoff on success
      setConn("open");
    };
    ws.onmessage = function (ev) {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.status) applyStatus(msg.status);
      if (msg.type === "telemetry") applyData(msg.data);
      // "hello" also carries status which we already applied.
    };
    ws.onclose = function () { setConn("closed"); scheduleReconnect(); };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(BACKOFF_MAX, Math.round(backoff * 1.7));
  }

  function setConn(state) {
    el.dot.className = "dot";
    if (state === "open") { el.dot.classList.add("ok"); el.connText.textContent = "Live"; }
    else if (state === "connecting") { el.dot.classList.add("warn"); el.connText.textContent = "Connecting…"; }
    else { el.dot.classList.add("bad"); el.connText.textContent = everConnected ? "Reconnecting…" : "Offline"; }
  }

  // ============================================================ rendering ==
  function applyStatus(s) {
    el.pps.textContent = fmt(s.pps, 0) + " pps";
    // If the receiver reports connected but websocket says otherwise, dot reflects WS.
    if (s.connected === false && el.dot.classList.contains("ok")) {
      // WS is up but no game packets flowing
      el.dot.className = "dot warn";
      el.connText.textContent = "No packets";
    }
  }

  function setBar(fillEl, pct) {
    fillEl.style.width = clamp(pct || 0, 0, 100).toFixed(1) + "%";
  }

  let lastRaceOn = null;
  function applyData(d) {
    if (!d) {
      el.raceOn.textContent = "Waiting for packets…";
      el.raceOn.className = "pill idle";
      return;
    }

    // race state
    const on = d.race_on === 1 || d.race_on === true;
    if (on !== lastRaceOn) {
      el.raceOn.textContent = on ? "RACE ON" : "PAUSED";
      el.raceOn.className = "pill " + (on ? "" : "idle");
      lastRaceOn = on;
    }

    // hero
    el.speed.textContent = fmt(d.speed_kmh, 0);
    el.gear.textContent = d.gear != null ? d.gear : "–";

    // rpm
    el.rpm.textContent = fmt(d.rpm, 0);
    el.rpmMax.textContent = fmt(d.rpm_max, 0);
    const rpmPct = clamp(d.rpm_pct || 0, 0, 100);
    el.rpmPctText.textContent = fmt(rpmPct, 0) + "%";
    setBar(el.rpmPctFill, rpmPct);
    // redline warning colour
    el.rpmPctFill.style.background = rpmPct > 92 ? "var(--danger)"
      : rpmPct > 80 ? "var(--accent-2)" : "var(--accent)";
    drawRpmArc(rpmPct);

    // input bars
    setBar(el.throttle, d.throttle); el.throttleV.textContent = fmt(d.throttle, 0) + "%";
    setBar(el.brake, d.brake); el.brakeV.textContent = fmt(d.brake, 0) + "%";
    setBar(el.clutch, d.clutch); el.clutchV.textContent = fmt(d.clutch, 0) + "%";
    setBar(el.handbrake, d.handbrake); el.handbrakeV.textContent = fmt(d.handbrake, 0) + "%";

    // steering (-1..1) centered
    const st = clamp(d.steer || 0, -1, 1);
    el.steerFill.style.width = (Math.abs(st) * 50).toFixed(1) + "%";
    if (st >= 0) { el.steerFill.style.left = "50%"; el.steerFill.style.right = "auto"; }
    else { el.steerFill.style.left = "auto"; el.steerFill.style.right = "50%"; }
    el.steerV.textContent = st.toFixed(2);

    // power / torque / boost
    el.powerKw.textContent = fmt(d.power_kw, 0);
    el.torqueNm.textContent = fmt(d.torque_nm, 0);
    el.boostBar.textContent = fmt(d.boost_bar, 2);
    el.boostPsi.textContent = fmt(d.boost_psi, 1) + " psi";

    // laps
    el.curLap.textContent = fmtLapTime(d.cur_lap);
    el.lastLap.textContent = fmtLapTime(d.last_lap);
    el.bestLap.textContent = fmtLapTime(d.best_lap);
    el.lapNum.textContent = d.lap_number != null ? d.lap_number : "–";

    // acceleration (m/s^2 -> g)
    el.accelLong.textContent = toG(d.accel_long).toFixed(2);
    el.accelLat.textContent = toG(d.accel_lat).toFixed(2);
    el.yaw.textContent = fmt(d.yaw_rate, 2);

    // tyres 2x2 [FL,FR,RL,RR]
    updateQuad(tyreCells.temp, d.tire_temp, (v) => fmt(v, 0) + "°", (v) => tyreColour(v), true);
    updateQuad(tyreCells.slipR, d.slip_ratio, (v) => fmt(v, 2), slipColour);
    updateQuad(tyreCells.slipA, d.slip_angle, (v) => fmt(v, 2), slipColour);
    updateQuad(tyreCells.comb, d.combined_slip, (v) => fmt(v, 2), combColour);
    updateQuad(tyreCells.susp, d.susp_norm, (v) => fmt(v, 2), suspColour);

    // car info
    if (d.car_ordinal != null) {
      el.car.textContent = [
        "#" + d.car_ordinal,
        d.car_class || "",
        d.pi != null ? "PI " + d.pi : "",
        d.drivetrain || "",
        d.cylinders != null ? d.cylinders + "cyl" : ""
      ].filter(Boolean).join(" · ");
    }
  }

  function updateQuad(cells, arr, fmtFn, colourFn, isTemp) {
    if (!arr) return;
    for (let i = 0; i < cells.length && i < arr.length; i++) {
      const numEl = cells[i].querySelector(".num");
      numEl.textContent = fmtFn(arr[i]);
      if (colourFn) {
        const col = colourFn(arr[i]);
        if (isTemp) { cells[i].style.background = col; numEl.style.color = "#0d1117"; }
        else { numEl.style.color = col; }
      }
    }
  }

  // slip ratio / angle -> colour (0 good green, high red)
  function slipColour(v) { return rampRGB(clamp(Math.abs(v) / 1.0, 0, 1)); }
  function combColour(v) { return rampRGB(clamp(Math.abs(v) / 1.5, 0, 1)); } // >1 = losing grip
  function suspColour(v) {
    // near 0 (full extension) or near 1 (bottom-out) is notable
    const t = clamp(v, 0, 1);
    const edge = Math.max(0.15 - t, t - 0.85); // >0 near an edge
    return edge > 0 ? "var(--warn)" : "var(--text)";
  }

  // ------------------------------------------------ RPM arc (canvas gauge)
  const rpmCanvas = el.rpmArc;
  function drawRpmArc(pct) {
    if (!rpmCanvas) return;
    const { ctx, w, h } = window.FH.prepCanvas(rpmCanvas, rpmCanvas.clientHeight || 120);
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h * 0.92, r = Math.min(w / 2, h) * 0.82;
    const start = Math.PI, end = 2 * Math.PI; // top half sweep 180deg
    // track
    ctx.lineWidth = Math.max(8, r * 0.14);
    ctx.lineCap = "round";
    ctx.strokeStyle = "#232c38";
    ctx.beginPath(); ctx.arc(cx, cy, r, start, end); ctx.stroke();
    // fill
    const frac = clamp(pct, 0, 100) / 100;
    const ang = start + (end - start) * frac;
    ctx.strokeStyle = pct > 92 ? "#ff4d4f" : pct > 80 ? "#ff8a1e" : "#37d67a";
    ctx.beginPath(); ctx.arc(cx, cy, r, start, ang); ctx.stroke();
    // redline tick
    ctx.strokeStyle = "#ff4d4f"; ctx.lineWidth = 3;
    const rl = start + (end - start) * 0.92;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(rl) * (r - r * 0.16), cy + Math.sin(rl) * (r - r * 0.16));
    ctx.lineTo(cx + Math.cos(rl) * (r + r * 0.05), cy + Math.sin(rl) * (r + r * 0.05));
    ctx.stroke();
  }

  // ============================================================ recording ==
  async function refreshRecording() {
    try {
      const r = await api("/api/recording/status");
      renderRecording(r);
    } catch (e) { /* ignore transient */ }
  }
  function renderRecording(r) {
    const rec = !!r.recording;
    el.recPill.textContent = rec ? "REC" : "IDLE";
    el.recPill.className = "pill " + (rec ? "rec" : "idle");
    el.recFrames.textContent = rec
      ? (fmt(r.frame_count, 0) + " frames" + (r.session_id ? " · #" + r.session_id : ""))
      : "not recording";
    el.btnStart.disabled = rec;
    el.btnStop.disabled = !rec;
    el.btnMarker.disabled = !rec;
  }

  async function doStart() {
    setBusy(el.btnStart, true);
    try { renderRecording(await api("/api/recording/start", { json: {} })); }
    catch (e) { toast("Start failed: " + e.message); }
    finally { setBusy(el.btnStart, false); }
  }
  async function doStop() {
    setBusy(el.btnStop, true);
    try { renderRecording(await api("/api/recording/stop", { json: {} })); }
    catch (e) { toast("Stop failed: " + e.message); }
    finally { setBusy(el.btnStop, false); }
  }
  async function doMarker() {
    const label = prompt("Marker label:", "marker");
    if (label === null) return;
    setBusy(el.btnMarker, true);
    try { await api("/api/recording/marker", { json: { label: label || "marker" } }); toast("Marker added"); }
    catch (e) { toast("Marker failed: " + e.message); }
    finally { setBusy(el.btnMarker, false); }
  }
  function setBusy(btn, busy) { btn.disabled = busy; }

  // tiny transient toast
  let toastTimer = null;
  function toast(msg) {
    let t = $("toast");
    if (!t) {
      t = document.createElement("div"); t.id = "toast";
      t.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);" +
        "background:#161d27;border:1px solid #232c38;color:#e6edf3;padding:10px 16px;" +
        "border-radius:10px;font-size:13px;z-index:100;box-shadow:0 6px 20px rgba(0,0,0,.4);";
      document.body.appendChild(t);
    }
    t.textContent = msg; t.style.opacity = "1";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, 2200);
  }

  // ============================================================ wake lock ==
  let wakeLock = null;
  let wakeWanted = false;
  const wakeSupported = ("wakeLock" in navigator);

  async function requestWake() {
    if (!wakeSupported) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => {
        // OS released it (e.g. tab hidden); reflect state but keep intent.
        if (wakeWanted) el.wakeState.textContent = "paused";
      });
      el.wakeState.textContent = "on";
      el.btnWake.classList.add("on");
    } catch (e) {
      el.wakeState.textContent = "error";
    }
  }
  async function releaseWake() {
    try { if (wakeLock) await wakeLock.release(); } catch (e) {}
    wakeLock = null;
    el.wakeState.textContent = "off";
    el.btnWake.classList.remove("on");
  }
  function toggleWake() {
    if (!wakeSupported) { toast("Wake Lock not supported on this device"); return; }
    wakeWanted = !wakeWanted;
    if (wakeWanted) requestWake(); else releaseWake();
  }
  // Re-acquire when returning to the tab (locks auto-release on hide).
  document.addEventListener("visibilitychange", () => {
    if (wakeWanted && document.visibilityState === "visible" && !wakeLock) requestWake();
  });

  function initWake() {
    if (!wakeSupported) {
      el.wakeState.textContent = "unsupported";
      el.btnWake.disabled = true;
      el.btnWake.title = "Screen Wake Lock API not available";
    } else {
      el.wakeState.textContent = "off";
    }
  }

  // ============================================================ init ======
  function init() {
    el.btnStart.addEventListener("click", doStart);
    el.btnStop.addEventListener("click", doStop);
    el.btnMarker.addEventListener("click", doMarker);
    el.btnWake.addEventListener("click", toggleWake);
    initWake();
    connect();
    refreshRecording();
    setInterval(refreshRecording, 3000);
    // redraw rpm arc on resize for crisp hi-dpi
    window.addEventListener("resize", () => drawRpmArc(0));
    drawRpmArc(0);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
