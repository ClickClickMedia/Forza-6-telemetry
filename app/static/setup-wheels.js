/* Wires the setup sheet's numeric inputs to thumb-wheels (wheel.js).
 * The <input id="su_*"> elements stay as the hidden value store, so
 * gatherForm() and fillForm() in analysis.html are untouched — this is purely
 * the input layer. Gears become N per-gear wheels + a count stepper, recombined
 * into the same "4.17 / 2.89 / …" string on su_gears.
 *
 *   FHSetupWheels.build()          — once, after the sheet is visible
 *   FHSetupWheels.sync(topGear)    — on open / when a saved setup is loaded
 */
(function () {
  "use strict";
  const CFG = {
    su_tp_f: { min: 0.5, max: 4, step: 0.01, dec: 2, unit: "bar" },
    su_tp_r: { min: 0.5, max: 4, step: 0.01, dec: 2, unit: "bar" },
    su_final: { min: 2.0, max: 6.0, step: 0.01, dec: 2 },
    su_camber_f: { min: -6, max: 2, step: 0.1, dec: 1, unit: "°" },
    su_camber_r: { min: -6, max: 2, step: 0.1, dec: 1, unit: "°" },
    su_toe_f: { min: -1, max: 1, step: 0.1, dec: 1, unit: "°" },
    su_toe_r: { min: -1, max: 1, step: 0.1, dec: 1, unit: "°" },
    su_caster: { min: 1, max: 7, step: 0.1, dec: 1, unit: "°" },
    su_arb_f: { min: 1, max: 65, step: 1, dec: 0 },
    su_arb_r: { min: 1, max: 65, step: 1, dec: 0 },
    su_spring_f: { min: 0, max: null, step: 1, dec: 0 },
    su_spring_r: { min: 0, max: null, step: 1, dec: 0 },
    su_ride_f: { min: 0, max: null, step: 0.1, dec: 1 },
    su_ride_r: { min: 0, max: null, step: 0.1, dec: 1 },
    su_reb_f: { min: 0, max: 20, step: 0.1, dec: 1 },
    su_reb_r: { min: 0, max: 20, step: 0.1, dec: 1 },
    su_bump_f: { min: 0, max: 20, step: 0.1, dec: 1 },
    su_bump_r: { min: 0, max: 20, step: 0.1, dec: 1 },
    su_aero_f: { min: 0, max: null, step: 1, dec: 0 },
    su_aero_r: { min: 0, max: null, step: 1, dec: 0 },
    su_diff_f_accel: { min: 0, max: 100, step: 1, dec: 0, unit: "%" },
    su_diff_f_decel: { min: 0, max: 100, step: 1, dec: 0, unit: "%" },
    su_diff_r_accel: { min: 0, max: 100, step: 1, dec: 0, unit: "%" },
    su_diff_r_decel: { min: 0, max: 100, step: 1, dec: 0, unit: "%" },
    su_diff_centre: { min: 0, max: 100, step: 1, dec: 0, unit: "%" },
    su_brake_bal: { min: 0, max: 100, step: 1, dec: 0, unit: "%" },
    su_brake_pres: { min: 0, max: 200, step: 1, dec: 0, unit: "%" },
  };
  const GEAR = { min: 0.4, max: 6, step: 0.01, dec: 2 };
  const ORD = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th",
               "10th"];

  const wheels = {};
  let gearWheels = [], gearRows = null, gearCountEl = null, built = false;

  function wheelAfter(input, cfg) {
    const m = document.createElement("div");
    input.style.display = "none";
    input.insertAdjacentElement("afterend", m);
    return FHWheel.make(m, cfg, parseFloat(input.value),
                        (v) => { input.value = v; });
  }

  function setGearCount(n) {
    const cur = gearWheels.map((w) => parseFloat(w.value()));
    gearRows.innerHTML = ""; gearWheels = [];
    for (let i = 0; i < n; i++) {
      const row = document.createElement("div");
      row.className = "gear-row";
      row.innerHTML = '<span class="eyebrow">' + ORD[i] + "</span>";
      const m = document.createElement("div");
      row.appendChild(m); gearRows.appendChild(row);
      let v = isFinite(cur[i]) ? cur[i]
        : (i > 0 && isFinite(cur[i - 1]) ? cur[i - 1] * 0.82 : 3.2 - i * 0.4);
      if (v < GEAR.min) v = GEAR.min;
      gearWheels.push(FHWheel.make(m, GEAR, v, recombine));
    }
    gearCountEl.textContent = n;
  }
  function recombine() {
    document.getElementById("su_gears").value =
      gearWheels.map((w) => w.value()).join(" / ");
  }

  function buildGears() {
    const input = document.getElementById("su_gears");
    if (!input) return;
    input.style.display = "none";
    const wrap = document.createElement("div");
    wrap.className = "gears";
    wrap.innerHTML =
      '<div class="gears-head"><span class="eyebrow">Per-gear ratios</span>' +
      '<span class="gears-count"><button type="button" class="gc" data-d="-1">−</button>' +
      '<b id="gearCount">6</b><button type="button" class="gc" data-d="1">+</button></span></div>' +
      '<div class="gear-rows"></div>';
    input.insertAdjacentElement("afterend", wrap);
    gearRows = wrap.querySelector(".gear-rows");
    gearCountEl = wrap.querySelector("#gearCount");
    wrap.querySelectorAll(".gc").forEach((b) => b.addEventListener("click", () => {
      const n = gearWheels.length + (b.dataset.d === "1" ? 1 : -1);
      setGearCount(Math.max(1, Math.min(10, n)));
      recombine();
    }));
  }

  function build() {
    if (built) return; built = true;
    Object.keys(CFG).forEach((id) => {
      const input = document.getElementById(id);
      if (input) wheels[id] = wheelAfter(input, CFG[id]);
    });
    buildGears();
  }

  function sync(topGear) {
    Object.keys(wheels).forEach((id) => {
      const input = document.getElementById(id);
      if (input) wheels[id].set(parseFloat(input.value));
    });
    const input = document.getElementById("su_gears");
    const parts = (input.value || "").split(/[/,]/)
      .map((s) => parseFloat(s)).filter(isFinite);
    const n = parts.length || (topGear >= 4 ? topGear : 6);
    setGearCount(n);
    parts.forEach((v, i) => { if (gearWheels[i]) gearWheels[i].set(v); });
    recombine();
  }

  window.FHSetupWheels = { build, sync };
})();
