# ECT_AD3 Status

Last updated: 2026-07-10 (probe validation complete)

## Goal

Use Analog Discovery 3 as the ECT measurement front-end with stability and
specimen sensitivity equal to or better than the original EW-8SCT path.

## Status: validated on the real probe

On 2026-07-10 the actual EW-8SCT drive-pickup probe was moved onto the AD3
BNC adapter and the full chain was validated end to end: continuous-drive
ratiometric lock-in, probe resonance characterization, no-specimen baseline,
and an 8-specimen sensitivity sweep (two wire types x 0/10/20/30 % plastic
strain). The probe is currently connected to the AD3, not the EW-8SCT.

## Wiring (current, 2026-07-10)

- W1 BNC: probe drive coil
- CH1 BNC: probe pickup coil
- W2 BNC -> CH2 BNC: direct cable; W2 outputs a clock- and start-synchronized
  twin of the drive as the ratio reference ("option B"). With a BNC T on W1
  the reference could tap the true drive node instead
  (`reference_output_channel=None`).

## Measurement standard (logger `--mode ratio` defaults)

- 120 kHz continuous sine, 4.0 V amplitude on W1 and W2
  (probe resonance measured 2026-07-10: pickup peak 114 mV per 1 V drive at
  120 kHz, ~5x the 100 kHz response; Q ~ 20)
- CH1 signal on the low input range, CH2 reference on the high range
- 1 MS/s, buffer 16384 per channel, `--averages 32` pooled captures per row
- pooled per-window complex ratios, MAD-median aggregation, clipping /
  reference-magnitude / phase-spread guards, in-row capture retries

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
`ad3_log_ratio_probe_20260710_01.csv`): amplitude step noise 0.0076 %
(13x better), phase step noise 0.0013 deg (~70x better). Slow drift tracks
AD3 PCB warm-up; start long runs warm or port the ECT raw-space env
correction.

Specimen sweep (30 s per specimen, runs `spec_t{1,2}_pe{00,10,20,30}`,
`spec_t1_pe20_r2` replaces a failed first attempt):

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

## Open items

- Return-to-baseline check: DONE 2026-07-10 (`return_check_01`): after the
  specimen sweep the no-specimen point returned to within 0.48 % of the
  morning baseline (1/60 of the smallest specimen effect); the residual moves
  with board temperature and humidity and is part of the drift-attribution
  question below.
- 24 h no-specimen drift run started 2026-07-10 14:52 KST at 10 s cadence
  with RN171 ambient logging. Segment `drift24_20260710_01` died after ~6 h
  when libdwf segfaulted the process during a DptiIO error storm; logging
  now runs under `drift_watchdog.py`, which restarts crashed segments with
  sequential run ids (`drift24_20260710_s02`, ...). Analyze with
  `analysis/drift_attribution.py drift24_20260710_01,drift24_20260710_s02,...`
  (comma-stitched segments, `--skip-first-min 10`).
  Early readout on the first ~40 min: environment-driven (95 % of variance
  explained; env-unique 85 %p, humidity dominant with unique dR2 0.21;
  env correction cuts p95-p05 drift 0.50 % -> 0.13 %). Confirm after the
  full 24 h with diurnal cycles before treating the split as final.
- Warm-up handling for long drift runs: either a settle period before
  logging or an AD3-PCB-temperature term in a new env correction fit.
- Data-driven PE calibration for the ratio path (the 8 specimen runs are the
  seed data); `config.yaml` PE coefficients still belong to the EW-8SCT
  path and do not apply to AD3 ratios.
- A >= 24 h drift run on the probe for a true apples-to-apples drift number
  against the EW-8SCT 26.7 h log.
- The EW-8SCT currently has no probe attached; decide which instrument owns
  the probe going forward.
