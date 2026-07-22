/* Forza-style thumb-wheel value picker — range-agnostic, with a null state.
 *
 * A slider is absolute (position = value between min/max) so it can't exist
 * without a range. A wheel is RELATIVE — you scroll from where you are — so it
 * needs no min/max. That's the fix for fields whose range Forza never tells us
 * (springs, aero, ride height): they just free-scroll. Where a real bound
 * exists (0-100%, ARB min 1, camber into minus) we clamp with a soft stop.
 *
 * UNSET is a first-class state. A dial you have not touched reads "—" and is
 * dimmed; it exports "" so the report says the field wasn't provided rather
 * than inventing a value. Turning the dial wakes it at `home` (mid-range);
 * tapping the readout lets you type an exact value; clearing the box returns
 * it to unset.
 *
 * Each detent lands exactly on a step (2.89, never 2.887). Slow drag =
 * tick-by-tick; a fast flick carries momentum for the long travels. Vertical
 * swipes are left to the page (touch-action: pan-y) so the sheet still scrolls.
 *
 * FHWheel.make(mountEl, cfg, initialValue, onChange) -> { set, value, isSet }
 *   cfg = { min, max, step, dec, unit, home }   (min/max null = free that side)
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
    // Where an unset dial starts when you first turn it: an explicit cfg.home,
    // else the middle of a bounded range, else 0. Only a starting point — the
    // recorded value is whatever you dial or type; untouched dials stay unset.
    const home = clamp(cfg.home != null ? cfg.home
      : (cfg.min != null && cfg.max != null ? snap((cfg.min + cfg.max) / 2) : 0));
    const parse0 = (v) => { const n = parseFloat(v); return isFinite(n) ? clamp(n) : null; };
    let value = parse0(initial);                         // null = unset

    mount.classList.add("wheel");
    mount.innerHTML =
      '<div class="wh-track"></div><div class="wh-needle"></div>' +
      '<button type="button" class="wh-val" aria-label="tap to type a value"></button>';
    const track = mount.querySelector(".wh-track");
    const valBtn = mount.querySelector(".wh-val");

    function render() {
      const unset = value === null;
      mount.classList.toggle("unset", unset);
      const centre = unset ? home : value;              // ticks orbit home when unset
      const half = mount.clientWidth / 2 || 150;
      const base = Math.round(centre * q);
      let html = "";
      for (let i = -SIDE; i <= SIDE; i++) {
        const idx = base + i;
        const v = idx / q;
        const x = half + (v - centre) * q * PX;
        if (x < -PX || x > mount.clientWidth + PX) continue;
        const oob = (cfg.min != null && v < cfg.min - 1e-9) ||
                    (cfg.max != null && v > cfg.max + 1e-9);
        const major = idx % 5 === 0;
        // only label a major tick clear of both edges, else it clips ("4.15" -> ".15")
        const lbl = major && !oob && x > 24 && x < mount.clientWidth - 24;
        html += '<i class="wh-t' + (major ? " maj" : "") + (oob ? " oob" : "") +
                '" style="left:' + x.toFixed(1) + 'px">' +
                (lbl ? '<b>' + fmt(v) + "</b>" : "") + "</i>";
      }
      track.innerHTML = html;
      valBtn.textContent = unset ? "—" : fmt(value) + (cfg.unit ? " " + cfg.unit : "");
    }

    function commit() { onChange(value === null ? "" : fmt(value)); }

    // ---- drag + momentum ----------------------------------------------------
    // Direction-aware so the sheet still scrolls: the dial only claims a gesture
    // once it's clearly horizontal; a vertical swipe is left to the browser
    // (touch-action: pan-y). An unset dial wakes at `home` on the first turn.
    let dragging = false, claimed = false, sx = 0, sy = 0, lastX = 0, vel = 0, raf = 0, moved = 0;
    const posX = (e) => (e.touches ? e.touches[0].clientX : e.clientX);
    const posY = (e) => (e.touches ? e.touches[0].clientY : e.clientY);

    function down(e) {
      dragging = true; claimed = false; moved = 0; vel = 0;
      sx = lastX = posX(e); sy = posY(e);
      cancelAnimationFrame(raf);
    }
    function move(e) {
      if (!dragging) return;
      const x = posX(e), y = posY(e);
      if (!claimed) {
        const dX = Math.abs(x - sx), dY = Math.abs(y - sy);
        if (dX < 5 && dY < 5) { lastX = x; return; }    // too small to judge
        if (dY > dX) { dragging = false; return; }      // vertical -> let it scroll
        claimed = true;
        mount.classList.add("dragging");
        if (e.pointerId != null && mount.setPointerCapture) {
          try { mount.setPointerCapture(e.pointerId); } catch (_) {}
        }
      }
      const dx = x - lastX; lastX = x; vel = dx; moved += Math.abs(dx);
      if (value === null) value = home;                 // wake an unset dial
      value = clamp(value - dx / PX / q);               // drag left -> value up
      render();
      e.preventDefault();
    }
    function up() {
      if (!dragging) return;
      dragging = false; mount.classList.remove("dragging");
      if (claimed) glide();
    }
    function glide() {
      if (value === null) return;
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
      mount.addEventListener("touchstart", down, { passive: true });
      mount.addEventListener("touchmove", move, { passive: false });
      mount.addEventListener("touchend", up);
      mount.addEventListener("mousedown", down);
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    }

    // ---- tap the readout to type an exact value (blank = clear to unset) -----
    valBtn.addEventListener("click", () => {
      if (moved > 4) { moved = 0; return; }             // that was a drag, not a tap
      const inp = document.createElement("input");
      inp.type = "number"; inp.step = String(step);
      inp.value = value === null ? "" : fmt(value);
      inp.placeholder = "—";
      inp.className = "wh-type"; inp.inputMode = "decimal";
      valBtn.replaceWith(inp); inp.focus(); inp.select();
      const done = (apply) => {
        if (apply) {
          const raw = inp.value.trim();
          if (raw === "") { value = null; commit(); }    // cleared -> unset
          else {
            const n = parseFloat(raw);
            if (isFinite(n)) { value = clamp(snap(n)); commit(); }
          }
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
      value: () => (value === null ? "" : fmt(value)),
      set: (v) => { value = parse0(v); render(); },
      isSet: () => value !== null,
    };
  }

  window.FHWheel = { make };
})();
