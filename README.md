# ECT_AD3

AD3 (Analog Discovery 3) eddy-current testing front-end for plastic-strain
measurement: ratiometric lock-in driver, drift logger, and analysis tools.
Successor to the `../ECTv2/` experiments; validated on the real EW-8SCT
drive-pickup probe on 2026-07-10 (see `ISSUE_STATUS.md`).

Current AD3/sensor issue notes are tracked in
`ECT_AD3/ISSUE_STATUS.md`.

## Logger

```bash
python ECT_AD3/ad3_timeseries_logger.py
```

The logger defaults to `--mode ratio`: W1 drives the probe drive coil
continuously, CH1 reads the pickup coil, W2 outputs a start-synchronized twin
of the drive that is BNC-cabled into CH2 as the reference, and each row logs
the complex lock-in ratio pickup/reference. The ratio cancels capture start
phase, so no AWG-restart trigger is needed; this removed the restart
transients and most `DptiIO` USB errors of the legacy path. In ratio mode
`raw_x`/`raw_y` are dimensionless (signal/reference), and the logged
`signal_*`/`reference_*` fields are reference-phase-aligned per capture:
magnitudes are median(|.|), `signal_phase_deg` is the reference-relative
(= ratio) phase, and `reference_phase_deg` is ~0 by construction. With a BNC
T on the drive node, wire CH2 from the drive itself and pass
`reference_output_channel=None` in code (the twin drive is then unused).

Ratio-mode defaults (2026-07-10 probe standard): 120 kHz (measured probe
resonance), 4.0 V drive on W1/W2, 1 MS/s, buffer 16384 (two enabled channels
halve the shared 32768 buffer), `--averages 32` pooled captures per row,
reference on the high input range. See `ISSUE_STATUS.md` for measured
performance vs the EW-8SCT.

The legacy AWG-restart single-channel path remains available:

```bash
python ECT_AD3/ad3_timeseries_logger.py --mode triggered
```

Default output:

```text
ECT_AD3/logs/ad3_log_YYYYMMDD_HHMMSS.csv
```

Main columns:

- `number`, `timestamp`, `time_s`
- `width_mm`, `thickness_mm`
- `raw_x`, `raw_y`, `magnitude`, `phase_deg`, `vpp`, `signal_freq_hz`
- robust lock-in fields: `lockin_window_count`, `lockin_window_kept`,
  `lockin_window_rejected`, `lockin_window_phase_std_deg`,
  `lockin_window_magnitude_rel_std`
- quality fields: `row_status`, `row_error`, `ad3_retry_count`,
  `ad3_recovery_count`, `signal_freq_ok`, `drive_freq_error_hz`
- inferred segmentation fields: `run_id`, `block_id`, `lock_state`,
  `valid_signal`, `is_stable`, `stable_count`, `phase_unwrapped_deg`,
  `phase_center_deg`, `phase_delta_deg`, `magnitude_center`,
  `magnitude_delta`, `lock_event`
- `plastic_strain`
- `temp_c`, `humid_pct`
- `ad3_pcb_temp_c`, `ad3_fpga_temp_c`, `ad3_usb_v`, `ad3_usb_a`

Each run also writes:

- `ad3_log_YYYYMMDD_HHMMSS_blocks.csv`: one row per inferred stable/lock
  block, with block-level phase, magnitude, geometry, ambient, and AD3 board
  statistics. Use this file, not raw rows, for environment-coefficient fitting.
  When enough stable rows exist, the mean/std columns use only those stable
  rows and `summary_basis=stable`; otherwise they use all rows in the block and
  `accepted_for_env_fit=0`.
- `ad3_log_YYYYMMDD_HHMMSS_meta.json`: command-line arguments, paths,
  runtime platform, and git state when available.

AD3 board telemetry is read from WaveForms AnalogIO status nodes. On Analog
Discovery 3, the USB monitor channel exposes PCB temperature, FPGA die
temperature, USB voltage, and USB current. If the installed SDK/device variant
does not expose a node, the logger leaves that CSV field blank and continues.

## AD3 Code Layout

- `ad3_ratiometric_lockin.py`: continuous-drive ratiometric driver
  (default measurement path since 2026-07-10).
- `ad3_triggered_lockin.py`: legacy AWG-restart triggered driver; also owns
  the shared measurement dataclass.
- `ad3_lockin_measure.py`: short diagnostic logger and impedance/spectrum
  plot (`--mode ratio|triggered`).
- `ad3_timeseries_logger.py`: long drift logger with laser, ambient, board
  telemetry, and optional PE calculation (`--mode ratio|triggered`).

## Examples

Log AD3 only:

```bash
python ECT_AD3/ad3_timeseries_logger.py --no-laser --no-atmosphere
```

Fast two-coil ratiometric diagnostic run:

```bash
python ECT_AD3/ad3_lockin_measure.py --duration-s 12 --interval-s 0.5
```

Legacy W1-to-CH1 jumper diagnostic:

```bash
python ECT_AD3/ad3_lockin_measure.py --mode triggered --duration-s 12 --interval-s 0.5
```

Fast interactive impedance-plane view only:

```bash
python ECT_AD3/ad3_impedance_plane_viewer.py
```

This viewer skips CSV logging, Keyence, RN171, PE calculation, and block
segmentation. It uses the same triggered AD3 lock-in driver as the drift
logger, but defaults to `--averages 1` and `--interval-s 0` so the impedance
plane updates as fast as the AD3 capture loop allows. In the plot window, use
space to pause, `r` to clear the trace, and `q` or Esc to quit.

`--averages` performs row-internal repeated captures before writing one CSV
row (ratio mode default 32, triggered mode default 64), so use a slower
interval if the loop reports interval lag or AD3 recovery events. In ratio
mode the drive is never restarted between captures, so higher averages do not
provoke the restart-related USB errors seen in triggered mode.

The logger applies robustness inside each AD3 row only. Adjacent CSV rows are
independent: `raw_x`, `raw_y`, `magnitude`, and `phase_deg` are not rolling
filtered or smoothed across time.

The AD3 capture is demodulated in small integer-cycle windows. The driver
rejects outlier windows in the lock-in complex plane with a MAD gate, then
aggregates kept windows with a median. If the kept windows' phase spread is too
large, the row is marked as an AD3 error instead of recording a misleading
near-zero lock-in result.

Disable the internal lock-in window filter for diagnostics:

```bash
python ECT_AD3/ad3_timeseries_logger.py --no-lockin-window-filter
```

Tune the internal lock-in filter:

```bash
python ECT_AD3/ad3_timeseries_logger.py \
  --lockin-window-cycles 15 \
  --lockin-window-mad-threshold 6 \
  --max-lockin-window-phase-std-deg 5
```

AD3/WaveForms can occasionally leave AnalogIn in a bad state after a fast
triggered capture loop. The logger now applies recovery between retries:

- retry 1: soft reset/reconfigure AnalogIn/AnalogOut
- retry 2 by default: close/reopen the AD3 handle

Recovery attempts are recorded in `ad3_recovery_count`.

```bash
python ECT_AD3/ad3_timeseries_logger.py \
  --ad3-retries 2 --ad3-recover-delay-s 0.25 --ad3-hard-reopen-after 2
```

For long drift collection with laser and RN171 enabled, prefer `--interval-s`
of at least `0.5` s; use slower intervals such as `5` s for environment drift
tests.

Current AD3-tested internal sampling option:

```bash
python ECT_AD3/ad3_timeseries_logger.py \
  --interval-s 0.5 \
  --sample-rate-hz 1000000 \
  --buffer-size 30000 \
  --lockin-window-cycles 15 \
  --lockin-window-mad-threshold 6 \
  --lockin-min-windows 4 \
  --averages 16\
  --no-pe
```

On this AD3/WaveForms setup, requested analog-in buffers above the device limit
are rejected at startup instead of being silently demodulated. A request such as
`--buffer-size 50000` or `--buffer-size 192000` is clamped by WaveForms to
`32768`, so it is not used for drift logging.

Short stability smoke test:

```bash
python ECT_AD3/ad3_timeseries_logger.py \
  --duration-s 8 --interval-s 0.25 --no-laser --no-atmosphere --no-pe --no-plot
```

Use W2 and CH2:

```bash
python ECT_AD3/ad3_timeseries_logger.py --output-channel 1 --input-channel 1
```

Use a 3.6 second interval similar to the EW-8SCT serial drift dataset:

```bash
python ECT_AD3/ad3_timeseries_logger.py --interval-s 3.6
```

Override sensor addresses without editing source:

```bash
KEYENCE_HOST=192.168.0.111 RN171_HOST=192.168.0.51 \
python ECT_AD3/ad3_timeseries_logger.py
```

Disable PE calculation while collecting raw drift:

```bash
python ECT_AD3/ad3_timeseries_logger.py --no-pe
```

If a valid sensor state is being rejected because the dominant spectral peak is
not the drive bin, keep the raw quality fields and explicitly opt into stable
classification:

```bash
python ECT_AD3/ad3_timeseries_logger.py --allow-off-peak-stable
```

## PE Calibration

The AMF PE model is maintained under `calibration/pe/`:

```text
PE = C_PHASE*phase + C_AMP*amp + C_AREA*area + C_PXAR*(phase*area) + C_OFFSET
```

The same environmental correction terms from AMF are supported:

```text
PE_corr = PE + ENV_C1*dT + ENV_C2*dT^2 + ENV_C3*dH + ENV_C4*dH^2 + ENV_C5*dT*dH
```

Initial coefficients in `ECT_AD3/config.yaml` mirror the current AMF
`PE_CALIBRATION` block. These are useful for plumbing tests, but AD3 needs its
own fitted coefficients before the `plastic_strain` column should be treated as
calibrated.

Fit coefficients from one or more calibration CSV files:

```bash
python ECT_AD3/calibration/pe/fit_pe_from_csv.py calibration.csv --output ECT_AD3/config.yaml
```

For the eight-specimen manual workflow, use the interactive entrypoint:

```bash
python ECT_AD3/calibration/pe/interactive_fit_8_specimens.py
```

Both fit entrypoints write a JSON report by default at
`ECT_AD3/calibration/pe/pe_calibration_latest.json`, preserving the logger
metadata and measurement options used as the calibration standard.

Calibration CSVs should contain:

- target column: `specimen_pct` by default
- signal columns: `raw_x`, `raw_y`
- geometry columns: `width_mm`, `thickness_mm`
- optional environment columns: `temp_c`, `humid_pct`

Alternative AMF-style column names such as `ect_x`, `ect_y`, `laser1_mm`, and
`laser2_mm` are also accepted.

## Drift attribution

For long no-specimen runs with ambient sensors, decompose observed drift into
environment-driven vs pure sensor drift (commonality analysis, per-channel
unique contributions, thermal-lag scan, env-corrected residual):

```bash
python ECT_AD3/analysis/drift_attribution.py drift24_20260710_01 --skip-first-min 30
```

The verdict distinguishes environment-driven, pure drift, mixed, and
"ambiguous (need diurnal cycles)" cases, and prints collinearity and
low-excitation cautions rather than over-attributing.
