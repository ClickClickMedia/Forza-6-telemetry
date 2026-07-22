/* Forza-style thumb-wheel value picker — range-agnostic.
 *
 * A slider is absolute (position = value between min/max) so it can't exist
 * without a range. A wheel is RELATIVE — you scroll from where you are — so it
 * needs no min/max. That's the fix for fields whose range Forza never tells us
 * (springs, aero, ride height): they just free-scroll. Where a real bound
 * exists (0-100%, ARB min 1, camber into minus) we clamp with a soft stop.
 *
 * Each detent lands exactly on a step (2.89, never 2.887) — more precise than a
 * slider. Slow drag = tick-by-tick; a fast flick carries momentum for the long
 * travels (0.5 -> 4.17). Tap the number to type an exact value for a big jump.
 *
 * FHWheel.make(mountEl, cfg, initialValue, onChange) -> { set(v), value() }
 *   cfg = { min, max, step, dec, unit }   (min/max null = free that side)
 */
(function () {
  "use strict";
  const PX = 30;          // pixels between detents
  const SIDE = 12;        // detents rendered each side of centre
  const FRICTION = 0.92;  // momentum decay per frame
  const clampNum = (v, lo, hi) => {
    if (lo != null && v < lo) v = lo;
    if (hi != null && v > hi) v = hi;
    return v;
  };

  function make(mount, cfg, initial, onChange) {
    const step = cfg.step, dec = cfg.dec || 0;
    const q = 1 / step;                                  // step quantiser
    const snap = (v) => Math.round(v * q) / q;
    const clamp = (v) => clampNum(v, cfg.min, cfg.max);
    const fmt = (v) => (Math.abs(v) < 1e-9 ? 0 : v).toFixed(dec);
    let value = clamp(isFinite(initial) ? initial : (cfg.min != null ? cfg.min : 0));

    mount.classList.add("wheel");
    mount.innerHTML =
      '<div class="wh-track"></div><div class="wh-needle"></div>' +
      '<button type="button" class="wh-val" aria-label="tap to type a value"></button>';
    const track = mount.querySelector(".wh-track");
    const valBtn = mount.querySelector(".wh-val");

    function render() {
      const half = mount.clientWidth / 2 || 150;
      const base = Math.round(value * q);               // nearest detent index
      let html = "";
      for (let i = -SIDE; i <= SIDE; i++) {
        const idx = base + i;
        const v = idx / q;
        const x = half + (v - value) * q * PX;
        if (x < -PX || x > mount.clientWidth + PX) continue;
        const oob = (cfg.min != null && v < cfg.min - 1e-9) ||
                    (cfg.max != null && v > cfg.max + 1e-9);
        const major = idx % 5 === 0;
        // only label a major tick when it sits clear of both edges, else the
        // centred label gets clipped by the wheel's overflow ("4.15" -> ".15")
        const lbl = major && !oob && x > 24 && x < mount.clientWidth - 24;
        html += '<i class="wh-t' + (major ? " maj" : "") + (oob ? " oob" : "") +
                '" style="left:' + x.toFixed(1) + 'px">' +
                (lbl ? '<b>' + fmt(v) + "</b>" : "") + "</i>";
      }
      track.innerHTML = html;
      valBtn.textContent = fmt(value) + (cfg.unit ? " " + cfg.unit : "");
    }

    function commit() { onChange(fmt(value)); }

    // ---- drag + momentum ----------------------------------------------------
    let dragging = false, lastX = 0, vel = 0, raf = 0, moved = 0;
    const posX = (e) => (e.touches ? e.touches[0].clientX : e.clientX);

    function down(e) {
      dragging = true; lastX = posX(e); vel = 0; moved = 0;
      cancelAnimationFrame(raf);
      mount.classList.add("dragging");
      if (e.pointerId != null && mount.setPointerCapture) {
        try { mount.setPointerCapture(e.pointerId); } catch (_) {}
      }
      e.preventDefault();
    }
    function move(e) {
      if (!dragging) return;
      const x = posX(e), dx = x - lastX; lastX = x; vel = dx; moved += Math.abs(dx);
      value = clamp(value - dx / PX / q);               // drag left -> value up
      render();
      e.preventDefault();
    }
    function up() {
      if (!dragging) return;
      dragging = false; mount.classList.remove("dragging");
      glide();
    }
    function glide() {
      if (Math.abs(vel) > 0.4) {
        value = clamp(value - vel / PX / q);
        vel *= FRICTION;
        render();
        raf = requestAnimationFrame(glide);
      } else {
        value = clamp(snap(value)); render(); commit();
      }
    }

    // pointer events cover mouse + touch; fall back to touch/mouse pairs
    if (window.PointerEvent) {
      mount.addEventListener("pointerdown", down);
      mount.addEventListener("pointermove", move);
      mount.addEventListener("pointerup", up);
      mount.addEventListener("pointercancel", up);
    } else {
      mount.addEventListener("touchstart", down, { passive: false });
      mount.addEventListener("touchmove", move, { passive: false });
      mount.addEventListener("touchend", up);
      mount.addEventListener("mousedown", down);
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    }

    // ---- tap the number to type an exact value ------------------------------
    valBtn.addEventListener("click", () => {
      if (moved > 4) { moved = 0; return; }   // that was a drag, not a tap
      const inp = document.createElement("input");
      inp.type = "number"; inp.step = String(step); inp.value = fmt(value);
      inp.className = "wh-type"; inp.inputMode = "decimal";
      valBtn.replaceWith(inp); inp.focus(); inp.select();
      const done = (apply) => {
        if (apply) {
          const n = parseFloat(inp.value);
          if (isFinite(n)) { value = clamp(snap(n)); commit(); }
        }
        inp.replaceWith(valBtn); render();
      };
      inp.addEventListener("blur", () => done(true));
      inp.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); inp.blur(); }
        if (ev.key === "Escape") { done(false); }
      });
    });

    const ro = window.ResizeObserver ? new ResizeObserver(render) : null;
    if (ro) ro.observe(mount);
    render();

    return {
      value: () => fmt(value),
      set: (v) => { value = clamp(isFinite(parseFloat(v)) ? parseFloat(v) : value); render(); },
    };
  }

  window.FHWheel = { make };
})();
