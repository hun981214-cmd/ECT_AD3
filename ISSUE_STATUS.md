# ECT_AD3 Status

Last updated: 2026-07-15 (110 kHz spare-cable standard; log naming unified)

Log naming was unified on 2026-07-15 (see README "Log naming convention"):
`spec_*` -> `refsweep_20260710_*`, `ref2_*` -> `refsweep_20260714_*`,
`ref3_*` -> `refsweep_20260715_*`, `ratio_probe_20260710_01` /
`drift24_20260710_01` -> `_s01`, `return_check_01` ->
`return_check_20260710_s01`, `probe_state_check` ->
`probe_state_check_20260714_s01`, `ratio_probe_smoke_01` ->
`smoke_20260710_s01`, `specimen_check_*` -> `ad3_speccheck_*`. Run ids below
use the new names; each renamed meta JSON records its `renamed_from`.

## Goal

Use Analog Discovery 3 as the ECT measurement front-end with stability and
specimen sensitivity equal to or better than the original EW-8SCT path.

## Status: validated on the real probe

On 2026-07-10 the actual EW-8SCT drive-pickup probe was moved onto the AD3
BNC adapter and the full chain was validated end to end: continuous-drive
ratiometric lock-in, probe resonance characterization, no-specimen baseline,
and an 8-specimen sensitivity sweep (two wire types x 0/10/20/30 % plastic
strain). The probe is currently connected to the AD3, not the EW-8SCT.

## Wiring (current; unchanged since 2026-07-10 except the probe-side cable)

- W1 BNC: probe drive coil (via the SPARE probe cable since 2026-07-15)
- CH1 BNC: probe pickup coil
- W2 BNC -> CH2 BNC: direct cable; W2 outputs a clock- and start-synchronized
  twin of the drive as the ratio reference ("option B"). With a BNC T on W1
  the reference could tap the true drive node instead
  (`reference_output_channel=None`).

## Measurement standard (logger `--mode ratio` defaults, 2026-07-15)

- 110 kHz continuous sine, 0.15 V amplitude on W1 and W2
  (spare-cable probe resonance measured 2026-07-15: 12.5 V/V peak gain;
  pickup ~1.9 V at 0.15 V drive, 33 % clip margin on the low input range)
- CH1 signal and CH2 reference both on the low input range (the driver picks
  the high range only for drives above 1.2 V)
- 1 MS/s, buffer 16384 per channel, `--averages 32` pooled captures per row
- pooled per-window complex ratios, MAD-median aggregation, clipping /
  reference-magnitude / phase-spread guards, in-row capture retries
- superseded standard (2026-07-10..14, original cable): 120 kHz, 4.0 V
  (pickup peak 114 mV per 1 V drive at 120 kHz, ~5x the 100 kHz response,
  Q ~ 20); all `*_2026071{0,3,4}` runs were recorded at it

## Root cause history

- The 2026-05-04 "sensor path sees 9 kHz, not the drive" blocker was wiring
  state, not electronics: the 2026-07 fixture shows a clean drive-pickup
  transformer path. The residual ambient 9 kHz on CH1 is ~1.6 mV and is
  rejected by lock-in demodulation away from 9 kHz.
- The legacy driver restarted the AWG every capture for trigger phase. The
  restarts caused amplitude-settling noise and most `DptiIO ERC:10` USB
  errors. The ratio cancels capture phase, so the continuous-drive driver
  (`ad3_ratiometric_lockin.py`) removes both problems; remaining sporadic USB
  glitches are absorbed by in-row capture retries plus soft/hard recovery
  escalation (row loss ~1-2 %, no level shifts after recovery).

## Measured performance vs EW-8SCT

EW-8SCT baseline (26.7 h no-specimen raw drift log, 10 s cadence):
amplitude step noise ~0.10 % of |Z|, phase-equivalent noise ~0.09 deg,
raw p95-p05 drift 6.2 % (uncorrected).

AD3 ratio probe (no specimen, 5 min, 2 s cadence,
`ad3_log_ratio_probe_20260710_s01.csv`): amplitude step noise 0.0076 %
(13x better), phase step noise 0.0013 deg (~70x better). Slow drift tracks
AD3 PCB warm-up; start long runs warm or port the ECT raw-space env
correction.

Specimen sweep (30 s per specimen, runs `refsweep_20260710_t{1,2}_pe{00,10,20,30}`,
`refsweep_20260710_t1_pe20_r2` replaces a failed first attempt):

- Both types are strictly monotonic in the complex ratio plane vs plastic
  strain; mean complex step per 10 % PE = 2.3 % (type1) / 2.0 % (type2) of
  the baseline magnitude, 67-81x the worst within-run sigma.
- All 8 specimens separate by >= 82x the per-row noise sigma (step-noise
  based, drift-free) and >= 25x the within-run window sigma (which includes
  post-placement settling). EW-8SCT 2026-03-31 8-specimen logs give
  ~350-450x their within-file sigma - same class of discriminability (both
  far beyond any confusion), with the AD3 raw noise floor an order of
  magnitude lower in relative terms.
- Analysis: `analysis/analyze_ratio_probe_specimens.py` ->
  `analysis/ratio_probe_specimens_20260710.{png,json}`.

## Reference (current)

The active reference sweep is `ratio_probe_specimens_20260714.json`
(runs `refsweep_20260714_*`), re-recorded 2026-07-14 in the FINAL fixture state (Keyence
laser mounted) after fixture handling was shown to shift trajectories ~1 %.
Reference x/y are stored temperature-corrected to 27 C - the same space the
runtime compares in; storing raw values let a ~1 C ambient drift during the
sweep contaminate the trajectories (that bug cost ~5 %p on type2 before the
fix). With the matched reference, no session anchor is needed: a live
`t2_pe30` read PE 28.0-28.4 / nearest `type2_pe30` with only the automatic
empty-probe baseline. Re-record the reference (7 min) whenever the
probe/fixture geometry is disturbed.

## Standard change 2026-07-15 (spare probe cable)

The original probe cable failed after a power cycle (probe path dead on any
AWG; probe flip suspected of loosening a lead). A spare cable revived the
path with a very different transfer: resonance moved 120 -> 110 kHz and the
peak gain rose 0.114 -> 12.5 V/V (lower-loss cable). New standard: 110 kHz,
0.15 V drive (pickup ~1.9 V, clip margin 33 %), air |ratio| ~12.83,
specimens 10.59-11.51 (both types strictly monotonic). Reference `refsweep_20260715_*`,
outputs `analysis/ratio_probe_specimens_20260715.*`, PE model refit
(trajectory LOO 3.4 %p; the linear sensor_cal form now scores 2.9 but lacks
the air-rejection gate, so the runtime keeps the trajectory model).
OPEN: the temperature model (`temp_response_20260713.json`) belongs to the
OLD cable/scale and is effectively a no-op now - re-run the A/C sweep and a
long air run on the new standard, then re-verify residual drift.

## Open items

- Return-to-baseline check: DONE 2026-07-10 (`return_check_20260710_s01`): after the
  specimen sweep the no-specimen point returned to within 0.48 % of the
  morning baseline (1/60 of the smallest specimen effect); the residual moves
  with board temperature and humidity and is part of the drift-attribution
  question below.
- 24 h drift attribution: DONE 2026-07-11. Run covered 2026-07-10 14:52 to
  2026-07-11 14:53 KST at 10 s cadence in 6 segments (libdwf segfaulted the
  process 5 times during DptiIO storms; `drift_watchdog.py` restarted each
  within 15 s; 8477 usable rows over 23.84 h). Verdict (magnitude):
  ENVIRONMENT-DRIVEN - 70 % of variance explained, env-unique 52 %p,
  shared 15 %p, pure-time drift only 3 %p (phase: env-unique 75 %p). The
  dominant driver is the room HVAC's ~45 min temperature cycle
  (26.0-27.1 C; corr -0.92 with |ratio|); humidity's unique share is small
  once temperature is accounted for. Raw p95-p05 drift = 0.76 %/24 h
  (EW-8SCT raw: 6.2 %/26.7 h, ~8x worse); a static linear env correction
  brings it to 0.50 %. The signal LEADS the RN171 reading by ~4 min (probe
  coil responds to air faster than the boxed sensor), producing the
  hysteresis loop in the temp-vs-signal scatter - a lag-compensated or
  dynamic env model is the next accuracy lever. Outputs:
  `analysis/drift_attribution_drift24_20260710_full.{png,json}`.
- Warm-up handling for long drift runs: either a settle period before
  logging or an AD3-PCB-temperature term in a new env correction fit.
- Temperature characterization: DONE 2026-07-13 (A/C sweep 27.2 -> 19.9 C,
  7.1 C span, runs `tempsweep_20260713_s01,s03,s04`, analyzed by
  `analysis/temp_response.py`, adversarially verified by 3 independent
  re-derivations). Results: thermal lag -4.8 min (probe LEADS the RN171
  reading; matches the ~-4 min seen in the 24 h HVAC cycles); global
  lag-compensated sensitivity -0.399 %/C (|ratio|) and +0.322 deg/C (phase);
  the response is measurably CURVED - local slope -0.24 %/C at 20 C to
  -0.65 %/C at 27 C, which reconciles the drift24-implied -0.46..-0.63 %/C
  in the 26-27 C regime. Use the lag-compensated quadratic T + T^2 (+H for
  phase) as the correction model: raw 2.14 % span -> 0.20 % in-sample,
  ~0.3-0.4 % expected deployed (out-of-sample split test). Humidity is
  negligible for magnitude but real for phase (+0.05 deg/%RH).
- Env-correction productization: wire the lag-compensated quadratic
  coefficients from `analysis/temp_response_20260713.json` into a runtime
  correction (and re-fit after any probe/fixture change).
- Post-correction residual drift: ANALYZED 2026-07-14
  (`analysis/residual_drift.py` on the 23.5 h air run; conclusions
  adversarially verified by 3 independent re-derivations). After the best
  self-fitted T/H(+pcb) correction a STRUCTURED residual remains: total span
  ~0.52 %, slow component (30-min block means) ~0.33 % - hundreds of times
  the 0.007 % white-noise floor - dominated by the partially-cancelled
  ~45 min HVAC line (2-4.6x attenuation is the limit of the single-pole
  lag/RC model family) plus hours-scale wander. No sensor aging demonstrated:
  the self-fit trend is +0.09 +- 0.21 %/24h (n.s., bootstrap-robust); the
  deployed sweep model leaves +0.38 %/24h but split-half transport alone
  produces spurious trends of -0.24..+0.75 %/24h, so that is model-transport
  error, not aging. ad3_usb_v correlates with the residual (+0.3) but was
  REFUTED as a cause (time/temperature proxy; coefficient non-transportable;
  forward OOS degrades) - keep it as a diagnostic, never in corrections.
  Next accuracy lever: a multi-pole / two-time-constant thermal model, or
  co-locating a faster temperature sensor with the probe.
- Data-driven PE calibration for the ratio path (the 8 specimen runs are the
  seed data); `config.yaml` PE coefficients still belong to the EW-8SCT
  path and do not apply to AD3 ratios.
- A >= 24 h drift run on the probe for a true apples-to-apples drift number
  against the EW-8SCT 26.7 h log.
- The EW-8SCT currently has no probe attached; decide which instrument owns
  the probe going forward.
