# Analysis-session learnings — app improvement notes

**Date:** 2026-07-23
**Car under test:** 2003 Toyota Celica Sport Speciality II (C 500, FWD)
**Material:** two AI packages (touge session #157, Rivals session #160), the saved
tune lineage (v1–v5), and an external AI's tune read (Fable) cross-checked against
the raw frames.
**Why this doc exists:** this was a *realistic, messy* tuning session, not a clean
lab test. It exposed one concrete bug (Rivals lap detection), one whole class of
usage the app doesn't account for (users drift off a controlled test the moment
they get bored), and a handful of interpretation calls the app's own coach/report
could learn to make. Captured here so the app can be improved.

---

## TL;DR — what to fix, most valuable first

*(Reordered after reading `app/laps.py` + `coach.py`. The app already does event-type
detection, game-lap times, corner-phase USI and the throttle-reapply flag — so the
release is much smaller than the raw symptoms suggested.)*

1. **[SHIP] Rivals restart / junk-lap guard.** The one real bug: Rivals resets
   `LapNumber` on each restart, the wire path emits near-zero-distance restart
   segments with a stale `LastLap`, and a phantom `44.311` wins `best_lap`. Guard on
   distance + `LapNumber` resets (§1, §5.1).
2. **[SHIP] Full-throttle shift point.** `shift_rpm_avg` is upshifts-only but counts
   part-throttle short-shifts, reading ~1,400 rpm low. Add a throttle-gated stat
   (§4, §5.2).
3. **[VERIFY] Confound surfacing** on cross-session compare — `lap_route_m` and
   lineage exist; check whether the deltas (tune fields · route · assists) are shown,
   add if not (§3, §5.4).
4. **[VERIFY before building] Remaining coach ideas** — front-limited on-lift
   discriminator, assist-off-but-tune-unset flag. Check `coach.py` first; the flags I
   assumed missing already exist (§4, §5.5).
5. **[DONE — do not rebuild]** event detection, game-lap times, corner-phase USI,
   throttle-reapply flag, route-aware compare fingerprint.

---

## 1. The Rivals lap-detection artefact (the bug)

Session #160 was a **Rivals** run on a circuit. The package reported:

- `best_lap = 44.311` — **bogus**. That "lap" has `distance_m ≈ 0` and `max_kmh 27`.
- Several phantom rows with `distance_m = 0`, zero throttle/brake/slide, duplicated
  times (`58.575` twice, `58.442`), interleaved with the real laps.
- The genuine hot laps are the `distance_m ≈ 1900 m` rows: **58.425 / 58.559 /
  59.076 / 60.11 / 60.64 / 60.84 / 61.38**. Real best = **58.425**, not 44.311.

A consumer (human or AI) that trusts `best_lap` or the raw lap list is misled. I
only got the right answer by filtering on `distance_m` and working from the raw
frames.

**Root cause (confirmed by reading `app/laps.py`, not just the symptoms):** #160 took
the *correct* game-lap path — its `LapNumber` is populated, so `_seg_indices`
(laps.py:449) split on `LapNumber` transitions and `_lap_time` (laps.py:478) read the
game's `LastLap`. The bug is that **Rivals resets `LapNumber` to 0 on every restart**
(#160: `0,1,0,1,2,3,4,0,…`, two resets). `_seg_indices` splits at those resets too,
producing near-zero-distance restart segments; `_lap_time` then pins a **stale
`LastLap`** onto them; and the only guard is `t < MIN_LAP_S (15 s)` — so a phantom
`44.311` with no distance behind it passes as a complete lap and wins `best_lap`.
The tool was *not* ignoring game data or synthesizing — it just lacked a distance /
restart guard on the wire path.

---

## 2. Free-roam artefact vs Rivals-queuing artefact — they ARE separable

The ask: could the app tell that #157's junk was a *free-roam* artefact while
#160's was a *Rivals-queuing* artefact? Yes — the raw lap channels make the event
type unambiguous. Measured over `IsRaceOn` frames:

| Signal | #157 touge (free-roam) | #160 Rivals (circuit) | What it tells us |
|---|---|---|---|
| `LapNumber` | `0` the whole time | increments `0→4` | Game is/ isn't counting laps |
| `BestLap` channel | `0` (0 % of frames) | `61.38` (57 % of frames) | Game reports a best lap only on a real circuit |
| `LastLap` channel | `0` | populated (57 %) | Authoritative per-lap time available in Rivals, absent in free-roam |
| `CurrentLap` timer | `0` | up to `61.4` | Game lap clock running or not |
| `CurrentRaceTime` | continuous, **0 resets** | **3 resets** | Rivals restarts/requeues punch the clock back |
| `DistanceTraveled` | `0…55,825 m` (one long point-to-point) | `−59…23,910 m` (loops + reset glitch) | Free-roam covers ground; circuit loops |
| Position bbox | 863 m × 537 m (A→B route) | 550 m × 352 m (closed loop) | Point-to-point vs circuit |

**They are opposite signatures.** Free-roam: the game provides **no** lap data at
all (`LapNumber` pinned at 0, `BestLap`/`LastLap`/`CurrentLap` all zero), so the app
*must* synthesize laps — and the touge session's rewind/out-lap junk was in fact
already handled (`valid=false`, `rewind_affected=true`). Rivals: the game provides
**full** lap data, but the app ignored it and synthesized anyway, and the restart
resets became phantom laps.

**Detection rule (cheap, robust):**

```
if max(LapNumber) == 0 and max(LastLap) == 0 and max(BestLap) == 0:
    event = "free-roam"      # no game laps → synthesize (position-gate/timed),
                             # and LABEL the laps as synthetic in the UI/report
else:
    event = "circuit/rivals" # game laps exist → trust LastLap for lap times;
                             # treat CurrentRaceTime resets as restart boundaries,
                             # NOT as laps; drop 0-distance splits
```

**Validated across the full 77-session library (2026-07-23):** the split is perfectly
bimodal — 75 free-roam (all three lap channels 0), 2 game-lapped (#160, #146; both
`BestLap` *and* `LastLap` populated), **zero ambiguous** sessions. The rule holds.

**Refinement the validation caught:** `CurrentRaceTime` resets are **not** a
game-lapped/Rivals signal — ~20 free-roam sessions also reset (rewinds/respawns on
the open road). So resets must **not** be used to classify event type; the lap
channels do that. Treat a reset only as a *within-session* rewind/restart marker,
and apply the "drop the phantom split at a reset" logic **only inside a game-lapped
session** (free-roam already flags these via `rewind_affected`).

**A sharper red flag we should surface:** in #160 the game's `BestLap` maxed at
`61.38` while our synthesis produced clean `58.4` laps. When our lap times and the
game's `LastLap`/`BestLap` disagree by that much, something is wrong (dirty-lap
invalidation, or our splits are off). The app should reconcile the two and warn,
not silently pick one.

---

## 3. How real users actually test (the "getting bored" drift)

The tune lineage over this session:

| Tune | Session | Best lap | What changed from prior | Assists |
|---|---|---|---|---|
| v1 | #142 | 70.911 | baseline | on |
| v2 | #156 | 71.522 | final 3.76→4.15, camber −2→−1.5, diff 13→38, ARB_r 25.7→42 | on |
| v3 | #157 | **70.737** | final →4.90, camber →−2.8, diff →55, rear softened | on |
| v5 | #160 | 58.425* | +front pressure 1.75, camber −3.0, **ABS off, TCS off**, **different track (Rivals circuit)** | **off** |

\* different track — not comparable to the touge times.

The intent at the start was a disciplined one-lever-at-a-time A/B. By the end the
user had: changed **several** chassis values at once, **toggled both assists**, and
**switched to a completely different event**. This is not misuse — it is the normal
arc of someone tuning for fun. The app should assume it, not fight it.

**Implications for the app:**

- **Never present an uncontrolled comparison as clean.** The lineage table already
  carries the tune per session (good). Extend it: when two sessions are lined up
  (Compare, or lineage in the report), compute and state the deltas —
  *"3 tune fields changed · different route · ABS toggled"* — so nobody reads a
  0.8 s gap as "the diff change was worth 0.8 s."
- **Lean on the raw-frame diagnosis, which survives messy testing.** Every genuinely
  useful call this session came from the raw channels (front vs rear slip, on-lift
  slip, brake-lock depth, inside-wheel spin), not from the lap A/B. That is the
  app's durable value: telemetry measures *the actual car*, so it is correct even
  when the experiment is a mess and even when a typed tune value is wrong
  (a camber mis-click meant the saved tune said −2.5 while the car ran −2.8; the
  slip data was measured on the real −2.8 car, so the read held regardless).
- **Assists on↔off resets what every braking/traction metric means.** When the user
  flips ABS/TCS, prior sessions are not just a different tune — the *meaning* of
  brake-lock and wheelspin counts changes. The report already half-knows this
  (ABS-on caveats). It should treat an assist change between sessions as a hard
  "different baseline" boundary in lineage/compare.

---

## 4. Interpretation calls worth encoding in the coach/report

These are reads I made by hand that the deterministic engine could make (or at
least scaffold). All are grounded in channels we already capture.

- **Front-limited vs rear-loose (cause vs symptom).** Compare cornering combined
  slip front (`max FL,FR`) vs rear (`max RL,RR`): fraction of corner frames over
  1.0. The decisive discriminator is **on-lift** (throttle < 10 %): if the front
  stays over the limit off-throttle, it's a grip limit, not power-induced rear. On
  this car: front 52–54 % over limit vs rear 19–28 %, on-lift front 1.30–1.38 vs
  rear 0.89–1.07. A "nervous" car flagged by the coach can be front-limited — the
  fix is calm the rear (soften), not grip it up. The coach's front↔rear "flip rate"
  headline should be paired with this so it doesn't imply the rear is the problem.
- **Metric ≠ outcome (don't chase the understeer index).** v3 had a *higher*
  understeer index than v2 (~0.32 vs ~0.22) and was 0.8 s *faster*. The clock is
  the arbiter. Any coach line that reads a metric as "worse" should defer to lap
  time where a valid lap exists.
- **ABS off makes brake tune a live lever — and it was unset.** In #160, hard-brake
  frames were at/over the lock threshold on essentially every stop (deepest wheel
  slip −7.38 = fully locked; up to 5 hard lock events on the scrappy laps), and
  `brake_bal`/`brake_pres` were **blank** in the tune. That is a catchable coach
  insight: *ABS is off and you haven't set brake pressure/balance — that's your
  inconsistency.* Generalize: when an assist is off, the corresponding tune fields
  become "should be set," and unset-ness is worth flagging (ties into the new
  null-state work — unset is now first-class).
- **Gearing validation from Power + Gear.** The final-drive change was confirmed
  good by: 6th-gear share went 0 %→27 % of the lap, top-of-6th 187 km/h @ 7,611 rpm,
  full-throttle time below the power band 44 %→39 %, peak power ~157 kW @ ~7,500 rpm
  (all from `Power`/`CurrentEngineRpm`/`Gear`/`Speed`). A "gearing" card could show
  gear-share, per-gear top speed/rpm, and % of full throttle spent below peak-power
  rpm.
- **Rear temp as a symptom read.** Cold rears (66 °C) that still throw slide stabs =
  cold+hard+stiff snap, not a grip deficit. After softening, rears moved into window
  (~77 °C on the touge) and slide share dropped. Temp-in-window vs slide-events is a
  useful pairing.
- **`shift_rpm_avg` counts part-throttle upshifts — gate it on throttle.**
  (Corrected after reading laps.py:847.) It is *already* upshifts-only
  (`np.diff(gear) > 0`), not polluted by downshifts. But it includes short-shifts
  and part-throttle upshifts out of slow corners, which dragged it to `6,388` when
  the clean *full-throttle* upshifts were a tight `7,750–7,780`. Fix is small: gate
  the shift-rpm stat on `accel >= FULL_THROTTLE` so it reports the on-power shift
  point (the number a tuner actually reasons about). `shift_rpm_spread` already
  exists alongside it, so add a `shift_rpm_full_throttle` rather than breaking the
  existing field.
- **Corner-phase imbalance — ALREADY IMPLEMENTED, no work needed.** (Corrected.)
  laps.py:562 computes entry/mid/exit/lift USI (`balance.phases`), tuning_export.py:188
  prints "Understeer index by corner phase," and coach.py:112 uses it. The external
  read's "+0.417 mid / +0.150 exit" came *from your own report*, not from independent
  computation. Leave it alone; it's good.
- **`throttle_reapply_s` — ALREADY SURFACED, no work needed.** (Corrected.)
  coach.py:179 already flags late throttle ("You're back on throttle {reapply}s after
  the apex…") against a calibrated `LATE_THROTTLE_S = 1.3`. The external read's
  "1.38 s past apex" is this metric. Nothing to add.

---

## 5. Concrete recommendations for `app/laps.py` (and neighbours)

Each item is tagged with its real status after reading the code — several things
the raw symptoms suggested were "missing" already exist.

1. **[SHIP — the actual bug] Rivals restart / junk-lap guard.** In `lap_report`'s
   wire-path loop (laps.py:1042) and `best_lap_s` selection (laps.py:1128): reject or
   mark-incomplete any segment whose `distance_m` is far below the session's median
   lap distance, or that straddles a `LapNumber` reset (`lap_no` decreasing = a Rivals
   restart, treat like `rewind_affected`). A best-lap-eligible lap must have real
   distance behind it. Kills the `44.311` and the 0-distance phantoms. This is the
   one genuine bug and the reason for the release.
2. **[SHIP — small] Full-throttle shift point.** Add `shift_rpm_full_throttle`
   (upshifts with `accel >= FULL_THROTTLE`) beside the existing `shift_rpm_avg`
   (laps.py:847/980). The current field reads ~1,400 rpm low on a well-driven car
   because it counts part-throttle short-shifts out of slow corners — not because it
   includes downshifts (it doesn't).
3. **[VERIFY, then maybe] Reconcile `best_lap` vs the game's `BestLap`.** In a
   game-lapped session, if they disagree beyond tolerance, warn rather than silently
   trusting the computed value — a cheap integrity net for the rare game-lap path.
4. **[VERIFY] Confound banner for cross-session compare.** `compact_summary` already
   stores `lap_route_m` ("never compare across routes", laps.py:1154) and the report
   carries tune lineage — check whether Compare/report *explicitly* flags "N tune
   fields changed · different route · assists toggled." Add it if not; treat an assist
   change as a hard baseline break.
5. **[VERIFY before building] Remaining coach ideas.** front-limited-vs-rear-loose
   on-lift discriminator; assist-off-but-tune-unset flag (esp. brakes when ABS is
   off); defer-to-the-clock on "worse metric" lines. Read `coach.py` first — the phase
   and throttle-reapply flags I *assumed* were missing already exist, so these may too.

**Already implemented — do NOT rebuild** (confirmed in code this pass):
event-type detection (`has_laps` via `LapNumber`, laps.py:458); game-lap times via
`LastLap` (`_lap_time`, laps.py:478); corner-phase USI (`balance.phases`, laps.py:562,
surfaced in tuning_export.py:188 and coach.py:112); the `throttle_reapply_s` coach flag
(coach.py:179); the route-aware comparison fingerprint (`lap_route_m`). The tool is more
complete than the symptoms implied — scope the release to items 1–2, verify 3–5.

---

## Appendix — data provenance

All figures above were computed directly from the two packages' `raw-telemetry.csv`
(full 324-byte Forza Data Out layout: per-wheel `TireSlipRatio`, `TireCombinedSlip`,
`TireSlipAngle`, `TireTemp`, plus `Power`, `Torque`, `Gear`, `Accel`, `Brake`,
`Steer`, `Speed`, `CurrentEngineRpm`, `LapNumber`, `BestLap`, `LastLap`,
`CurrentRaceTime`, `DistanceTraveled`, `PositionX/Z`). Clean-lap filtering used the
session's timed windows for the touge and `distance_m`-valid laps for Rivals. Tyre
temps are Forza's Fahrenheit channel converted to °C. Nothing here required data the
tool doesn't already capture.
