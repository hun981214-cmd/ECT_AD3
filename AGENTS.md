# ECT_AD3 — Agent Guide

AD3 (Digilent Analog Discovery 3) based eddy-current testing front-end for
plastic-strain measurement. Successor to the `../ECTv2/` experiments; the
ratiometric driver, logger, and 2026-07-10 probe validation live here.

See canonical standards: [../Agents/standards/WORKSPACE_AGENT_GUIDE_LITE.md](../Agents/standards/WORKSPACE_AGENT_GUIDE_LITE.md).

## Profile

`hardware-control`

## Entrypoints

- Drift/measurement logger: `uv run python ad3_timeseries_logger.py` (defaults to
  `--mode ratio`, the probe standard)
- Short diagnostic: `uv run python ad3_lockin_measure.py`
- Live impedance-plane viewer: `uv run python ad3_impedance_plane_viewer.py`
- Sensor facade for calibration tools: `live_sensors.py`
- Analysis: [analysis/](analysis/) (specimen sensitivity, drift attribution)

Run everything through the uv venv (`uv run python ...` or `.venv/bin/python`),
built on the JetPack system Python 3.8: `uv venv --python /usr/bin/python3 &&
uv sync --group dev`. Do not use the system `/bin/python3.9` (broken NumPy)
or the retired conda base interpreter.

## Hardware state (2026-07-15)

- The EW-8SCT drive-pickup probe is connected to the AD3 BNC adapter:
  W1 = drive coil, CH1 = pickup coil, W2 -> CH2 BNC cable = twin-drive
  reference. The EW-8SCT instrument currently has NO probe attached.
- Probe resonance: 110 kHz (standard drive frequency) at 0.15 V drive.
  The SPARE probe cable is installed since 2026-07-15 (the original failed);
  it has ~34x the old transfer gain, so never drive it at the old 4.0 V —
  that clips the pickup. The superseded 2026-07-10 standard was 120 kHz/4.0 V.
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
- Record run metadata via the logger's `--run-id`, named per the README "Log
  naming convention" (`<campaign>_<YYYYMMDD>[_<detail>][_sNN]`).
- `live_sensors.LATEST_STANDARD_META` pins the meta JSON that defines the
  measurement standard; re-pin it whenever the standard changes.
