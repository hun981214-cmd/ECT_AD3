#!/usr/bin/env python3
"""
AD3 absolute-probe ECT time-series logger.

Logs raw impedance-plane signals together with geometry, ambient environment,
and Analog Discovery 3 board telemetry for drift analysis.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = SCRIPT_DIR.parent
SF_DIR = WORKSPACE_DIR / "SF"
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))
if str(SF_DIR) not in sys.path:
    sys.path.append(str(SF_DIR))

try:
    from AMF.io.keyence_laser import KeyenceLaser
except Exception:  # pragma: no cover - optional hardware dependency
    try:
        from keyence_laser import KeyenceLaser
    except Exception:
        KeyenceLaser = None

try:
    from AMF.io.rn171_sensor import RN171WCSensor
except Exception:  # pragma: no cover - optional hardware dependency
    try:
        from rn171_sensor import RN171WCSensor
    except Exception:
        RN171WCSensor = None

try:
    from pe_calibration import compute_pe, load_pe_config
except Exception:  # pragma: no cover - optional dependency during partial installs
    compute_pe = None
    load_pe_config = None

try:
    from ad3_triggered_lockin import AD3TriggeredLockIn
except Exception:  # pragma: no cover - optional hardware dependency
    AD3TriggeredLockIn = None

try:
    from ad3_ratiometric_lockin import AD3ContinuousRatioLockIn
except Exception:  # pragma: no cover - optional hardware dependency
    AD3ContinuousRatioLockIn = None

MODE_DEFAULTS = {
    # Continuous-drive ratiometric probe path: W1 drive coil, CH1 pickup,
    # W2 twin-drive reference BNC-cabled into CH2. 120 kHz is the measured
    # probe resonance (2026-07-10).
    "ratio": {
        "output_channel": 0,
        "input_channel": 0,
        "reference_channel": 1,
        "reference_output_channel": 1,
        "drive_freq_hz": 110_000.0,
        "drive_amplitude_v": 0.15,
        "buffer_size": 16_384,
        "averages": 32,
    },
    # Legacy AWG-restart triggered single-channel path.
    "triggered": {
        "output_channel": 0,
        "input_channel": 0,
        "reference_channel": None,
        "drive_freq_hz": 10_000.0,
        "drive_amplitude_v": 1.0,
        "buffer_size": 10_000,
        "averages": 64,
    },
}


def csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    try:
        if not math.isfinite(float(value)):
            return ""
    except (TypeError, ValueError):
        return value
    return f"{float(value):.9g}"


def finite_float(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def phase_diff_deg(value_deg: float, reference_deg: float) -> float:
    return ((value_deg - reference_deg + 180.0) % 360.0) - 180.0


def stats(values: list[float]) -> dict[str, float]:
    clean = [float(v) for v in values if finite_float(v)]
    if not clean:
        return {"mean": float("nan"), "std": float("nan"), "min": float("nan"), "max": float("nan"), "range": float("nan")}
    arr = np.array(clean, dtype=float)
    return {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "range": float(np.max(arr) - np.min(arr)),
    }


def current_git_state(path: Path) -> dict[str, object]:
    def run_git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    try:
        root = run_git("rev-parse", "--show-toplevel")
        commit = run_git("rev-parse", "HEAD")
        status_text = run_git("status", "--short")
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    return {
        "available": True,
        "root": root,
        "commit": commit,
        "dirty": bool(status_text),
        "status_short": status_text,
    }


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


BLOCK_SUMMARY_FIELDNAMES = [
    "run_id",
    "block_id",
    "lock_state",
    "start_reason",
    "end_reason",
    "start_number",
    "end_number",
    "start_timestamp",
    "end_timestamp",
    "start_time_s",
    "end_time_s",
    "duration_s",
    "sample_count",
    "valid_signal_count",
    "stable_sample_count",
    "accepted_for_env_fit",
    "summary_basis",
    "summary_sample_count",
    "summary_start_number",
    "summary_end_number",
    "phase_mean_deg",
    "phase_std_deg",
    "phase_min_deg",
    "phase_max_deg",
    "phase_range_deg",
    "magnitude_mean",
    "magnitude_std",
    "magnitude_range",
    "raw_x_mean",
    "raw_x_std",
    "raw_y_mean",
    "raw_y_std",
    "width_mm_mean",
    "width_mm_std",
    "thickness_mm_mean",
    "thickness_mm_std",
    "temp_c_mean",
    "temp_c_std",
    "temp_c_min",
    "temp_c_max",
    "temp_c_range",
    "humid_pct_mean",
    "humid_pct_std",
    "humid_pct_min",
    "humid_pct_max",
    "humid_pct_range",
    "ad3_pcb_temp_c_mean",
    "ad3_pcb_temp_c_std",
    "ad3_pcb_temp_c_range",
    "signal_freq_ok_frac",
    "row_status_counts",
]


@dataclass
class StabilityConfig:
    run_id: str
    phase_jump_deg: float
    magnitude_jump_frac: float
    magnitude_jump_abs: float
    stable_min_samples: int
    stable_window: int
    stable_phase_std_deg: float
    stable_magnitude_rel_std: float
    require_signal_freq_ok: bool


class StableBlockTracker:
    def __init__(self, config: StabilityConfig) -> None:
        self.config = config
        self.block_id = -1
        self.lock_state = ""
        self.start_reason = ""
        self.samples: list[dict[str, object]] = []
        self.valid_phases: list[float] = []
        self.valid_magnitudes: list[float] = []
        self.last_phase_raw: float | None = None
        self.last_phase_unwrapped: float | None = None
        self.consecutive_stable = 0

    def _start_block(self, reason: str) -> None:
        self.block_id += 1
        self.lock_state = f"L{self.block_id:03d}"
        self.start_reason = reason
        self.samples = []
        self.valid_phases = []
        self.valid_magnitudes = []
        self.last_phase_raw = None
        self.last_phase_unwrapped = None
        self.consecutive_stable = 0

    def _center_phase(self) -> float:
        if not self.valid_phases:
            return float("nan")
        return float(np.median(np.array(self.valid_phases, dtype=float)))

    def _center_magnitude(self) -> float:
        if not self.valid_magnitudes:
            return float("nan")
        return float(np.median(np.array(self.valid_magnitudes, dtype=float)))

    def _candidate_phase(self, phase_deg: float) -> float:
        if self.last_phase_raw is None or self.last_phase_unwrapped is None:
            return float(phase_deg)
        return self.last_phase_unwrapped + phase_diff_deg(float(phase_deg), self.last_phase_raw)

    def _jump_reason(self, phase_unwrapped_deg: float, magnitude: float) -> str:
        if len(self.valid_phases) < max(2, min(self.config.stable_min_samples, 4)):
            return ""

        phase_center = self._center_phase()
        magnitude_center = self._center_magnitude()
        reasons = []
        if finite_float(phase_center):
            phase_delta = float(phase_unwrapped_deg) - phase_center
            if abs(phase_delta) > self.config.phase_jump_deg:
                reasons.append(f"phase_jump:{phase_delta:+.3f}deg")
        if finite_float(magnitude_center) and abs(magnitude_center) > 1e-12:
            magnitude_delta = float(magnitude) - magnitude_center
            magnitude_delta_frac = abs(magnitude_delta) / abs(magnitude_center)
            if (
                abs(magnitude_delta) > self.config.magnitude_jump_abs
                and magnitude_delta_frac > self.config.magnitude_jump_frac
            ):
                reasons.append(f"magnitude_jump:{magnitude_delta:+.6g}")
        return "|".join(reasons)

    def _rolling_stability(self) -> tuple[bool, float, float]:
        n = min(self.config.stable_window, len(self.valid_phases), len(self.valid_magnitudes))
        if n < self.config.stable_min_samples:
            return False, float("nan"), float("nan")
        phase_tail = np.array(self.valid_phases[-n:], dtype=float)
        magnitude_tail = np.array(self.valid_magnitudes[-n:], dtype=float)
        phase_std = float(np.std(phase_tail, ddof=1)) if n > 1 else 0.0
        mag_mean = float(np.mean(magnitude_tail))
        mag_std = float(np.std(magnitude_tail, ddof=1)) if n > 1 else 0.0
        mag_rel_std = mag_std / abs(mag_mean) if abs(mag_mean) > 1e-12 else float("inf")
        stable = (
            phase_std <= self.config.stable_phase_std_deg
            and mag_rel_std <= self.config.stable_magnitude_rel_std
        )
        return stable, phase_std, mag_rel_std

    def _summarize_current(self, end_reason: str) -> dict[str, object] | None:
        if not self.samples:
            return None
        first = self.samples[0]
        last = self.samples[-1]
        status_counts = Counter(str(s.get("row_status", "")) for s in self.samples)
        valid_count = sum(1 for s in self.samples if int(s.get("valid_signal", 0) or 0) == 1)
        stable_count = sum(1 for s in self.samples if int(s.get("is_stable", 0) or 0) == 1)
        accepted = stable_count >= self.config.stable_min_samples
        summary_samples = [
            s for s in self.samples if int(s.get("is_stable", 0) or 0) == 1
        ] if accepted else self.samples
        summary_first = summary_samples[0]
        summary_last = summary_samples[-1]
        summary_basis = "stable" if accepted else "all"
        freq_known = [s for s in self.samples if s.get("signal_freq_ok") != ""]
        freq_ok_count = sum(1 for s in freq_known if int(s.get("signal_freq_ok", 0) or 0) == 1)
        start_time_s = float(first["time_s"]) if finite_float(first.get("time_s")) else float("nan")
        end_time_s = float(last["time_s"]) if finite_float(last.get("time_s")) else float("nan")
        phase_s = stats([float(s["phase_unwrapped_deg"]) for s in summary_samples if finite_float(s.get("phase_unwrapped_deg"))])
        magnitude_s = stats([float(s["magnitude"]) for s in summary_samples if finite_float(s.get("magnitude"))])
        raw_x_s = stats([float(s["raw_x"]) for s in summary_samples if finite_float(s.get("raw_x"))])
        raw_y_s = stats([float(s["raw_y"]) for s in summary_samples if finite_float(s.get("raw_y"))])
        width_s = stats([float(s["width_mm"]) for s in summary_samples if finite_float(s.get("width_mm"))])
        thickness_s = stats([float(s["thickness_mm"]) for s in summary_samples if finite_float(s.get("thickness_mm"))])
        temp_s = stats([float(s["temp_c"]) for s in summary_samples if finite_float(s.get("temp_c"))])
        humid_s = stats([float(s["humid_pct"]) for s in summary_samples if finite_float(s.get("humid_pct"))])
        pcb_s = stats([float(s["ad3_pcb_temp_c"]) for s in summary_samples if finite_float(s.get("ad3_pcb_temp_c"))])
        return {
            "run_id": self.config.run_id,
            "block_id": self.block_id,
            "lock_state": self.lock_state,
            "start_reason": self.start_reason,
            "end_reason": end_reason,
            "start_number": first.get("number", ""),
            "end_number": last.get("number", ""),
            "start_timestamp": first.get("timestamp", ""),
            "end_timestamp": last.get("timestamp", ""),
            "start_time_s": start_time_s,
            "end_time_s": end_time_s,
            "duration_s": end_time_s - start_time_s if finite_float(start_time_s) and finite_float(end_time_s) else float("nan"),
            "sample_count": len(self.samples),
            "valid_signal_count": valid_count,
            "stable_sample_count": stable_count,
            "accepted_for_env_fit": 1 if accepted else 0,
            "summary_basis": summary_basis,
            "summary_sample_count": len(summary_samples),
            "summary_start_number": summary_first.get("number", ""),
            "summary_end_number": summary_last.get("number", ""),
            "phase_mean_deg": phase_s["mean"],
            "phase_std_deg": phase_s["std"],
            "phase_min_deg": phase_s["min"],
            "phase_max_deg": phase_s["max"],
            "phase_range_deg": phase_s["range"],
            "magnitude_mean": magnitude_s["mean"],
            "magnitude_std": magnitude_s["std"],
            "magnitude_range": magnitude_s["range"],
            "raw_x_mean": raw_x_s["mean"],
            "raw_x_std": raw_x_s["std"],
            "raw_y_mean": raw_y_s["mean"],
            "raw_y_std": raw_y_s["std"],
            "width_mm_mean": width_s["mean"],
            "width_mm_std": width_s["std"],
            "thickness_mm_mean": thickness_s["mean"],
            "thickness_mm_std": thickness_s["std"],
            "temp_c_mean": temp_s["mean"],
            "temp_c_std": temp_s["std"],
            "temp_c_min": temp_s["min"],
            "temp_c_max": temp_s["max"],
            "temp_c_range": temp_s["range"],
            "humid_pct_mean": humid_s["mean"],
            "humid_pct_std": humid_s["std"],
            "humid_pct_min": humid_s["min"],
            "humid_pct_max": humid_s["max"],
            "humid_pct_range": humid_s["range"],
            "ad3_pcb_temp_c_mean": pcb_s["mean"],
            "ad3_pcb_temp_c_std": pcb_s["std"],
            "ad3_pcb_temp_c_range": pcb_s["range"],
            "signal_freq_ok_frac": freq_ok_count / len(freq_known) if freq_known else float("nan"),
            "row_status_counts": ";".join(f"{k}:{v}" for k, v in sorted(status_counts.items())),
        }

    def update(self, row: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
        finalized: list[dict[str, object]] = []
        if self.block_id < 0:
            self._start_block("initial")

        signal_freq_ok = int(row.get("signal_freq_ok", 0) or 0) == 1
        valid_signal = (
            row.get("row_status") == "ok"
            and finite_float(row.get("phase_deg"))
            and finite_float(row.get("magnitude"))
            and (signal_freq_ok or not self.config.require_signal_freq_ok)
        )

        phase_unwrapped = float("nan")
        jump_reason = ""
        if valid_signal:
            phase_raw = float(row["phase_deg"])
            magnitude = float(row["magnitude"])
            phase_unwrapped = self._candidate_phase(phase_raw)
            jump_reason = self._jump_reason(phase_unwrapped, magnitude)
            if jump_reason:
                summary = self._summarize_current(jump_reason)
                if summary is not None:
                    finalized.append(summary)
                self._start_block(jump_reason)
                phase_unwrapped = float(phase_raw)

        block_sample_index = len(self.samples)
        if valid_signal:
            self.valid_phases.append(phase_unwrapped)
            self.valid_magnitudes.append(float(row["magnitude"]))
            self.last_phase_raw = float(row["phase_deg"])
            self.last_phase_unwrapped = phase_unwrapped

        stable, rolling_phase_std, rolling_mag_rel_std = self._rolling_stability()
        is_stable = bool(valid_signal and stable)
        self.consecutive_stable = self.consecutive_stable + 1 if is_stable else 0

        phase_center = self._center_phase()
        magnitude_center = self._center_magnitude()
        phase_delta = phase_unwrapped - phase_center if finite_float(phase_unwrapped) and finite_float(phase_center) else float("nan")
        magnitude_delta = (
            float(row["magnitude"]) - magnitude_center
            if finite_float(row.get("magnitude")) and finite_float(magnitude_center)
            else float("nan")
        )
        meta = {
            "run_id": self.config.run_id,
            "block_id": self.block_id,
            "lock_state": self.lock_state,
            "block_sample_index": block_sample_index,
            "valid_signal": 1 if valid_signal else 0,
            "is_stable": 1 if is_stable else 0,
            "stable_count": self.consecutive_stable,
            "phase_unwrapped_deg": phase_unwrapped,
            "phase_center_deg": phase_center,
            "phase_delta_deg": phase_delta,
            "magnitude_center": magnitude_center,
            "magnitude_delta": magnitude_delta,
            "rolling_phase_std_deg": rolling_phase_std,
            "rolling_magnitude_rel_std": rolling_mag_rel_std,
            "lock_event": jump_reason,
        }
        sample = {**row, **meta}
        self.samples.append(sample)
        return meta, finalized

    def finalize(self, reason: str) -> dict[str, object] | None:
        summary = self._summarize_current(reason)
        self.samples = []
        return summary


class TriggeredAD3ECTReader:
    def __init__(
        self,
        mode: str,
        output_channel: int,
        input_channel: int,
        reference_channel: int | None,
        reference_output_channel: int | None,
        drive_freq_hz: float,
        drive_amplitude_v: float,
        sample_rate_hz: float,
        buffer_size: int,
        analog_range_v: float,
        reference_range_v: float | None,
        averages: int,
        timeout_s: float,
        discard_s: float,
        lockin_window_cycles: int,
        lockin_window_mad_threshold: float,
        lockin_min_windows: int,
        lockin_window_filter: bool,
        max_lockin_window_phase_std_deg: float,
    ) -> None:
        if mode == "ratio":
            if AD3ContinuousRatioLockIn is None:
                raise RuntimeError("ad3_ratiometric_lockin.py is unavailable")
            if reference_channel is None:
                raise RuntimeError("ratio mode requires --reference-channel")
            self.driver = AD3ContinuousRatioLockIn(
                output_channel=output_channel,
                input_channel=input_channel,
                reference_channel=reference_channel,
                reference_output_channel=reference_output_channel,
                drive_freq_hz=drive_freq_hz,
                drive_amplitude_v=drive_amplitude_v,
                sample_rate_hz=sample_rate_hz,
                buffer_size=buffer_size,
                signal_range_v=analog_range_v,
                reference_range_v=reference_range_v,
                averages=averages,
                timeout_s=timeout_s,
                lockin_window_cycles=lockin_window_cycles,
                lockin_window_mad_threshold=lockin_window_mad_threshold,
                lockin_min_windows=lockin_min_windows,
                lockin_window_filter=lockin_window_filter,
                max_lockin_window_phase_std_deg=max_lockin_window_phase_std_deg,
            )
            return
        if AD3TriggeredLockIn is None:
            raise RuntimeError("ad3_triggered_lockin.py is unavailable")
        self.driver = AD3TriggeredLockIn(
            output_channel=output_channel,
            input_channel=input_channel,
            reference_channel=reference_channel,
            reference_output_channel=reference_output_channel,
            drive_freq_hz=drive_freq_hz,
            drive_amplitude_v=drive_amplitude_v,
            sample_rate_hz=sample_rate_hz,
            buffer_size=buffer_size,
            analog_range_v=analog_range_v,
            averages=averages,
            timeout_s=timeout_s,
            discard_s=discard_s,
            lockin_window_cycles=lockin_window_cycles,
            lockin_window_mad_threshold=lockin_window_mad_threshold,
            lockin_min_windows=lockin_min_windows,
            lockin_window_filter=lockin_window_filter,
            max_lockin_window_phase_std_deg=max_lockin_window_phase_std_deg,
        )

    def open(self) -> None:
        self.driver.open()
        print(f"AD3 readback: {self.driver.readback()}")

    def read(self) -> dict[str, float]:
        measurement, _waveform, _freqs, _amp = self.driver.measure()
        return {
            "raw_x": measurement.raw_x,
            "raw_y": measurement.raw_y,
            "magnitude": measurement.magnitude,
            "phase_deg": measurement.phase_deg,
            "signal_raw_x": measurement.signal_raw_x,
            "signal_raw_y": measurement.signal_raw_y,
            "signal_magnitude": measurement.signal_magnitude,
            "signal_phase_deg": measurement.signal_phase_deg,
            "reference_raw_x": measurement.reference_raw_x,
            "reference_raw_y": measurement.reference_raw_y,
            "reference_magnitude": measurement.reference_magnitude,
            "reference_phase_deg": measurement.reference_phase_deg,
            "vpp": measurement.vpp,
            "mean_v": measurement.mean_v,
            "std_v": measurement.std_v,
            "drive_bin_amp": measurement.drive_bin_amp,
            "drive_bin_phase_deg": measurement.drive_bin_phase_deg,
            "peak_freq_hz": measurement.peak_freq_hz,
            "peak_amp": measurement.peak_amp,
            "snr_drive_to_peak": measurement.snr_drive_to_peak,
            "sample_rate_hz": measurement.sample_rate_hz,
            "buffer_size": measurement.buffer_size,
            "lockin_window_count": measurement.lockin_window_count,
            "lockin_window_kept": measurement.lockin_window_kept,
            "lockin_window_rejected": measurement.lockin_window_rejected,
            "lockin_window_phase_std_deg": measurement.lockin_window_phase_std_deg,
            "lockin_window_magnitude_rel_std": measurement.lockin_window_magnitude_rel_std,
            "signal_freq_hz": measurement.peak_freq_hz,
        }

    def read_board_status(self) -> dict[str, float]:
        return self.driver.read_board_status()

    def recover(self, reopen: bool, delay_s: float) -> None:
        self.driver.recover(reopen=reopen, delay_s=delay_s)

    def close(self) -> None:
        self.driver.close()


def open_laser(args: argparse.Namespace) -> Optional[object]:
    if args.no_laser:
        return None
    if KeyenceLaser is None:
        print("[warn] KeyenceLaser driver is unavailable; geometry columns will be blank.")
        return None
    try:
        laser = KeyenceLaser(host=args.laser_host, port=args.laser_port, timeout=args.laser_timeout)
        if hasattr(laser, "ensure_thickness_mode"):
            laser.ensure_thickness_mode()
        else:
            laser.set_mode(1)
            time.sleep(args.laser_settle_s)
        return laser
    except Exception as exc:
        print(f"[warn] Keyence laser unavailable: {exc}; geometry columns will be blank.")
        return None


def open_atmosphere(args: argparse.Namespace) -> Optional[object]:
    if args.no_atmosphere:
        return None
    if RN171WCSensor is None:
        print("[warn] RN171WC driver is unavailable; ambient columns will be blank.")
        return None
    try:
        sensor = RN171WCSensor(host=args.atmo_host, port=args.atmo_port)
        sensor.connect()
        return sensor
    except Exception as exc:
        print(f"[warn] RN171WC unavailable: {exc}; ambient columns will be blank.")
        return None


def read_laser(laser: Optional[object]) -> tuple[float, float]:
    if laser is None:
        return float("nan"), float("nan")
    try:
        if hasattr(laser, "ensure_thickness_mode"):
            laser.ensure_thickness_mode()
        thickness_mm, width_mm = laser.read_measurement()
        return float(thickness_mm), float(width_mm)
    except Exception as exc:
        print(f"\n[warn] Keyence read failed: {exc}")
        return float("nan"), float("nan")


def read_atmosphere(sensor: Optional[object]) -> tuple[float, float]:
    if sensor is None:
        return float("nan"), float("nan")
    try:
        temp_c, humid_pct = sensor.read_data()
        return float(temp_c), float(humid_pct)
    except Exception as exc:
        print(f"\n[warn] RN171WC read failed: {exc}")
        return float("nan"), float("nan")


ECT_FIELD_DEFAULTS = {
    "raw_x": float("nan"),
    "raw_y": float("nan"),
    "magnitude": float("nan"),
    "phase_deg": float("nan"),
    "signal_raw_x": float("nan"),
    "signal_raw_y": float("nan"),
    "signal_magnitude": float("nan"),
    "signal_phase_deg": float("nan"),
    "reference_raw_x": float("nan"),
    "reference_raw_y": float("nan"),
    "reference_magnitude": float("nan"),
    "reference_phase_deg": float("nan"),
    "vpp": float("nan"),
    "mean_v": float("nan"),
    "std_v": float("nan"),
    "drive_bin_amp": float("nan"),
    "drive_bin_phase_deg": float("nan"),
    "peak_freq_hz": float("nan"),
    "peak_amp": float("nan"),
    "snr_drive_to_peak": float("nan"),
    "sample_rate_hz": float("nan"),
    "buffer_size": float("nan"),
    "lockin_window_count": float("nan"),
    "lockin_window_kept": float("nan"),
    "lockin_window_rejected": float("nan"),
    "lockin_window_phase_std_deg": float("nan"),
    "lockin_window_magnitude_rel_std": float("nan"),
    "signal_freq_hz": float("nan"),
}


def read_ad3_with_retries(
    ad3: TriggeredAD3ECTReader,
    retries: int,
    retry_delay_s: float,
    recover_delay_s: float,
    hard_reopen_after: int,
) -> tuple[dict[str, float], str, str, int, int]:
    attempts = max(0, retries) + 1
    last_exc: BaseException | None = None
    recovery_count = 0
    for attempt in range(attempts):
        try:
            return ad3.read(), "ok", "", attempt, recovery_count
        except (TimeoutError, RuntimeError) as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                print(f"\n[warn] AD3 read failed ({exc}); retry {attempt + 1}/{retries}.")
                try:
                    reopen = hard_reopen_after > 0 and (attempt + 1) >= hard_reopen_after
                    ad3.recover(reopen=reopen, delay_s=recover_delay_s)
                    recovery_count += 1
                    print(f"[warn] AD3 recovery applied: {'hard reopen' if reopen else 'soft reset'}.")
                except Exception as recover_exc:
                    print(f"[warn] AD3 recovery failed: {recover_exc}")
                time.sleep(max(0.0, retry_delay_s))
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                print(f"\n[warn] AD3 read raised {type(exc).__name__}: {exc}; retry {attempt + 1}/{retries}.")
                try:
                    reopen = hard_reopen_after > 0 and (attempt + 1) >= hard_reopen_after
                    ad3.recover(reopen=reopen, delay_s=recover_delay_s)
                    recovery_count += 1
                    print(f"[warn] AD3 recovery applied: {'hard reopen' if reopen else 'soft reset'}.")
                except Exception as recover_exc:
                    print(f"[warn] AD3 recovery failed: {recover_exc}")
                time.sleep(max(0.0, retry_delay_s))

    assert last_exc is not None
    status = "ad3_timeout" if isinstance(last_exc, TimeoutError) else "ad3_error"
    print(f"\n[warn] AD3 read failed after {attempts} attempt(s): {last_exc}; row will keep AD3 fields blank.")
    try:
        ad3.recover(reopen=True, delay_s=recover_delay_s)
        recovery_count += 1
        print("[warn] AD3 hard reopen applied for next row.")
    except Exception as recover_exc:
        print(f"[warn] AD3 hard reopen failed: {recover_exc}")
    return dict(ECT_FIELD_DEFAULTS), status, f"{type(last_exc).__name__}: {last_exc}", attempts - 1, recovery_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", default=os.environ.get("ECTV2_LOG_DIR", str(SCRIPT_DIR / "logs")))
    parser.add_argument("--run-id", default=os.environ.get("ECTV2_RUN_ID"))
    parser.add_argument("--summary-path", default=None, help="Stable-block summary CSV path. Defaults to *_blocks.csv next to the raw log.")
    parser.add_argument("--meta-path", default=None, help="Run metadata JSON path. Defaults to *_meta.json next to the raw log.")
    parser.add_argument("--no-summary", action="store_true")
    parser.add_argument("--interval-s", type=float, default=float(os.environ.get("ECTV2_INTERVAL_S", "10.0")))
    parser.add_argument("--duration-s", type=float, default=0.0, help="Stop after this many seconds. 0 means run until Ctrl+C.")
    parser.add_argument(
        "--mode",
        choices=tuple(MODE_DEFAULTS),
        default=os.environ.get("ECTV2_MODE", "ratio"),
        help=(
            "Measurement mode. 'ratio' (default) drives continuously and logs the "
            "complex pickup/drive ratio; 'triggered' is the legacy AWG-restart path. "
            "Unset channel/frequency/buffer options take per-mode defaults."
        ),
    )
    parser.add_argument("--output-channel", type=int, default=None, choices=(0, 1))
    parser.add_argument("--input-channel", type=int, default=None, choices=(0, 1))
    parser.add_argument(
        "--reference-output-channel",
        type=int,
        choices=(0, 1),
        default=None,
        help="Optional W1/W2 output channel to drive as the physical reference signal.",
    )
    parser.add_argument(
        "--reference-channel",
        type=int,
        choices=(0, 1),
        default=None,
        help=(
            "Optional analog-in channel carrying the drive/reference signal. "
            "When set, raw_x/raw_y are complex lock-in(input)/lock-in(reference)."
        ),
    )
    parser.add_argument("--drive-freq-hz", type=float, default=None)
    parser.add_argument("--drive-amplitude-v", type=float, default=None)
    parser.add_argument("--sample-rate-hz", type=float, default=1_000_000.0)
    parser.add_argument("--buffer-size", type=int, default=None)
    parser.add_argument("--analog-range-v", type=float, default=5.0)
    parser.add_argument(
        "--reference-range-v",
        type=float,
        default=None,
        help="Ratio mode only: reference channel input range. Default picks the high range when the drive amplitude would clip the low range.",
    )
    parser.add_argument("--averages", type=int, default=None)
    parser.add_argument("--discard-s", type=float, default=0.001)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--lockin-window-cycles", type=int, default=15)
    parser.add_argument("--lockin-window-mad-threshold", type=float, default=6.0)
    parser.add_argument("--lockin-min-windows", type=int, default=4)
    parser.add_argument("--max-lockin-window-phase-std-deg", type=float, default=5.0)
    parser.add_argument("--no-lockin-window-filter", action="store_true")
    parser.add_argument("--ad3-retries", type=int, default=2)
    parser.add_argument("--ad3-retry-delay-s", type=float, default=0.1)
    parser.add_argument("--ad3-recover-delay-s", type=float, default=0.25)
    parser.add_argument("--ad3-hard-reopen-after", type=int, default=2)
    parser.add_argument("--signal-freq-tolerance-hz", type=float, default=50.0)
    parser.add_argument("--allow-off-peak-stable", action="store_true", help="Allow rows whose dominant spectral peak is not at the drive frequency to be marked stable.")
    parser.add_argument("--phase-jump-deg", type=float, default=4.0)
    parser.add_argument("--magnitude-jump-frac", type=float, default=0.06)
    parser.add_argument("--magnitude-jump-abs", type=float, default=0.01)
    parser.add_argument("--stable-min-samples", type=int, default=6)
    parser.add_argument("--stable-window", type=int, default=12)
    parser.add_argument("--stable-phase-std-deg", type=float, default=1.0)
    parser.add_argument("--stable-magnitude-rel-std", type=float, default=0.03)
    parser.add_argument("--fsync-every", type=int, default=20)
    parser.add_argument("--laser-host", default=os.environ.get("KEYENCE_HOST", "192.168.0.111"))
    parser.add_argument("--laser-port", type=int, default=int(os.environ.get("KEYENCE_PORT", "64000")))
    parser.add_argument("--laser-timeout", type=float, default=2.0)
    parser.add_argument("--laser-settle-s", type=float, default=0.08)
    parser.add_argument("--atmo-host", default=os.environ.get("RN171_HOST", "192.168.0.34"))
    parser.add_argument("--atmo-port", type=int, default=int(os.environ.get("RN171_PORT", "502")))
    parser.add_argument("--pe-config", default=os.environ.get("ECTV2_PE_CONFIG", str(SCRIPT_DIR / "config.yaml")))
    parser.add_argument("--plot-path", default=None)
    parser.add_argument("--plot-every", type=int, default=1)
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--no-pe", action="store_true")
    parser.add_argument("--no-laser", action="store_true")
    parser.add_argument("--no-atmosphere", action="store_true")
    return parser


def update_impedance_plot(raw_x: list[float], raw_y: list[float], plot_path: Path) -> None:
    if not raw_x or not raw_y:
        return
    x = np.array(raw_x, dtype=float)
    y = np.array(raw_y, dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    if not np.any(finite):
        return
    x_plot = x[finite]
    y_plot = y[finite]
    fig, ax = plt.subplots(figsize=(6.5, 6.0), dpi=140)
    idx = np.flatnonzero(finite)
    sc = ax.scatter(x_plot, y_plot, c=idx, s=16, cmap="viridis", alpha=0.85, edgecolors="none")
    ax.scatter([x_plot[0]], [y_plot[0]], s=52, marker="o", color="#2ca02c", label="start", zorder=3)
    ax.scatter([x_plot[-1]], [y_plot[-1]], s=52, marker="x", color="#d62728", label="latest", zorder=3)
    ax.plot(x_plot, y_plot, color="#777777", linewidth=0.7, alpha=0.35)
    ax.axhline(0.0, color="#bbbbbb", linewidth=0.7)
    ax.axvline(0.0, color="#bbbbbb", linewidth=0.7)
    x_min, x_max = float(np.min(x_plot)), float(np.max(x_plot))
    y_min, y_max = float(np.min(y_plot)), float(np.max(y_plot))
    x_span = x_max - x_min
    y_span = y_max - y_min
    fallback_span = max(abs(x_min), abs(x_max), abs(y_min), abs(y_max), 1.0) * 1e-3
    if x_span <= 0:
        x_span = fallback_span
        x_min -= x_span / 2.0
        x_max += x_span / 2.0
    if y_span <= 0:
        y_span = fallback_span
        y_min -= y_span / 2.0
        y_max += y_span / 2.0
    x_margin = max(x_span * 0.12, fallback_span * 0.5)
    y_margin = max(y_span * 0.12, fallback_span * 0.5)
    ax.set_xlim(x_min - x_margin, x_max + x_margin)
    ax.set_ylim(y_min - y_margin, y_max + y_margin)
    ax.set_xlabel("raw_x")
    ax.set_ylabel("raw_y")
    ax.set_title("AD3 Impedance Plane")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    cbar = fig.colorbar(sc, ax=ax)
    cbar.set_label("sample index")
    fig.tight_layout()
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(plot_path)
    plt.close(fig)


def main() -> int:
    args = build_parser().parse_args()
    mode_defaults = MODE_DEFAULTS[args.mode]
    for name, default in mode_defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, default)
    if args.mode == "ratio" and args.reference_channel is None:
        raise SystemExit("ratio mode requires --reference-channel")
    if args.reference_channel is not None and args.reference_channel == args.input_channel:
        raise SystemExit("--reference-channel must differ from --input-channel")
    if (
        args.reference_output_channel is not None
        and args.reference_output_channel == args.output_channel
    ):
        raise SystemExit("--reference-output-channel must differ from --output-channel")
    log_dir = Path(args.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or datetime.now().strftime("adhoc_%Y%m%d_%H%M%S")
    log_path = log_dir / f"ad3_log_{run_id}.csv"
    summary_path = (
        Path(args.summary_path).expanduser().resolve()
        if args.summary_path
        else log_path.with_name(f"{log_path.stem}_blocks.csv")
    )
    meta_path = (
        Path(args.meta_path).expanduser().resolve()
        if args.meta_path
        else log_path.with_name(f"{log_path.stem}_meta.json")
    )
    plot_path = (
        Path(args.plot_path).expanduser().resolve()
        if args.plot_path
        else log_path.with_suffix(".png")
    )

    fieldnames = [
        "number",
        "timestamp",
        "time_s",
        "width_mm",
        "thickness_mm",
        "raw_x",
        "raw_y",
        "magnitude",
        "phase_deg",
        "signal_raw_x",
        "signal_raw_y",
        "signal_magnitude",
        "signal_phase_deg",
        "reference_raw_x",
        "reference_raw_y",
        "reference_magnitude",
        "reference_phase_deg",
        "vpp",
        "signal_freq_hz",
        "plastic_strain",
        "temp_c",
        "humid_pct",
        "ad3_pcb_temp_c",
        "ad3_fpga_temp_c",
        "ad3_usb_v",
        "ad3_usb_a",
        "mean_v",
        "std_v",
        "drive_bin_amp",
        "drive_bin_phase_deg",
        "peak_freq_hz",
        "peak_amp",
        "snr_drive_to_peak",
        "sample_rate_hz",
        "buffer_size",
        "lockin_window_count",
        "lockin_window_kept",
        "lockin_window_rejected",
        "lockin_window_phase_std_deg",
        "lockin_window_magnitude_rel_std",
        "row_status",
        "row_error",
        "ad3_retry_count",
        "ad3_recovery_count",
        "run_id",
        "block_id",
        "lock_state",
        "block_sample_index",
        "valid_signal",
        "is_stable",
        "stable_count",
        "phase_unwrapped_deg",
        "phase_center_deg",
        "phase_delta_deg",
        "magnitude_center",
        "magnitude_delta",
        "rolling_phase_std_deg",
        "rolling_magnitude_rel_std",
        "lock_event",
        "signal_freq_ok",
        "drive_freq_error_hz",
        "loop_elapsed_s",
        "interval_lag_s",
    ]

    ad3 = TriggeredAD3ECTReader(
        mode=args.mode,
        output_channel=args.output_channel,
        input_channel=args.input_channel,
        reference_channel=args.reference_channel,
        reference_output_channel=args.reference_output_channel,
        drive_freq_hz=args.drive_freq_hz,
        drive_amplitude_v=args.drive_amplitude_v,
        sample_rate_hz=args.sample_rate_hz,
        buffer_size=args.buffer_size,
        analog_range_v=args.analog_range_v,
        reference_range_v=args.reference_range_v,
        averages=args.averages,
        timeout_s=args.timeout_s,
        discard_s=args.discard_s,
        lockin_window_cycles=args.lockin_window_cycles,
        lockin_window_mad_threshold=args.lockin_window_mad_threshold,
        lockin_min_windows=args.lockin_min_windows,
        lockin_window_filter=not args.no_lockin_window_filter,
        max_lockin_window_phase_std_deg=args.max_lockin_window_phase_std_deg,
    )
    laser = None
    atmo = None
    pe_calibration = None
    tracker = StableBlockTracker(
        StabilityConfig(
            run_id=run_id,
            phase_jump_deg=args.phase_jump_deg,
            magnitude_jump_frac=args.magnitude_jump_frac,
            magnitude_jump_abs=args.magnitude_jump_abs,
            stable_min_samples=max(2, args.stable_min_samples),
            stable_window=max(2, args.stable_window),
            stable_phase_std_deg=args.stable_phase_std_deg,
            stable_magnitude_rel_std=args.stable_magnitude_rel_std,
            require_signal_freq_ok=not args.allow_off_peak_stable,
        )
    )
    metadata = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
        "python": sys.version,
        "platform": platform.platform(),
        "args": vars(args),
        "paths": {
            "raw_csv": str(log_path),
            "block_summary_csv": None if args.no_summary else str(summary_path),
            "metadata_json": str(meta_path),
            "plot": None if args.no_plot else str(plot_path),
        },
        "git": current_git_state(SCRIPT_DIR),
        "status": "running",
    }
    write_json(meta_path, metadata)
    stop_reason = "completed"
    rows_written = 0

    try:
        print("Opening Analog Discovery 3...")
        ad3.open()
        laser = open_laser(args)
        atmo = open_atmosphere(args)
        if not args.no_pe and load_pe_config is not None:
            pe_config_path = Path(args.pe_config).expanduser().resolve()
            if pe_config_path.exists():
                pe_calibration = load_pe_config(pe_config_path)
                print(f"Loaded PE calibration: {pe_config_path}")
            else:
                print(f"[warn] PE config not found: {pe_config_path}; plastic_strain will be blank.")

        summary_f = None
        summary_writer = None
        if not args.no_summary:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_f = summary_path.open("w", newline="")
            summary_writer = csv.DictWriter(summary_f, fieldnames=BLOCK_SUMMARY_FIELDNAMES)
            summary_writer.writeheader()

        with log_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            print(f"Logging to: {log_path}")
            if not args.no_summary:
                print(f"Block summary: {summary_path}")
            print(f"Run metadata: {meta_path}")
            if not args.no_plot:
                print(f"Impedance plot: {plot_path}")
            print("Press Ctrl+C to stop.")
            print("number,time_s,block,stable,status,width,thickness,raw_x,raw_y,phase,temp,humid,ad3_pcb_temp,plastic_strain")
            started = time.monotonic()
            number = 0
            plot_raw_x = []
            plot_raw_y = []

            while True:
                loop_started = time.monotonic()
                now = datetime.now()
                time_s = loop_started - started
                if args.duration_s > 0 and time_s >= args.duration_s:
                    break

                thickness_mm, width_mm = read_laser(laser)
                temp_c, humid_pct = read_atmosphere(atmo)
                ect, row_status, row_error, ad3_retry_count, ad3_recovery_count = read_ad3_with_retries(
                    ad3,
                    retries=args.ad3_retries,
                    retry_delay_s=args.ad3_retry_delay_s,
                    recover_delay_s=args.ad3_recover_delay_s,
                    hard_reopen_after=args.ad3_hard_reopen_after,
                )
                board = ad3.read_board_status()
                if pe_calibration is not None and compute_pe is not None:
                    plastic_strain = compute_pe(
                        ect["raw_x"],
                        ect["raw_y"],
                        temp_c,
                        thickness_mm,
                        width_mm,
                        humid_pct,
                        pe_calibration,
                    )
                else:
                    plastic_strain = float("nan")

                drive_freq_error_hz = (
                    float(ect["signal_freq_hz"]) - args.drive_freq_hz
                    if finite_float(ect.get("signal_freq_hz"))
                    else float("nan")
                )
                signal_freq_ok = (
                    1
                    if finite_float(drive_freq_error_hz)
                    and abs(drive_freq_error_hz) <= args.signal_freq_tolerance_hz
                    else 0
                )
                loop_elapsed_s = time.monotonic() - loop_started
                interval_lag_s = max(0.0, loop_elapsed_s - args.interval_s)
                row = {
                    "number": number,
                    "timestamp": now.isoformat(timespec="milliseconds"),
                    "time_s": time_s,
                    "width_mm": width_mm,
                    "thickness_mm": thickness_mm,
                    "temp_c": temp_c,
                    "humid_pct": humid_pct,
                    "plastic_strain": plastic_strain,
                    "row_status": row_status,
                    "row_error": row_error,
                    "ad3_retry_count": ad3_retry_count,
                    "ad3_recovery_count": ad3_recovery_count,
                    "signal_freq_ok": signal_freq_ok,
                    "drive_freq_error_hz": drive_freq_error_hz,
                    "loop_elapsed_s": loop_elapsed_s,
                    "interval_lag_s": interval_lag_s,
                    **ect,
                    **board,
                }
                block_meta, finalized_blocks = tracker.update(row)
                row.update(block_meta)
                writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})
                f.flush()
                if args.fsync_every > 0 and number > 0 and number % args.fsync_every == 0:
                    os.fsync(f.fileno())
                if summary_writer is not None and summary_f is not None:
                    for block in finalized_blocks:
                        summary_writer.writerow({key: csv_value(block.get(key)) for key in BLOCK_SUMMARY_FIELDNAMES})
                    if finalized_blocks:
                        summary_f.flush()
                        if args.fsync_every > 0:
                            os.fsync(summary_f.fileno())
                if not args.no_plot:
                    plot_raw_x.append(ect["raw_x"])
                    plot_raw_y.append(ect["raw_y"])
                    if args.plot_every > 0 and number % args.plot_every == 0:
                        update_impedance_plot(plot_raw_x, plot_raw_y, plot_path)

                print(
                    f"{number:06d},{time_s:9.1f},"
                    f"{row['lock_state']:>5},{row['is_stable']!s:>1},{row_status:>10},"
                    f"{csv_value(width_mm):>9},{csv_value(thickness_mm):>9},"
                    f"{csv_value(ect['raw_x']):>12},{csv_value(ect['raw_y']):>12},"
                    f"{csv_value(ect['phase_deg']):>9},"
                    f"{csv_value(temp_c):>7},{csv_value(humid_pct):>7},"
                    f"{csv_value(board['ad3_pcb_temp_c']):>7},"
                    f"{csv_value(plastic_strain):>10}",
                    end="\r",
                )

                number += 1
                rows_written = number
                sleep_s = args.interval_s - (time.monotonic() - loop_started)
                if sleep_s > 0:
                    time.sleep(sleep_s)

    except KeyboardInterrupt:
        print("\nStopping...")
        stop_reason = "keyboard_interrupt"
    except Exception as exc:
        stop_reason = f"error:{type(exc).__name__}"
        raise
    finally:
        if "summary_writer" in locals() and summary_writer is not None and "summary_f" in locals() and summary_f is not None:
            final_block = tracker.finalize(stop_reason)
            if final_block is not None:
                summary_writer.writerow({key: csv_value(final_block.get(key)) for key in BLOCK_SUMMARY_FIELDNAMES})
            summary_f.flush()
            os.fsync(summary_f.fileno())
            summary_f.close()
        if "plot_raw_x" in locals() and not args.no_plot:
            update_impedance_plot(plot_raw_x, plot_raw_y, plot_path)
        ad3.close()
        if laser is not None:
            laser.close()
        if atmo is not None:
            atmo.close()
        metadata["status"] = stop_reason
        metadata["finished_at"] = datetime.now().isoformat(timespec="seconds")
        metadata["rows_written"] = rows_written
        write_json(meta_path, metadata)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
