# ECT_AD3 — Agent Guide

AD3 (Digilent Analog Discovery 3) based eddy-current testing front-end for
plastic-strain measurement. Successor to the `../ECTv2/` experiments; the
ratiometric driver, logger, and 2026-07-10 probe validation live here.

See canonical standards: [../dotfiles/agent/WORKSPACE_AGENT_GUIDE_LITE.md](../dotfiles/agent/WORKSPACE_AGENT_GUIDE_LITE.md).

## Entrypoints

- Drift/measurement logger: `python ad3_timeseries_logger.py` (defaults to
  `--mode ratio`, the probe standard)
- Short diagnostic: `python ad3_lockin_measure.py`
- Live impedance-plane viewer: `python ad3_impedance_plane_viewer.py`
- Sensor facade for calibration tools: `live_sensors.py`
- Analysis: [analysis/](analysis/) (specimen sensitivity, drift attribution)

Run everything with the Conda python (`/home/nvidia/miniconda3/bin/python3`);
the system `/bin/python3.9` has a broken NumPy.

## Hardware state (2026-07-10)

- The EW-8SCT drive-pickup probe is connected to the AD3 BNC adapter:
  W1 = drive coil, CH1 = pickup coil, W2 -> CH2 BNC cable = twin-drive
  reference. The EW-8SCT instrument currently has NO probe attached.
- Probe resonance: 120 kHz (standard drive frequency).
- The AD3 is a single-open device: only one process can hold it. Check for a
  running logger (`pgrep -f ad3_timeseries_logger`) before starting anything
  that opens the AD3.

## Don't touch

- [logs/](logs/): measurement data, append-only. Never delete or rewrite.
- A long drift run may be in progress; do not stop it without asking.

## Conventions

- `ISSUE_STATUS.md` tracks current hardware/driver status and open items —
  update it when the standard or wiring changes.
- Long-run analysis outputs go to [analysis/](analysis/) as
  `<topic>_<runid>.{png,json}`; PNG/CSV outputs are gitignored, scripts and
  JSON summaries are committed.
- Record run metadata via the logger's `--run-id`; the newest
  `ad3_log_*_meta.json` referenced by `live_sensors.LATEST_STANDARD_META` is
  the measurement standard.
