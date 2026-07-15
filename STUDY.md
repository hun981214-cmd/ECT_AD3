# ECT_AD3 Study Notes — PE Sensing Physics, Models, and Measurement Consistency

Last updated: 2026-07-15. This document consolidates the full 2026-07-15
investigation: temperature response of the new probe standard, residual
drift, PE-conversion model selection (regression vs trajectory), the
impedance-plane physics of the specimen trajectories, the resistivity–
plastic-strain material law, and a consistency audit of every geometry
measurement. Companion data: [data/measurement.csv](data/measurement.csv).
Status/history: [ISSUE_STATUS.md](ISSUE_STATUS.md). All strains are
unitless (eps = dL/L; the 8 calibration specimens are nominally
0/0.10/0.20/0.30 per type). PE errors are stated as absolute strain
differences (a "+0.40 error" on a true 0.30 specimen reads 0.70).

## 1. Hardware context

- Probe: EW-8SCT drive-pickup absolute probe on the AD3 BNC adapter
  (W1 = drive coil, CH1 = pickup, W2->CH2 = start-synchronized twin-drive
  reference; complex ratio pickup/reference is the measurand).
- Standard since 2026-07-15 (spare probe cable): 110 kHz, 0.15 V drive,
  air |ratio| ~12.80 at 27 C reference temperature.
- The SAME 8 physical specimens (2 wire types x nominal 0/0.10/0.20/0.30)
  are used across all ECT / ECTv2 / ECT_AD3 calibration history.
- type1 = thick rectangular Cu wire (~3.3 x 2.0 mm coated),
  type2 = thin (~3.3 x 1.7 mm coated). Both PI-coated.

## 2. Temperature response and correction (spare-cable standard)

Sweep `tempsweep_20260715_s01..s04` (A/C shutoff warm-up, 24.2 -> 30.0 C,
3.22 h, 2272 rows; fitted by `analysis/temp_response.py` ->
`analysis/temp_response_20260715.json`):

| quantity | spare cable (07-15) | original cable (07-13) |
|---|---|---|
| thermal lag (probe vs RN171) | -2.5 min (probe leads) | -4.8 min |
| d|ratio|/dT | -0.238 %/C (curved: -0.29 @24C .. -0.20 @30C) | -0.399 %/C |
| d(phase)/dT | +0.222 deg/C | +0.322 deg/C |
| raw span over sweep | 0.836 % | 2.14 % (7.1 C span) |
| corrected (lag-quadratic T+T^2+H) | 0.088 % in-sample / 0.114 % OOS | 0.20 % / 0.3-0.4 % |

Residual drift on the post-warm-up window (27.2-30.0 C, 2.8 h), verified by
3 independent re-derivations (commit cbb97b7):

- deployed model residual 0.086 % span; best self-fit 0.056 % span,
  0.015 % at 30-min blocks; **no significant time trend**; residual-vs-env
  correlations all |r| <= 0.13; noise floor 0.0067 %.
- PE-equivalent (worst case, type2): span ~0.004-0.006, 30-min block
  ~0.001 — negligible vs the 0.10 inter-specimen spacing.
- The script's alarming split-half OOS (0.633 %, worse than raw) was traced
  to an artifact: the 5-parameter RC+pcb correction is unidentifiable on
  the weakly-excited half of a monotonic ramp (condition number ~1e7) and
  its extrapolation error is a smooth time-ramp (corr 0.998), not sensor
  drift. Same-complexity honest splits transport at 0.058-0.077 %.
- CAVEATS: in-sample (model fitted on this sweep); no HVAC cycles in the
  window. The definitive out-of-sample overnight run is pending (needs the
  probe back on the fixture).

Uncorrected temperature sensitivity in PE terms (via the trajectory model):
~0.03-0.07 per 1 C. With correction: ~0.001 per 1 C.

## 3. PE conversion model selection

### 3.1 Candidates

- **A-form** (the EW-8SCT `sensor_cal` regression, no type branching):
  `PE = C_PH*phase + C_AMP*amp + C_AREA*area + C_PXAR*(phase*area) + C0`
  on ABSOLUTE (temperature-corrected) values.
- **Trajectory projection** (current runtime): store the empty-probe
  baseline and the 8 specimen deltas (27 C-corrected); classify wire type
  from area (threshold 5.89 mm2 coated; the two families are separated by
  a wide gap so classification is unambiguous for these wires); project
  the measured baseline-delta onto that type's PE-parameterized polyline;
  off-trajectory distance gates air/invalid states to NaN.

### 3.2 Accuracy, same methodology on both instruments (same 8 specimens)

| evaluation | meaning | EW-8SCT (A-form) | AD3 (A-form) | AD3 (trajectory) |
|---|---|---|---|---|
| in-sample (fit 8, re-predict 8) | interpolation error right after calibration | 0.0051 | 0.0083 | ~0.002 (repeatability-limited) |
| LOO (drop one node) | conservative bound for unseen strain values | 0.016 | 0.028 | 0.034 |

Notes: AD3's A-form in-sample residual is concentrated in type2 with an
S-shaped sign pattern (linear form vs trajectory geometry mismatch);
type1 residuals <= 0.0025, better than EW's. The gap between in-sample and
LOO is calibration DENSITY (0.10 node spacing), not hardware: real usage
inside the calibrated range sits between the two rows. Denser specimens
(0.05 steps) would shrink it for both instruments.

### 3.3 Robustness — why the A-form was rejected for runtime

Monte-Carlo perturbation tests (300 draws) on the AD3 calibration:

| scenario | A-form | trajectory (session re-zero) |
|---|---|---|
| calibration noise 0.1 % of signal | 0.052 | 0.008 |
| calibration noise 1.0 % | 0.081 | 0.063 |
| runtime common-mode drift 0.1 % | 0.04-0.24 | ~0 |
| runtime common-mode drift 1.0 % | 0.39-2.38 | ~0 |
| uncorrected temperature, per 1 C | **~0.80** | 0.03-0.07 |

Mechanism: the A-form fit balances large absolute terms against each other
(large C_AMP/C_PH with cancellation); a 0.1 % absolute shift breaks the
cancellation. Measured reality: post-correction drift 0.06-0.09 %, fixture
handling shifts ~1 % — both beyond the A-form's tolerance. The EW-8SCT's
own A-form fails the same way under its 6 %/day drift, consistent with the
historical instability of its env-corrected long logs. The trajectory
model cancels common-mode shifts by construction (delta vs a fresh
empty-probe baseline each session; cost = 10 s of air measurement).

### 3.4 Verdict

- Hardware: AD3 superior on every directly measured metric (noise 13-70x,
  raw drift 8x, repeatability eps <= 0.005). The small calibration-day fit
  difference (0.016 vs 0.028 LOO) is statistically weak (8 points), driven
  by model-form mismatch + specimen placement repeatability, and is
  overwhelmed within hours by the drift difference.
- Runtime model: **trajectory projection** (already type-label-free: type
  is inferred from area). The A-form is kept as an offline comparison /
  paper baseline only.
- Live verification (2026-07-15 morning, full AMS_control stack): inserted
  type2 eps=0.30 specimen read 0.306-0.308 with correct type.

## 4. Physics of the specimen trajectories

### 4.1 Framework

The impedance-plane locus of a conductor in a coil is the universal
ber/bei curve parameterized by ka (|k| = sqrt(w*mu*sigma), a = effective
radius) with fill factor eta = a^2/b^2 scaling the excursion (thesis
Fig. 1-5 / Eq. 35). Plastic strain moves the operating point through TWO
channels: geometry (elongation shrinks the section: d(lnA) ~ -ln(1+eps),
lowering both eta and ka) and conductivity (cold work raises resistivity,
lowering ka). Skin depth at 110 kHz is ~0.20 mm vs half-thickness
0.7-1.0 mm (a/delta ~ 6-7): strong skin regime; response dominated by
outer geometry. Our 2-coil ratio measurement is an affine image of the
same physics.

### 4.2 Measured trajectory curvature (turning angles of the 4-node
polylines, sagitta as % of chord)

| | AD3 @110 kHz | EW-8SCT |
|---|---|---|
| type1 | 7.8, 0.7 deg (3.0 %, 1.1 %) | 2.1, 3.2 deg |
| type2 | 1.3, 0.1 deg (0.5 %, 0.1 %) | 4.5, 2.3 deg |

type1 is distinctly curved at 110 kHz, type2 nearly straight; at the EW
operating point both are mildly and similarly curved. Frequency choice =
choosing ka; 110 kHz was picked for SNR (probe resonance), not for feature
linearity. This explains why one pooled linear regression fits EW's
feature space more evenly than AD3's (EW A-form residuals show the same
type2 S-pattern at ~half amplitude).

### 4.3 Resistivity–strain law: accumulated cold work (KEY RESULT)

Direct resistivity data (PI coating stripped, separate specimens from the
same stocks; see measurement.csv batch `stripped_rho`) initially seemed to
show qualitatively different behavior per type: type1 flat to eps~0.11
then rising; type2 rising immediately and linearly. The published copper
model rho = r0 + r1*eps^r2 (r0 = 1.775e-8, r1 = 2.894e-9 Ohm m, r2 = 1.5)
has zero initial slope, matching type1 but not type2.

Resolution (user hypothesis, confirmed quantitatively): resistivity is a
function of the ABSOLUTE accumulated equivalent strain, not of the strain
applied after delivery. The thinner type2 wire is drawn further and
arrives pre-strained. A single universal law with per-type offset,

```
rho(eps) = 1.781e-8 + 2.936e-9 * (eps0_type + eps)^1.5   [Ohm m]
eps0(type1) = 0.000        eps0(type2) = 0.100
```

fits ALL 10 stripped-batch points at the instrument quantization limit
(RMS 0.0083e-8 vs 0.0180e-8 for the no-offset published law; every point
within one 0.01e-8 quantum). It simultaneously explains type2's higher
initial resistivity (1.79 vs 1.78e-8) and its linear-looking low-strain
rise (the ^1.5 curve is locally linear when entered at 0.10). The fitted
r0/r1 essentially reproduce the published values.

### 4.4 Two-driver trajectory decomposition

Model: node-to-node signal steps dz = alpha*dlnA + beta*dln(sigma) with
complex alpha (geometry direction) and beta (conductivity direction) per
type.

- Geometry alone (beta = 0) leaves 30 % (type1) / 66 % (type2) of the step
  structure unexplained and cannot produce ANY turning — the conductivity
  channel is required. With a sigma term, type1 residual drops to ~6-14 %.
- With the eps0-offset universal law evaluated at the geometry-implied
  strain, the model reproduces type1's measured turning-angle pattern
  (model 7.1/2.0 deg vs measured 7.8/0.7) — the bend is the fingerprint of
  hardening onset (the sigma-share of the ka-motion grows ~0 % -> ~20 %
  across type1's strain range).
- Amplitude residuals remain contaminated by geometry-measurement noise
  (see 5.3): the fitted beta magnitudes/phases are not yet physical.
  Blocked on averaged-geometry measurements (weighing).

### 4.6 Absolute strain axis (2026-07-15, final)

The trajectory PE axis was re-parameterized once more, from delivered
area-strain to ABSOLUTE strain eps_abs = eps0(type) + eps_area — no
regression refit needed (the lookup geometry is unchanged; LOO identical
at 1.92 %p). The projection now reads PE_tot directly (type2's
as-received wire reads 0.10); delivered strain = PE_tot - eps0 and
resistivity rho(eps_abs) are derived outputs. specimen_check reports
PE_tot / PE(delivered) / rho. The summary figure header carries the two
governing equations (resistivity law; theory-mapped signal model with the
trajectory-projection readout), and panel A overlays faint THEORETICAL
trajectory families (start-area A0 sweeps with volume conservation and
the evolving rho(eps), plus rho-frozen geometry-only comparisons) — the
measured trajectories lie on the theory family at their own A0.

### 4.6b Conversion to the thesis impedance theory (VALIDATED 2026-07-15)

The measured complex ratio is convertible to the thesis's normalized
cylinder-impedance equations (Eq. 35 family): with mu_eff(ka) from the
Kelvin (ber/bei) functions, ka = sqrt(w*mu0/rho)*a_equiv (equivalent
circular radius from the bare area, rho from the eps0 universal law), the
theory quantity T = A*(1 - mu_eff(ka)) reproduces every measured specimen
delta through ONE complex coupling constant:

    r - r_air ~= C2_type * A * (1 - mu_eff(ka))

Residual 6.5 % with a single global C2; 2.6 % (type1) / 2.8 % (type2) with
per-type C2 (coupling genuinely differs with wire size: lift-off/bore fill
beyond the area term). Angles match within ~2 deg. In thesis-normalized
coordinates the specimens track the universal cylinder locus vs ka with a
systematic offset attributable to the rectangular cross-section vs the
circular-cylinder theory (equivalent-radius approximation). Figure:
`analysis/theory_mapping_20260715.png`. Consequence: dx/dy (ratio deltas)
and the thesis normalized-impedance plane are related by a per-type
complex affine map — the trajectory model can be presented as operating in
the theory's coordinates.

SINGLE-FORMULA requirement (user, 2026-07-15 — no type-specific
equations anywhere): the per-type constants were eliminated. (i) Coupling:
C2(A) = ca + cb*A (one complex-linear function of bare area) fits all 8
nodes at 4.2 % residual (global constant 6.5 %, per-type 2.6-2.8 %) —
recorded in pe_ratio_model.json as `theory_coupling`. (ii) eps0 is now a
continuous drawing law eps0(A0) = k*ln(A_ref/A0), k = 0.260,
A_ref = 6.617 mm2 (exactly determined by the two stocks; interpolation
between them is hypothesis until an intermediate stock is measured) —
recorded as `eps0_law`. (iii) Runtime: the hard area threshold was
replaced by a smooth logistic blend of the two stock-trajectory
projections (width 0.08 mm2; saturated at every calibration point, worst
round-trip deviation 0.21 %p; gap-area wires get a continuous, flagged
interpolation instead of a jump). The summary figure's mesh is now drawn
from the single formula.

IMPORTANT caveat (user-raised, 2026-07-15): the theoretical (area x
strain) mesh must NOT be used as a coordinate system to read specimen
values off measured points. Two reasons: (i) the coupling C2 is per-type
(~20 % apart) — against a single global-C2 mesh both types sit
systematically off; and (ii) the mesh is nearly degenerate (iso-strain
curves almost coincide because area dominates), so the (A, eps) -> signal
map is close to non-invertible: the strain-separating direction spans
~0.3 % of the signal while the theory's own residual is 2.6-6.5 %. The
theory explains structure (directions, curvature, scaling); the
measurement SCALE must remain the empirically calibrated trajectory.
This is the quantitative justification for the lookup-based runtime.

Note on measured resistivity coverage: the EC calibration set has directly
measured rho only at eps = 0 (the undeformed specimens share the same
stock state as the stripped batch's eps = 0 rows; attached in
measurement.csv); all deformed-specimen rho values are law-derived.

## 5. Measurement consistency audit

### 5.1 Laser dimension history (Keyence; laser1 = depth, laser2 = width)

All usable dimension data comes from the 2026-03-31 sessions (16 files in
ECT/logs; specimens matched across files by EW amplitude). Repeats exist
only for: t1_pe00 (x4), t1_pe20 (x4), t2_pe00 (x2). Session-to-session
spread: width +-0.3-0.8 % (t1_pe00 width read 3.390 in the first session,
3.415 in all three later sessions -> first-session low bias). The 0710 /
0714 AD3 sweeps logged no laser columns. measurement.csv uses per-specimen
medians.

### 5.2 Coating thickness (per type, per direction)

Derived by matching eps=0 coated dims to the stripped batch's eps=0 bare
dims (same undeformed stock, so valid regardless of batch identity):

| | width side | depth side |
|---|---|---|
| type1 | 0.067 mm | 0.083 mm |
| type2 | 0.105 mm | 0.111 mm |

The initially assumed uniform 0.12 mm is close for type2 only; type1's
insulation is ~30-40 % thinner. All bare dims in measurement.csv use these.

### 5.3 Identified measurement problems (ranked)

1. **t1_pe30 and t2_pe20 laser depths are non-monotone vs strain**
   (2.051 > 2.022 mm; 1.665 > 1.624 mm) — physically impossible for the
   same stock. Both are single-session readings with no repeats. Their
   bare areas deviate +7-8 % from volume conservation while all other
   specimens sit within +-2 %. The AD3 signal steps do NOT track these
   area anomalies (|dz|/|dlnA| jumps 3-8x exactly on these segments),
   i.e. the coil-averaged geometry the ECT sees disagrees with the
   single-point laser reading. Re-measure first (multi-point average).
2. **Nominal-vs-geometry strain gap in the calibration set — RESOLVED
   (2026-07-15, user)**: the EC calibration pe labels were assigned from
   TENSILE-MACHINE DISPLACEMENT, while the resistivity batch's labels are
   AREA-BASED — which is exactly why the stripped batch matches volume
   conservation and the EC set does not (machine strain includes grip
   slip, elastic recovery, and elongation outside the sensed region;
   nominal 0.30 corresponds to only ~0.20-0.21 local strain). The two
   batches are different specimens of the same types (initial stock
   deviation possible), so the coating estimates in 5.2 carry
   stock-tolerance uncertainty (~+-0.015 mm/side per 1 % stock spread).
   DECISION: relabel the EC calibration PE axis to AREA-BASED strain
   (eps = A0_bare/A_bare - 1), keeping the nominal names as specimen IDs.
   Rationale: the coil senses the local section, so area-based strain is
   the quantity the signal actually follows; it also unifies the strain
   definition with the resistivity law (one axis for the eps0 universal
   model and the two-driver decomposition). Residual error after
   relabeling = initial stock deviation (~+-0.01-0.02 strain, since the
   deformed specimens' own A0 is unknown); future calibration sets should
   record each specimen's dims BEFORE stretching to remove it.
   Sequencing: fix problem 1 first (weigh / multi-point re-laser), THEN
   compute the new labels, THEN regenerate the reference/PE model and
   propagate to AMS_control at port time.
3. **Resistivity table resolution**: quantized at 0.01e-8 (0.56 % steps),
   marginal for the 0.1-0.6 % low-strain increments; also inherits point-
   area errors via rho = R*A/L. Sufficient for the eps0 conclusion (which
   halves the residual), insufficient for fine law discrimination.
4. Point-vs-average geometry generally: the laser measures one spot; the
   ECT integrates over the coil length. Weighing (mass / (density x
   length) = average bare area) is the definitive fix and directly
   feeds the two-driver decomposition.

### 5.4 Planned measurements

- Weigh all 8 specimens + measure lengths -> average bare areas.
- Re-laser t1_pe30, t2_pe20 (and t2_pe30) at >= 3 positions.
- Confirm pe label provenance (machine vs gauge) for the calibration set.
- Higher-resolution resistivity (4 digits) with mass-based areas;
  extend type2 to eps = 0.2, 0.3 (tests the universal law where the
  offset hypothesis predicts continued ^1.5 curvature).
- Optional: frequency scan of the 8-specimen contrast (ka selection for
  feature linearity vs SNR).

### 4.5 Area-based strain axis + resistivity labels (EXECUTED 2026-07-15)

The EC calibration PE axis was switched from nominal machine strain to
area-based strain (`data/specimen_labels.json` is the single source of
truth; delete/regenerate it to roll back — both analysis scripts fall back
to nominal labels when it is absent). New axis per specimen (names stay
nominal): type1 0 / 9.30 / 17.07 / 20.64 %, type2 0 / 8.88 / 11.60 /
19.72 %. Each specimen also carries eps_abs = eps0 + eps_area and its
model resistivity via the universal law (embedded in pe_ratio_model.json
as `resistivity_law`), so the runtime can report resistivity alongside
strain: rho spans 1.781-1.809e-8 (type1) / 1.790-1.829e-8 (type2) Ohm m
across the calibration range. Offline round-trip verified: every stored
node reads back its own label exactly and maps to its label resistivity;
air rejection unchanged (gated by off-trajectory distance in the live
loop). Trajectory LOO on the new axis is 0.045 (was 0.034 on the nominal
axis) — expected: the axis is now non-uniform and type2's middle nodes sit
close together (8.88/11.60, resting on the suspect t2_pe20 laser reading);
this number should improve when the suspect dims are re-measured and the
labels refreshed. Runtime lookup behavior is geometrically unchanged
(same trajectories, re-parameterized).

Outlier imputation (2026-07-15, user decision): t2_pe20's strain label was
replaced by a signal-derived value instead of re-measuring — eps interpolated
between the trusted pe10/pe30 neighbors by signal-arc fraction (locally
constant path speed), area from volume conservation: eps 0.1160 (suspect
laser) -> 0.1682 (nominal was 0.20). Marked `imputed` in measurement.csv.
Effects: trajectory LOO improved to 0.021 (max 0.042); the two-driver
segment test for type2 closed (resid 0.683 -> 0.067, step magnitudes within
2-19 %, directions within 0.3 deg) — though type2's middle segments are no
longer independent evidence for the sigma law (label derived from the same
signal). type1 (resid 0.139, all directions within 2 deg) remains the
independent check; t1_pe30's own suspect reading was left as-is. The
fitted beta phases still differ between types (-47 vs -179 deg) — the
decomposition's coefficients await averaged geometry (weighing) before
physical interpretation.

t1_pe30 endpoint imputation (2026-07-15, user decision — feasibility-check
scope): eps solved from the two-driver model calibrated on the two trusted
type1 segments (volume conservation + eps0 law), matching the measured
third step: eps = 0.2246 (arc-speed extrapolation agrees at 0.2204;
suspect laser said 0.2064, nominal 0.30). Direction residual 3.7 deg.
Final axis: type1 0 / 9.30 / 17.07 / 22.46 %, type2 0 / 8.88 / 16.82 /
19.72 %. Trajectory LOO on the final axis: 1.92 %p (max 4.2 at the
type2_pe00 endpoint). Summary figure:
`analysis/summary_model_20260715.png`.

EC-based estimation of eps0 (attempted 2026-07-15): scanning eps0 in the
two-driver fit (free complex alpha/beta per type; both residual- and
turning-angle-based metrics) is essentially FLAT — type2's predicted
turning is 1.13-1.17 deg for ANY eps0 in 0..0.3 (measured 1.29), so eps0
is NOT identifiable from the current EC trajectories. Structural reasons:
the free complex coefficients absorb most of the eps0 effect; only the
sigma-step SHAPE across 3 segments carries the signature, one node of
which is arc-imputed (which partially flattens exactly that shape); and
the sigma channel is a minor fraction of the trajectory motion. eps0 =
0.10 therefore remains sourced from the 4-wire resistivity fit (an
independent measurement, just not EC-derived). What would make EC-based
eps0 estimation work: low-strain type2 nodes (eps 0.02-0.06, where the
sqrt(eps0+eps) hardening-onset curvature fingerprint is strongest and
above our repeatability), denser sweeps, or a weighing-validated shared
constraint on beta across types.

Resistivity as the primary output (user proposal): adopted as a DERIVED
output — IMPLEMENTED in specimen_check (2026-07-15): every on-trajectory
readout now reports PE_area (delivered strain), eps_abs = eps0_type +
PE_area (absolute material state; type2 carries its +0.10 cold-work
offset), and rho via the universal law; NaN-safe for air/off-path states;
rows CSV gains eps_abs / rho_ohm_m columns. Round-trip verified on all 8
nodes. A true geometry-independent resistivity inversion (measured area
fixes the alpha channel, residual signal along beta gives rho directly,
making law deviations such as annealing detectable) becomes feasible once
weighing validates the two-driver decomposition; see 5.3/5.4.

## 6. Operational decisions (2026-07-15)

- AD3 replaces the EW-8SCT path; AMS_control runtime keeps the trajectory
  model. A-form retained offline for methodology comparison.
- Session procedure: empty-probe re-zero at session start (10 s), then
  measure; baseline-validity check planned (compare session air point to
  the stored reference baseline; if within tolerance no recalibration,
  else re-record the 7-min reference sweep). Wires whose area falls in the
  4.7-5.5 / 6.2-7.4 mm2 gap -> flag uncalibrated instead of interpolating.
- Re-record the reference after ANY probe/cable/fixture change (absolute
  trajectory SHAPE changes ~1 %; re-zero only absorbs common-mode shifts).
- Pending before production sign-off: overnight out-of-sample residual
  run on the new cable; AMS_control port of the new baseline
  (x=6.709282, y=-10.905688, |r|=12.804238 @27 C), temp model
  temp_response_20260715, and regenerated pe_ratio_model; subprocess
  isolation of the sensor read in AMS_control (libdwf segfault risk:
  in-process reads would take down the control loop; ECT_AD3 side is
  already supervised by drift_watchdog/specimen_check).

## 7. Data and analysis assets

- `data/measurement.csv` — consolidated per-specimen table (both batches,
  consensus laser dims, per-type coating, bare areas, geometry-implied
  strain, resistivity measured + eps0-model, AD3 27 C signal/deltas).
- `analysis/temp_response_20260715.json` — spare-cable temperature model.
- `analysis/ratio_probe_specimens_20260715.json` — active reference
  (refsweep_20260715, 27 C-corrected with the new temp model).
- `analysis/pe_ratio_model.json` — trajectory PE model (LOO 0.034).
- `analysis/residual_drift_tempsweep_20260715.{png,json}` — post-
  correction residual analysis.
- `analysis/pe_tempcorr_comparison_20260715.png` — per-row PE with/without
  temperature correction (8 panels; uncorrected-vs-27C-reference shows
  +0.03..+0.11 systematic error, corrected path RMSE 0.0011).
- Log naming convention: see README ("Log naming convention") — unified
  2026-07-15.
