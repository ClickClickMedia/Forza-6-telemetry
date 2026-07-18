/* =========================================================================
   app.js — shared helpers for the Forza Horizon 6 telemetry dashboard.
   Vanilla JS, no dependencies. Loaded by every page.
   Exposes helpers on window.FH.
   ========================================================================= */
(function () {
  "use strict";

  // ----------------------------------------------------------------- fetch
  /**
   * JSON fetch wrapper. Returns parsed JSON, throws on non-2xx.
   * @param {string} path  same-origin path, e.g. "/api/status"
   * @param {object} [opts] fetch options; if opts.json is set it is sent as body.
   */
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.json !== undefined) {
      init.method = opts.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    }
    if (opts.headers) Object.assign(init.headers, opts.headers);
    const res = await fetch(path, init);
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || j.error || detail; } catch (e) {}
      throw new Error("HTTP " + res.status + ": " + detail);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  }

  // ------------------------------------------------------------ formatting
  /** Format lap seconds -> "m:ss.mmm". 0/null/NaN -> "--:--.---" */
  function fmtLapTime(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds <= 0) {
      return "--:--.---";
    }
    const m = Math.floor(seconds / 60);
    const s = seconds - m * 60;
    const ss = s.toFixed(3).padStart(6, "0"); // "SS.mmm"
    return m + ":" + ss;
  }

  /** Number formatter with fixed decimals, "-" for null/NaN. */
  function fmt(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    return Number(v).toFixed(digits === undefined ? 0 : digits);
  }

  /** Format a duration in seconds as "Mm Ss" or "Hh Mm". */
  function fmtDuration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "–";
    const s = Math.round(seconds);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return m + "m " + String(r).padStart(2, "0") + "s";
    const h = Math.floor(m / 60), rm = m % 60;
    return h + "h " + String(rm).padStart(2, "0") + "m";
  }

  /** Format an ISO timestamp / epoch to a compact local string. */
  function fmtDate(v) {
    if (!v) return "–";
    let d;
    if (typeof v === "number") d = new Date(v * (v > 1e12 ? 1 : 1000));
    else d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString(undefined, {
      year: "2-digit", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit"
    });
  }

  /** m/s^2 -> g */
  function toG(ms2) { return (ms2 || 0) / 9.81; }

  /** Escape text for safe innerHTML insertion. */
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /** Read a query-string parameter. */
  function qs(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // ------------------------------------------------------------ colour ramp
  /**
   * Map a normalised value 0..1 to an rgb string on a blue->green->red ramp.
   * Used for heatmaps (route colouring, tyre temps, etc).
   */
  function rampRGB(t) {
    t = clamp(t, 0, 1);
    // stops: 0 blue (74,163,255), 0.5 green (55,214,122), 1 red (255,77,79)
    let r, g, b;
    if (t < 0.5) {
      const k = t / 0.5;
      r = 74 + (55 - 74) * k;
      g = 163 + (214 - 163) * k;
      b = 255 + (122 - 255) * k;
    } else {
      const k = (t - 0.5) / 0.5;
      r = 55 + (255 - 55) * k;
      g = 214 + (77 - 214) * k;
      b = 122 + (79 - 122) * k;
    }
    return "rgb(" + Math.round(r) + "," + Math.round(g) + "," + Math.round(b) + ")";
  }

  /** Colour a tyre temperature cell: cold=blue, ideal=green, hot=red (approx 20..120C). */
  function tyreColour(tempC) {
    if (tempC === null || tempC === undefined || !isFinite(tempC)) return "var(--card-2)";
    const t = clamp((tempC - 20) / (120 - 20), 0, 1);
    return rampRGB(t);
  }

  // --------------------------------------------------- canvas: hi-dpi setup
  /** Resize a canvas backing store to match CSS size * devicePixelRatio. */
  function prepCanvas(canvas, cssHeight) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = cssHeight || Math.max(1, Math.floor(rect.height));
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  // --------------------------------------------------- canvas: line chart
  /**
   * Draw one or more line series against a shared x range.
   * @param {HTMLCanvasElement} canvas
   * @param {object} cfg
   *   cfg.series = [{x:[], y:[], color, label, width?}]
   *   cfg.height, cfg.yMin, cfg.yMax (auto if omitted), cfg.title,
   *   cfg.yLabel, cfg.xLabel, cfg.grid (bool), cfg.fill (bool)
   */
  function lineChart(canvas, cfg) {
    cfg = cfg || {};
    const height = cfg.height || 180;
    const { ctx, w, h } = prepCanvas(canvas, height);
    const css = getComputedStyle(document.documentElement);
    const cGrid = "#232c38", cAxis = "#5b6672", cText = "#8b98a9";
    ctx.clearRect(0, 0, w, h);

    const padL = 44, padR = 10, padT = cfg.title ? 22 : 10, padB = 22;
    const plotW = w - padL - padR, plotH = h - padT - padB;

    const series = (cfg.series || []).filter(s => s && s.x && s.x.length);
    // compute ranges
    let xMin = Infinity, xMax = -Infinity, yMin = cfg.yMin, yMax = cfg.yMax;
    let autoY = (yMin === undefined || yMax === undefined);
    if (autoY) { yMin = Infinity; yMax = -Infinity; }
    for (const s of series) {
      for (let i = 0; i < s.x.length; i++) {
        const xv = s.x[i]; if (xv < xMin) xMin = xv; if (xv > xMax) xMax = xv;
        if (autoY) { const yv = s.y[i]; if (isFinite(yv)) { if (yv < yMin) yMin = yv; if (yv > yMax) yMax = yv; } }
      }
    }
    if (!isFinite(xMin)) { drawEmpty(); return; }
    if (autoY) {
      if (!isFinite(yMin)) { yMin = 0; yMax = 1; }
      if (yMin === yMax) { yMin -= 1; yMax += 1; }
      const pad = (yMax - yMin) * 0.08; yMin -= pad; yMax += pad;
    }
    if (xMin === xMax) xMax = xMin + 1;

    const sx = v => padL + (v - xMin) / (xMax - xMin) * plotW;
    const sy = v => padT + (1 - (v - yMin) / (yMax - yMin)) * plotH;

    // title
    if (cfg.title) {
      ctx.fillStyle = cText; ctx.font = "600 12px system-ui,sans-serif";
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(cfg.title, padL, 4);
    }

    // grid + y ticks
    ctx.strokeStyle = cGrid; ctx.lineWidth = 1;
    ctx.fillStyle = cText; ctx.font = "10px " + "ui-monospace,monospace";
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const yv = yMin + (yMax - yMin) * i / ticks;
      const py = sy(yv);
      if (cfg.grid !== false) { ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(w - padR, py); ctx.stroke(); }
      ctx.fillText(shortNum(yv), padL - 6, py);
    }
    // x labels (start / end)
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(shortNum(xMin), padL, h - padB + 4);
    ctx.fillText(shortNum(xMax), w - padR, h - padB + 4);

    // baseline axis
    ctx.strokeStyle = cAxis; ctx.beginPath();
    ctx.moveTo(padL, padT); ctx.lineTo(padL, padT + plotH); ctx.lineTo(padL + plotW, padT + plotH); ctx.stroke();

    // series lines
    for (const s of series) {
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < s.x.length; i++) {
        const yv = s.y[i];
        if (!isFinite(yv)) { started = false; continue; }
        const px = sx(s.x[i]), py = sy(yv);
        if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
      }
      ctx.lineWidth = s.width || 1.6;
      ctx.strokeStyle = s.color || "#37d67a";
      ctx.lineJoin = "round";
      ctx.stroke();
    }

    function drawEmpty() {
      ctx.fillStyle = cText; ctx.font = "12px system-ui,sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("No data", w / 2, h / 2);
    }
  }

  function shortNum(v) {
    if (!isFinite(v)) return "";
    const a = Math.abs(v);
    if (a >= 1000) return (v / 1000).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a === 0) return "0";
    return v.toFixed(2);
  }

  // --------------------------------------------------- canvas: route/heatmap
  /**
   * Draw an XY route coloured per-point by a channel value.
   * @param {HTMLCanvasElement} canvas
   * @param {object} route  {x:[], z:[], c:[]}  (c optional -> mapped to ramp)
   * @param {object} [opts]  {height, cMin, cMax, lineWidth, colorFn, multi}
   *   If opts.multi is an array of {x,z,color} it draws several plain routes.
   */
  function routeChart(canvas, route, opts) {
    opts = opts || {};
    const height = opts.height || 320;
    const { ctx, w, h } = prepCanvas(canvas, height);
    ctx.clearRect(0, 0, w, h);
    const pad = 16;

    // gather all points to compute bounds (supports single + multi)
    const groups = [];
    if (opts.multi && opts.multi.length) {
      for (const g of opts.multi) if (g.x && g.x.length) groups.push(g);
    } else if (route && route.x && route.x.length) {
      groups.push({ x: route.x, z: route.z, c: route.c });
    }
    if (!groups.length) {
      ctx.fillStyle = "#8b98a9"; ctx.font = "12px system-ui,sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("No route data", w / 2, h / 2);
      return;
    }

    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const g of groups) for (let i = 0; i < g.x.length; i++) {
      const x = g.x[i], z = g.z[i];
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    }
    let rngX = maxX - minX || 1, rngZ = maxZ - minZ || 1;
    // keep aspect ratio square-ish
    const scale = Math.min((w - 2 * pad) / rngX, (h - 2 * pad) / rngZ);
    const offX = (w - rngX * scale) / 2, offZ = (h - rngZ * scale) / 2;
    const px = x => offX + (x - minX) * scale;
    const pz = z => offZ + (maxZ - z) * scale; // flip Z so it reads like a map

    if (opts.multi && opts.multi.length) {
      // plain coloured routes (one colour each)
      for (const g of groups) {
        ctx.beginPath();
        for (let i = 0; i < g.x.length; i++) {
          const X = px(g.x[i]), Z = pz(g.z[i]);
          if (i === 0) ctx.moveTo(X, Z); else ctx.lineTo(X, Z);
        }
        ctx.strokeStyle = g.color || "#37d67a";
        ctx.lineWidth = opts.lineWidth || 2.4;
        ctx.lineJoin = "round"; ctx.lineCap = "round";
        ctx.stroke();
      }
      return;
    }

    // single route, coloured per segment by channel value
    const g = groups[0];
    const c = g.c || [];
    let cMin = opts.cMin, cMax = opts.cMax;
    if (cMin === undefined || cMax === undefined) {
      cMin = Infinity; cMax = -Infinity;
      for (const v of c) { if (isFinite(v)) { if (v < cMin) cMin = v; if (v > cMax) cMax = v; } }
      if (!isFinite(cMin)) { cMin = 0; cMax = 1; }
      if (cMin === cMax) cMax = cMin + 1;
    }
    const colorFn = opts.colorFn || (t => rampRGB(t));
    ctx.lineWidth = opts.lineWidth || 3;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    for (let i = 1; i < g.x.length; i++) {
      const v = c.length ? c[i] : 0.5;
      const t = c.length ? clamp((v - cMin) / (cMax - cMin), 0, 1) : 0.5;
      ctx.beginPath();
      ctx.moveTo(px(g.x[i - 1]), pz(g.z[i - 1]));
      ctx.lineTo(px(g.x[i]), pz(g.z[i]));
      ctx.strokeStyle = colorFn(t);
      ctx.stroke();
    }
    // start marker
    ctx.fillStyle = "#e6edf3";
    ctx.beginPath(); ctx.arc(px(g.x[0]), pz(g.z[0]), 4, 0, Math.PI * 2); ctx.fill();
  }

  // --------------------------------------------------- canvas: bar chart
  /** Simple vertical bar chart from {labels:[], values:[]}. */
  function barChart(canvas, data, opts) {
    opts = opts || {};
    const height = opts.height || 160;
    const { ctx, w, h } = prepCanvas(canvas, height);
    ctx.clearRect(0, 0, w, h);
    const labels = data.labels || [], values = data.values || [];
    if (!values.length) return;
    const padL = 8, padR = 8, padT = 8, padB = 22;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const maxV = Math.max.apply(null, values.concat([0.0001]));
    const n = values.length;
    const gap = 6;
    const bw = (plotW - gap * (n - 1)) / n;
    ctx.font = "10px ui-monospace,monospace"; ctx.textBaseline = "top";
    for (let i = 0; i < n; i++) {
      const x = padL + i * (bw + gap);
      const bh = (values[i] / maxV) * plotH;
      const y = padT + plotH - bh;
      ctx.fillStyle = opts.color || "#37d67a";
      ctx.fillRect(x, y, bw, bh);
      ctx.fillStyle = "#8b98a9"; ctx.textAlign = "center";
      ctx.fillText(String(labels[i]), x + bw / 2, padT + plotH + 5);
    }
  }

  // --------------------------------------------------- service worker
  function registerSW() {
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", function () {
        navigator.serviceWorker.register("/sw.js").catch(function (e) {
          console.warn("SW registration failed:", e);
        });
      });
    }
  }

  // --------------------------------------------------- nav active state
  function markActiveNav() {
    const path = window.location.pathname;
    document.querySelectorAll(".nav a.navlink").forEach(function (a) {
      const href = a.getAttribute("href");
      const active = (href === "/" && path === "/") || (href !== "/" && path.indexOf(href) === 0);
      if (active) a.classList.add("active");
    });
  }

  // expose
  window.FH = {
    api, fmtLapTime, fmt, fmtDuration, fmtDate, toG, esc, qs, clamp,
    rampRGB, tyreColour, prepCanvas, lineChart, routeChart, barChart,
    registerSW, markActiveNav
  };

  document.addEventListener("DOMContentLoaded", markActiveNav);
})();
