/* =========================================================================
   app.js — shared foundation for the FH6 telemetry dashboard.
   Vanilla JS, no dependencies. Loaded by every page.

   Provides on window.FH:
     - shell(active, opts)  : injects the shared chrome (top bar + tab bar)
     - api()                : JSON fetch wrapper
     - settings             : persisted unit prefs (°C/°F, km/h/mph)
     - ema()/emaArray()     : display smoothing for noisy channels
     - tempClass()/rampRGB(): the one functional colour language
     - formatters, canvas charts (palette-aware), toast, clipboard
   ========================================================================= */
(function () {
  "use strict";

  // ----------------------------------------------------------------- fetch
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

  // -------------------------------------------------------------- settings
  const settings = {
    _key: "fh6.settings",
    _cache: null,
    _load() {
      if (this._cache) return this._cache;
      try { this._cache = JSON.parse(localStorage.getItem(this._key)) || {}; }
      catch (e) { this._cache = {}; }
      return this._cache;
    },
    get(name, fallback) {
      const v = this._load()[name];
      return v === undefined ? fallback : v;
    },
    set(name, value) {
      const s = this._load();
      s[name] = value;
      try { localStorage.setItem(this._key, JSON.stringify(s)); } catch (e) {}
    }
  };

  // ------------------------------------------------------------ formatting
  function fmtLapTime(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds <= 0) {
      return "--:--.---";
    }
    const m = Math.floor(seconds / 60);
    const s = seconds - m * 60;
    return m + ":" + s.toFixed(3).padStart(6, "0");
  }

  function fmt(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    return Number(v).toFixed(digits === undefined ? 0 : digits);
  }

  function fmtDuration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "–";
    const s = Math.round(seconds);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return m + "m " + String(r).padStart(2, "0") + "s";
    const h = Math.floor(m / 60), rm = m % 60;
    return h + "h " + String(rm).padStart(2, "0") + "m";
  }

  function fmtDate(v) {
    if (!v) return "–";
    let d;
    if (typeof v === "number") d = new Date(v * (v > 1e12 ? 1 : 1000));
    else d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleString(undefined, {
      month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit"
    });
  }

  function toG(ms2) { return (ms2 || 0) / 9.81; }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function fmtBytes(n) {
    if (!isFinite(n) || n <= 0) return "0 MB";
    if (n < 1e6) return (n / 1e3).toFixed(0) + " kB";
    if (n < 1e9) return (n / 1e6).toFixed(1) + " MB";
    return (n / 1e9).toFixed(2) + " GB";
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function qs(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  // Unit-aware speed/temp. Wire truth is km/h and °C by the time it reaches
  // the browser; these convert for display only.
  function speedOut(kmh) {
    if (settings.get("speedUnit", "kmh") === "mph") {
      return { v: kmh * 0.621371, unit: "mph" };
    }
    return { v: kmh, unit: "km/h" };
  }
  function tempOut(c) {
    if (settings.get("tempUnit", "c") === "f") {
      return { v: c * 9 / 5 + 32, unit: "°F" };
    }
    return { v: c, unit: "°C" };
  }

  // ----------------------------------------------------- display smoothing
  // Exponential moving average for *display* only — recordings always keep
  // raw frames. alpha is per-update weight of the new sample.
  function ema(alpha) {
    let value = null;
    const fn = (x) => {
      if (!isFinite(x)) return value === null ? 0 : value;
      value = value === null ? x : value + alpha * (x - value);
      return value;
    };
    fn.reset = () => { value = null; };
    return fn;
  }
  function emaArray(alpha, n) {
    const fns = Array.from({ length: n }, () => ema(alpha));
    const fn = (arr) => fns.map((f, i) => f(arr && isFinite(arr[i]) ? arr[i] : NaN));
    fn.reset = () => fns.forEach(f => f.reset());
    return fn;
  }

  // ------------------------------------------------------- colour language
  // Tyre working window (°C), community-calibrated: optimal 88–99, usable
  // 77–121. cold < 77 · in-window 77–99 · hot 99–121 · over > 121.
  // Keep aligned with TEMP_COLD_C/TEMP_HOT_C in app/laps.py.
  const TEMP = { cold: 77, hot: 99, over: 121 };

  function tempClass(c) {
    if (!isFinite(c)) return "cold";
    if (c < TEMP.cold) return "cold";
    if (c <= TEMP.hot) return "optimal";
    if (c <= TEMP.over) return "hot";
    return "over";
  }

  // 0..1 → cold → optimal → hot → over (route/heatmap ramp).
  const RAMP_STOPS = [
    [0.00, [79, 168, 255]],
    [0.45, [59, 217, 138]],
    [0.75, [255, 174, 0]],
    [1.00, [255, 77, 66]]
  ];
  function rampRGB(t) {
    t = clamp(t, 0, 1);
    for (let i = 1; i < RAMP_STOPS.length; i++) {
      const [t1, c1] = RAMP_STOPS[i];
      const [t0, c0] = RAMP_STOPS[i - 1];
      if (t <= t1) {
        const k = (t - t0) / (t1 - t0 || 1);
        return "rgb(" + c0.map((a, j) => Math.round(a + (c1[j] - a) * k)).join(",") + ")";
      }
    }
    return "rgb(255,77,66)";
  }

  // ------------------------------------------------------------ palette
  let _pal = null;
  function palette() {
    if (_pal) return _pal;
    const css = getComputedStyle(document.documentElement);
    const v = (name, fb) => (css.getPropertyValue(name) || fb).trim() || fb;
    _pal = {
      line: v("--line", "#372F25"),
      inkMute: v("--ink-mute", "#6E6557"),
      inkDim: v("--ink-dim", "#9A8F7D"),
      paint: v("--paint", "#F2EDE3"),
      amber: v("--amber", "#FFAE00"),
      optimal: v("--optimal", "#3BD98A"),
      over: v("--over", "#FF4D42"),
      cold: v("--cold", "#4FA8FF"),
      bg2: v("--asphalt-2", "#1B1712")
    };
    return _pal;
  }

  // --------------------------------------------------- canvas: hi-dpi setup
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
  function lineChart(canvas, cfg) {
    cfg = cfg || {};
    const P = palette();
    const height = cfg.height || 180;
    const { ctx, w, h } = prepCanvas(canvas, height);
    ctx.clearRect(0, 0, w, h);

    const padL = 44, padR = 10, padT = cfg.title ? 22 : 10, padB = 22;
    const plotW = w - padL - padR, plotH = h - padT - padB;

    const series = (cfg.series || []).filter(s => s && s.x && s.x.length);
    let xMin = Infinity, xMax = -Infinity, yMin = cfg.yMin, yMax = cfg.yMax;
    const autoY = (yMin === undefined || yMax === undefined);
    if (autoY) { yMin = Infinity; yMax = -Infinity; }
    for (const s of series) {
      for (let i = 0; i < s.x.length; i++) {
        const xv = s.x[i]; if (xv < xMin) xMin = xv; if (xv > xMax) xMax = xv;
        if (autoY) { const yv = s.y[i]; if (isFinite(yv)) { if (yv < yMin) yMin = yv; if (yv > yMax) yMax = yv; } }
      }
    }
    if (!isFinite(xMin)) {
      ctx.fillStyle = P.inkMute; ctx.font = "12px " + "system-ui,sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("No data", w / 2, h / 2);
      return;
    }
    if (autoY) {
      if (!isFinite(yMin)) { yMin = 0; yMax = 1; }
      if (yMin === yMax) { yMin -= 1; yMax += 1; }
      const pad = (yMax - yMin) * 0.08; yMin -= pad; yMax += pad;
    }
    if (xMin === xMax) xMax = xMin + 1;

    const sx = v => padL + (v - xMin) / (xMax - xMin) * plotW;
    const sy = v => padT + (1 - (v - yMin) / (yMax - yMin)) * plotH;

    if (cfg.title) {
      ctx.fillStyle = P.inkDim; ctx.font = "700 11px ui-monospace,monospace";
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(cfg.title.toUpperCase(), padL, 4);
    }

    ctx.strokeStyle = P.line; ctx.lineWidth = 1;
    ctx.fillStyle = P.inkMute; ctx.font = "10px ui-monospace,monospace";
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const yv = yMin + (yMax - yMin) * i / ticks;
      const py = sy(yv);
      if (cfg.grid !== false) { ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(w - padR, py); ctx.stroke(); }
      ctx.fillText(shortNum(yv), padL - 6, py);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(shortNum(xMin), padL, h - padB + 4);
    ctx.fillText(shortNum(xMax), w - padR, h - padB + 4);

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
      ctx.strokeStyle = s.color || P.amber;
      ctx.lineJoin = "round";
      ctx.stroke();
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

  // --------------------------------------------------- canvas: route trace
  function routeChart(canvas, route, opts) {
    opts = opts || {};
    const P = palette();
    const height = opts.height || 320;
    const { ctx, w, h } = prepCanvas(canvas, height);
    ctx.clearRect(0, 0, w, h);
    const pad = 16;

    const groups = [];
    if (opts.multi && opts.multi.length) {
      for (const g of opts.multi) if (g.x && g.x.length) groups.push(g);
    } else if (route && route.x && route.x.length) {
      groups.push({ x: route.x, z: route.z, c: route.c });
    }
    if (!groups.length) {
      ctx.fillStyle = P.inkMute; ctx.font = "12px system-ui,sans-serif";
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
    const rngX = maxX - minX || 1, rngZ = maxZ - minZ || 1;
    const scale = Math.min((w - 2 * pad) / rngX, (h - 2 * pad) / rngZ);
    const offX = (w - rngX * scale) / 2, offZ = (h - rngZ * scale) / 2;
    const px = x => offX + (x - minX) * scale;
    const pz = z => offZ + (maxZ - z) * scale;

    // Teleports (event staging, fast travel) create huge position jumps
    // that would draw as long straight lines across the map — lift the pen
    // over any implausible jump instead. 400 m between consecutive
    // downsampled points is far beyond real driving.
    const JUMP_M = 400;

    if (opts.multi && opts.multi.length) {
      for (const g of groups) {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < g.x.length; i++) {
          const X = px(g.x[i]), Z = pz(g.z[i]);
          if (i > 0 && Math.hypot(g.x[i] - g.x[i - 1], g.z[i] - g.z[i - 1]) > JUMP_M) {
            started = false;
          }
          if (!started) { ctx.moveTo(X, Z); started = true; }
          else ctx.lineTo(X, Z);
        }
        ctx.strokeStyle = g.color || P.amber;
        ctx.lineWidth = opts.lineWidth || 2.4;
        ctx.lineJoin = "round"; ctx.lineCap = "round";
        ctx.stroke();
      }
      return;
    }

    const g = groups[0];
    const c = g.c || [];
    let cMin = opts.cMin, cMax = opts.cMax;
    if (cMin === undefined || cMax === undefined) {
      cMin = Infinity; cMax = -Infinity;
      for (const v of c) { if (isFinite(v)) { if (v < cMin) cMin = v; if (v > cMax) cMax = v; } }
      if (!isFinite(cMin)) { cMin = 0; cMax = 1; }
      if (cMin === cMax) cMax = cMin + 1;
    }
    ctx.lineWidth = opts.lineWidth || 3;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    for (let i = 1; i < g.x.length; i++) {
      if (Math.hypot(g.x[i] - g.x[i - 1], g.z[i] - g.z[i - 1]) > JUMP_M) {
        continue; // teleport — don't draw a line across the map
      }
      const v = c.length ? c[i] : 0.5;
      const t = c.length ? clamp((v - cMin) / (cMax - cMin), 0, 1) : 0.5;
      ctx.beginPath();
      ctx.moveTo(px(g.x[i - 1]), pz(g.z[i - 1]));
      ctx.lineTo(px(g.x[i]), pz(g.z[i]));
      ctx.strokeStyle = rampRGB(t);
      ctx.stroke();
    }
    ctx.fillStyle = P.paint;
    ctx.beginPath(); ctx.arc(px(g.x[0]), pz(g.z[0]), 4, 0, Math.PI * 2); ctx.fill();
  }

  // --------------------------------------------------- canvas: bar chart
  function barChart(canvas, data, opts) {
    opts = opts || {};
    const P = palette();
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
      ctx.fillStyle = opts.color || P.amber;
      ctx.fillRect(x, y, bw, bh);
      ctx.fillStyle = P.inkMute; ctx.textAlign = "center";
      ctx.fillText(String(labels[i]), x + bw / 2, padT + plotH + 5);
    }
  }

  // ------------------------------------------------------------ clipboard
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      // Insecure-context fallback (plain http on the LAN).
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;opacity:0;";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e2) {}
      ta.remove();
      return ok;
    }
  }

  // ---------------------------------------------------------------- toast
  let toastTimer = null;
  function toast(msg) {
    let t = document.getElementById("toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2400);
  }

  // --------------------------------------------------------------- shell
  const NAV = [
    { href: "/", key: "live", label: "Live",
      icon: '<circle cx="12" cy="13" r="8.5"/><path d="M12 13l4.2-4.2"/><path d="M5.5 16.5h-1M19.5 16.5h-1"/>' },
    { href: "/sessions", key: "sessions", label: "Sessions",
      icon: '<path d="M4 6h16M4 12h16M4 18h9"/>' },
    { href: "/analysis", key: "analysis", label: "Analysis",
      icon: '<path d="M4 5v14h16"/><path d="M7 15l3.5-5 3 3L18 7"/>' },
    { href: "/compare", key: "compare", label: "Compare",
      icon: '<circle cx="9.5" cy="12" r="5.5"/><circle cx="14.5" cy="12" r="5.5"/>' },
    { href: "/debug", key: "debug", label: "Debug",
      icon: '<rect x="8" y="8" width="8" height="8" rx="1.5"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.5 5.5L8 8M18.5 5.5L16 8M5.5 18.5L8 16M18.5 18.5L16 16"/>' }
  ];

  function shell(active, opts) {
    opts = opts || {};
    const links = NAV.map(n =>
      '<a href="' + n.href + '"' + (n.key === active ? ' class="active"' : "") + ">" + n.label + "</a>"
    ).join("");
    const status = opts.status
      ? '<span class="status"><span id="connDot" class="dot"></span>' +
        '<span id="connText">Offline</span><span id="pps" class="pps">0 pps</span></span>'
      : (opts.statusRight || "");

    const top = document.createElement("header");
    top.className = "topbar";
    top.innerHTML =
      '<span class="brand"><span class="mark" aria-hidden="true"></span>' +
      'FH<span class="six">6</span>&nbsp;TELEMETRY</span>' +
      '<nav class="navlinks">' + links + "</nav>" +
      '<span class="spacer"></span>' + status;
    document.body.prepend(top);

    const tabs = document.createElement("nav");
    tabs.className = "tabbar";
    tabs.setAttribute("aria-label", "Pages");
    tabs.innerHTML = NAV.map(n =>
      '<a href="' + n.href + '"' + (n.key === active ? ' class="active"' : "") +
      ' aria-label="' + n.label + '"><svg viewBox="0 0 24 24" aria-hidden="true">' +
      n.icon + "</svg><span>" + n.label + "</span></a>"
    ).join("");
    document.body.appendChild(tabs);
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

  // expose
  window.FH = {
    api, settings,
    fmtLapTime, fmt, fmtDuration, fmtDate, toG, esc, qs, clamp,
    speedOut, tempOut, ema, emaArray,
    TEMP, tempClass, rampRGB, palette,
    prepCanvas, lineChart, routeChart, barChart,
    copyText, toast, shell, registerSW, fmtBytes
  };
})();
