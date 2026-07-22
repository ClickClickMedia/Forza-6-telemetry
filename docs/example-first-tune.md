# First tune — a worked example

**Report goal → First tune** asks the AI for a complete one-shot baseline
when a car is new or far from sorted. The word that matters is *complete*:
it means **every tunable subsystem gets a verdict**, not that every value
must move. This is a real case that shows why that distinction is the whole
point.

The car: a **2005 Porsche Cayman GT3 WTAC**, S1 800, AWD.

## The evidence the export handed the AI

From one circuit session, the report led with a single dominant story:

- Front axle running **0.94 of its grip limit**, rear only **0.44**.
- Wheelspin almost entirely at the front: **34.8 s front-only** vs **1.6 s
  rear-only**.
- Inside-wheel flare **31.9 s** (inside 31.9 / outside 0.3) — the classic
  more-front-lock signal.
- Front−rear tyre temperature delta **+6.3 °C** — the thermal story agrees
  with the slip story: the fronts are working harder.
- Understeer index **+0.499**, strongest mid-corner and on lift.
- Body control already healthy: squat +0.02, dive +0.15, roll p95 0.38
  front / 0.30 rear, **no sustained bottoming**.

One physical constraint explains all of it: on an AWD car the **front axle
is doing two jobs at once** — carrying most of the lateral load *and*
receiving a share of engine torque — while the rear sits underused.

## The disciplined read: a verdict per subsystem

| Subsystem | Verdict | Why |
| --- | --- | --- |
| Centre differential | **CHANGE** | Move the centre split rearward to unload the front axle — the one lever the evidence points at. The exact % is yours within the slider range. |
| Front/rear diff lock | **RETAIN** (for now) | A later, isolated test — see below. Adding front lock while the front is still laterally saturated can *add* power understeer. |
| Alignment (camber/toe/caster) | **RETAIN** | Forza broadcasts one temperature per tyre, no tread bands. Nothing here justifies moving camber. |
| Brakes | **RETAIN** | No sustained lock events. |
| Gearing | **RETAIN** | Negligible limiter time, no gear-specific wheelspin evidence. |
| Springs / ride / dampers / ARBs | **RETAIN** | Body control is already in range and nothing is bottoming. |
| Aero | **RETAIN** | No speed-dependent balance evidence, and the slider range is unknown. |
| Tyre pressures | **RANGE REQUIRED** | No pressure channel on the wire; axle-average temps are in-window. |

**One CHANGE, the rest RETAIN — and that is a complete, valid first tune.**

**Pass rule** (straight from the data): front-only wheelspin falls, front
axle use drops while the rear rises, mid-corner and exit understeer ease,
matched pace improves, and the rear does **not** become the new limit.

**Conditional next test** — only if inside-front flare is still dominant
*after* the front axle is less saturated: try a little more front
acceleration lock, on its own, so you can attribute the result.

## Why "complete" is not "change everything"

On this exact car, a broad tune — the kind that touches alignment, gearing,
brakes, springs, aero and the differentials all at once — was tried instead.
The very next run measured, against the baseline above:

| Signal | Baseline | After the broad tune |
| --- | --- | --- |
| Best lap | 53.55 | **54.03** (slower) |
| Oversteer moments | 3 | **79** |
| Wheelspin events | 58 | 160 |
| Rear tyre peak | 268° | **304°** |

It did reduce the understeer (index +0.499 → +0.302) — by trading it for
snap oversteer, a cooking rear axle, and half a second of lap time. The
telemetry supported **one** lever; changing everything imported assumptions
the evidence never made.

That is the failure mode first-tune mode is built to prevent. It asks for a
disposition per subsystem — **CHANGE / RETAIN / CANNOT ASSESS / RANGE
REQUIRED** — makes "RETAIN the rest" a first-class answer, and tells the AI
to drop any change the report shows no failure mode for. The evidence you
copy is bounded to *this* car, *this* session — which is exactly what keeps
a broad, confident, wrong tune from happening.
