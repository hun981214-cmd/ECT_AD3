#!/usr/bin/env python3
"""Live sensor facade for ECTv2.

This is the ECTv2 counterpart to AMF's `Sensors` wrapper. It reads AD3 ECT,
Keyence geometry, and RN171 temperature/humidity through the same drivers used
by `ad3_timeseries_logger.py`, but exposes one `read_all()` call for calibration
tools.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
# Pinned to the newest run recorded at the CURRENT measurement standard
# (2026-07-15 spare-cable probe: 110 kHz / 0.15 V). Re-pin whenever the
# standard changes; a stale pin here silently configures old drive settings.
LATEST_STANDARD_META = SCRIPT_DIR / "logs" / "ad3_log_refsweep_20260715_baseline_meta.json"

# Per-run I/O toggles are run choices, not part of the measurement standard;
# never inherit them from the pinned meta (the baseline run used --no-laser).
_NON_STANDARD_ARGS = ("no_laser", "no_atmosphere", "no_pe")

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ad3_timeseries_logger import (  # noqa: E402
    TriggeredAD3ECTReader,
    open_atmosphere,
    open_laser,
    read_ad3_with_retries,
    read_atmosphere,
    read_laser,
)


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def load_standard_args(path: Path = LATEST_STANDARD_META) -> dict[str, Any]:
    if not path.exists():
        return {}
    metadata = json.loads(path.read_text(encoding="utf-8"))
    args = dict(metadata.get("args", {}))
    for key in _NON_STANDARD_ARGS:
        args.pop(key, None)
    return args


def _arg(name: str, overrides: dict[str, Any], standard: dict[str, Any], default: Any) -> Any:
    if name in overrides and overrides[name] is not None:
        return overrides[name]
    if name in standard and standard[name] is not None:
        return standard[name]
    return default


def build_sensor_args(**overrides: Any) -> argparse.Namespace:
    """Build an argparse-like namespace from the latest standard metadata."""
    standard = load_standard_args()
    reference_channel = _arg("reference_channel", overrides, standard, 1)
    reference_output_channel = _arg("reference_output_channel", overrides, standard, 1)
    reference_range_v = _arg("reference_range_v", overrides, standard, None)
    values = {
        "mode": str(_arg("mode", overrides, standard, "ratio")),
        "output_channel": int(_arg("output_channel", overrides, standard, 0)),
        "input_channel": int(_arg("input_channel", overrides, standard, 0)),
        "reference_channel": None if reference_channel is None else int(reference_channel),
        "reference_output_channel": (
            None if reference_output_channel is None else int(reference_output_channel)
        ),
        "reference_range_v": None if reference_range_v is None else float(reference_range_v),
        "drive_freq_hz": float(_arg("drive_freq_hz", overrides, standard, 120_000.0)),
        "drive_amplitude_v": float(_arg("drive_amplitude_v", overrides, standard, 4.0)),
        "sample_rate_hz": float(_arg("sample_rate_hz", overrides, standard, 1_000_000.0)),
        "buffer_size": int(_arg("buffer_size", overrides, standard, 16_384)),
        "analog_range_v": float(_arg("analog_range_v", overrides, standard, 5.0)),
        "averages": int(_arg("averages", overrides, standard, 32)),
        "timeout_s": float(_arg("timeout_s", overrides, standard, 5.0)),
        "discard_s": float(_arg("discard_s", overrides, standard, 0.001)),
        "lockin_window_cycles": int(_arg("lockin_window_cycles", overrides, standard, 15)),
        "lockin_window_mad_threshold": float(_arg("lockin_window_mad_threshold", overrides, standard, 6.0)),
        "lockin_min_windows": int(_arg("lockin_min_windows", overrides, standard, 4)),
        "no_lockin_window_filter": bool(_arg("no_lockin_window_filter", overrides, standard, False)),
        "max_lockin_window_phase_std_deg": float(_arg("max_lockin_window_phase_std_deg", overrides, standard, 5.0)),
        "ad3_retries": int(_arg("ad3_retries", overrides, standard, 2)),
        "ad3_retry_delay_s": float(_arg("ad3_retry_delay_s", overrides, standard, 0.1)),
        "ad3_recover_delay_s": float(_arg("ad3_recover_delay_s", overrides, standard, 0.25)),
        "ad3_hard_reopen_after": int(_arg("ad3_hard_reopen_after", overrides, standard, 2)),
        "signal_freq_tolerance_hz": float(_arg("signal_freq_tolerance_hz", overrides, standard, 50.0)),
        "laser_host": _arg("laser_host", overrides, standard, "192.168.0.111"),
        "laser_port": int(_arg("laser_port", overrides, standard, 64000)),
        "laser_timeout": float(_arg("laser_timeout", overrides, standard, 2.0)),
        "laser_settle_s": float(_arg("laser_settle_s", overrides, standard, 0.08)),
        "atmo_host": _arg("atmo_host", overrides, standard, "192.168.0.34"),
        "atmo_port": int(_arg("atmo_port", overrides, standard, 502)),
        "no_laser": bool(_arg("no_laser", overrides, standard, False)),
        "no_atmosphere": bool(_arg("no_atmosphere", overrides, standard, False)),
    }
    return argparse.Namespace(**values)


class ECTv2LiveSensors:
    """Unified live readout for AD3 ECT, Keyence geometry, and RN171 env."""

    def __init__(self, *, open_now: bool = True, **overrides: Any) -> None:
        self.args = build_sensor_args(**overrides)
        self.ad3 = TriggeredAD3ECTReader(
            mode=self.args.mode,
            output_channel=self.args.output_channel,
            input_channel=self.args.input_channel,
            reference_channel=self.args.reference_channel,
            reference_output_channel=self.args.reference_output_channel,
            reference_range_v=self.args.reference_range_v,
            drive_freq_hz=self.args.drive_freq_hz,
            drive_amplitude_v=self.args.drive_amplitude_v,
            sample_rate_hz=self.args.sample_rate_hz,
            buffer_size=self.args.buffer_size,
            analog_range_v=self.args.analog_range_v,
            averages=self.args.averages,
            timeout_s=self.args.timeout_s,
            discard_s=self.args.discard_s,
            lockin_window_cycles=self.args.lockin_window_cycles,
            lockin_window_mad_threshold=self.args.lockin_window_mad_threshold,
            lockin_min_windows=self.args.lockin_min_windows,
            lockin_window_filter=not self.args.no_lockin_window_filter,
            max_lockin_window_phase_std_deg=self.args.max_lockin_window_phase_std_deg,
        )
        self.laser = None
        self.atmo = None
        self._opened = False
        if open_now:
            self.open()

    def open(self) -> None:
        if self._opened:
            return
        self.ad3.open()
        self.laser = open_laser(self.args)
        self.atmo = open_atmosphere(self.args)
        self._opened = True

    def read_all(self) -> dict[str, Any]:
        if not self._opened:
            self.open()
        thickness_mm, width_mm = read_laser(self.laser)
        temp_c, humid_pct = read_atmosphere(self.atmo)
        ect, row_status, row_error, retry_count, recovery_count = read_ad3_with_retries(
            self.ad3,
            retries=self.args.ad3_retries,
            retry_delay_s=self.args.ad3_retry_delay_s,
            recover_delay_s=self.args.ad3_recover_delay_s,
            hard_reopen_after=self.args.ad3_hard_reopen_after,
        )
        board = self.ad3.read_board_status()
        drive_freq_error_hz = (
            float(ect["signal_freq_hz"]) - self.args.drive_freq_hz
            if _finite(ect.get("signal_freq_hz"))
            else float("nan")
        )
        signal_freq_ok = int(_finite(drive_freq_error_hz) and abs(drive_freq_error_hz) <= self.args.signal_freq_tolerance_hz)

        raw_x = ect.get("raw_x", float("nan"))
        raw_y = ect.get("raw_y", float("nan"))
        phase_rad = math.atan2(float(raw_y), float(raw_x)) if _finite(raw_x) and _finite(raw_y) else float("nan")

        return {
            "raw_x": raw_x,
            "raw_y": raw_y,
            "ect_raw_x": raw_x,
            "ect_raw_y": raw_y,
            "ect_phase": phase_rad,
            "ect_amp": ect.get("magnitude", float("nan")),
            "phase_deg": ect.get("phase_deg", float("nan")),
            "magnitude": ect.get("magnitude", float("nan")),
            "thickness_mm": thickness_mm,
            "width_mm": width_mm,
            "laser1_mm": thickness_mm,
            "laser2_mm": width_mm,
            "temp_c": temp_c,
            "humid_pct": humid_pct,
            "temperature": temp_c,
            "humidity": humid_pct,
            "row_status": row_status,
            "row_error": row_error,
            "ad3_retry_count": retry_count,
            "ad3_recovery_count": recovery_count,
            "signal_freq_ok": signal_freq_ok,
            "drive_freq_error_hz": drive_freq_error_hz,
            **ect,
            **board,
        }

    def close(self) -> None:
        try:
            self.ad3.close()
        finally:
            self._opened = False
        for sensor in (self.laser, self.atmo):
            close = getattr(sensor, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "ECTv2LiveSensors":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


Sensors = ECTv2LiveSensors


if __name__ == "__main__":
    with ECTv2LiveSensors() as sensors:
        print(sensors.read_all())
