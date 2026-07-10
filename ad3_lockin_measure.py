#!/usr/bin/env python3
"""Short diagnostic lock-in measurement for AD3 ECT.

Supports the continuous-drive ratiometric mode (default, two-coil path) and
the legacy AWG-restart triggered mode.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from ad3_ratiometric_lockin import AD3ContinuousRatioLockIn
from ad3_triggered_lockin import AD3TriggeredLockIn, TriggeredLockInMeasurement


SCRIPT_DIR = Path(__file__).resolve().parent

MODE_DEFAULTS = {
    "ratio": {
        "output_channel": 0,
        "input_channel": 0,
        "reference_channel": 1,
        "reference_output_channel": 1,
        "drive_freq_hz": 120_000.0,
        "drive_amplitude_v": 4.0,
        "buffer_size": 16_384,
        "averages": 4,
    },
    "triggered": {
        "output_channel": 0,
        "input_channel": 0,
        "reference_channel": None,
        "reference_output_channel": None,
        "drive_freq_hz": 10_000.0,
        "drive_amplitude_v": 1.0,
        "buffer_size": 10_000,
        "averages": 1,
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=tuple(MODE_DEFAULTS), default="ratio")
    parser.add_argument("--log-dir", default=str(SCRIPT_DIR / "logs"))
    parser.add_argument("--duration-s", type=float, default=20.0)
    parser.add_argument("--interval-s", type=float, default=0.2)
    parser.add_argument("--output-channel", type=int, default=None, choices=(0, 1))
    parser.add_argument("--input-channel", type=int, default=None, choices=(0, 1))
    parser.add_argument("--reference-channel", type=int, default=None, choices=(0, 1))
    parser.add_argument("--reference-output-channel", type=int, default=None, choices=(0, 1))
    parser.add_argument("--drive-freq-hz", type=float, default=None)
    parser.add_argument("--drive-amplitude-v", type=float, default=None)
    parser.add_argument("--sample-rate-hz", type=float, default=1_000_000.0)
    parser.add_argument("--buffer-size", type=int, default=None)
    parser.add_argument("--analog-range-v", type=float, default=5.0)
    parser.add_argument("--reference-range-v", type=float, default=None)
    parser.add_argument("--averages", type=int, default=None)
    parser.add_argument("--discard-s", type=float, default=0.001)
    parser.add_argument("--plot-every", type=int, default=10)
    return parser


def build_driver(args: argparse.Namespace):
    for name, default in MODE_DEFAULTS[args.mode].items():
        if getattr(args, name) is None:
            setattr(args, name, default)
    if args.mode == "ratio":
        return AD3ContinuousRatioLockIn(
            output_channel=args.output_channel,
            input_channel=args.input_channel,
            reference_channel=args.reference_channel,
            reference_output_channel=args.reference_output_channel,
            drive_freq_hz=args.drive_freq_hz,
            drive_amplitude_v=args.drive_amplitude_v,
            sample_rate_hz=args.sample_rate_hz,
            buffer_size=args.buffer_size,
            signal_range_v=args.analog_range_v,
            reference_range_v=args.reference_range_v,
            averages=args.averages,
        )
    return AD3TriggeredLockIn(
        output_channel=args.output_channel,
        input_channel=args.input_channel,
        reference_channel=args.reference_channel,
        reference_output_channel=args.reference_output_channel,
        drive_freq_hz=args.drive_freq_hz,
        drive_amplitude_v=args.drive_amplitude_v,
        sample_rate_hz=args.sample_rate_hz,
        buffer_size=args.buffer_size,
        analog_range_v=args.analog_range_v,
        averages=args.averages,
        discard_s=args.discard_s,
    )


def save_plot(prefix: Path, xs: list[float], ys: list[float], freqs: np.ndarray, amp: np.ndarray, drive_freq_hz: float) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.6), dpi=140)
    axes[0].scatter(xs, ys, s=18, alpha=0.85)
    axes[0].plot(xs, ys, color="#777777", linewidth=0.7, alpha=0.35)
    axes[0].axhline(0, color="#bbbbbb", linewidth=0.7)
    axes[0].axvline(0, color="#bbbbbb", linewidth=0.7)
    axes[0].set_xlabel("lock-in X (V)")
    axes[0].set_ylabel("lock-in Y (V)")
    axes[0].set_title("Impedance Plane")
    axes[0].grid(True, alpha=0.25)
    axes[0].axis("equal")

    axes[1].plot(freqs / 1000.0, amp, linewidth=0.9)
    axes[1].axvline(drive_freq_hz / 1000.0, color="tab:red", linestyle="--", linewidth=1.0)
    axes[1].set_xlim(0.0, max(30.0, 2.0 * drive_freq_hz / 1000.0))
    axes[1].set_xlabel("frequency (kHz)")
    axes[1].set_ylabel("amplitude (V)")
    axes[1].set_title("Spectrum")
    axes[1].grid(True, alpha=0.25)

    fig.tight_layout()
    fig.savefig(prefix.with_suffix(".png"))
    plt.close(fig)


def main() -> int:
    args = build_parser().parse_args()
    log_dir = Path(args.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = log_dir / f"ad3_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    csv_path = prefix.with_suffix(".csv")

    fieldnames = ["number", "timestamp", "time_s"] + [
        field.name for field in fields(TriggeredLockInMeasurement)
    ]

    xs: list[float] = []
    ys: list[float] = []
    last_freqs = None
    last_amp = None

    with build_driver(args) as ad3:
        print("Readback:", ad3.readback())
        print(f"Logging to: {csv_path}")
        print(f"Plot: {prefix.with_suffix('.png')}")
        started = time.monotonic()
        number = 0
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            while True:
                loop_started = time.monotonic()
                time_s = loop_started - started
                if args.duration_s > 0 and time_s >= args.duration_s:
                    break
                measurement, _waveform, freqs, amp = ad3.measure()
                last_freqs = freqs
                last_amp = amp
                xs.append(measurement.raw_x)
                ys.append(measurement.raw_y)
                row = {
                    "number": number,
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                    "time_s": time_s,
                    **asdict(measurement),
                }
                writer.writerow(row)
                f.flush()
                print(
                    f"{number:05d} t={time_s:7.2f}s "
                    f"x={measurement.raw_x:+.6f} y={measurement.raw_y:+.6f} "
                    f"mag={measurement.magnitude:.6f} phase={measurement.phase_deg:+7.2f} "
                    f"peak={measurement.peak_freq_hz:.1f}Hz/{measurement.peak_amp:.6f} "
                    f"snr={measurement.snr_drive_to_peak:.3f}",
                    end="\r",
                )
                if args.plot_every > 0 and number % args.plot_every == 0:
                    save_plot(prefix, xs, ys, freqs, amp, args.drive_freq_hz)
                number += 1
                sleep_s = args.interval_s - (time.monotonic() - loop_started)
                if sleep_s > 0:
                    time.sleep(sleep_s)

    if last_freqs is not None and last_amp is not None:
        save_plot(prefix, xs, ys, last_freqs, last_amp, args.drive_freq_hz)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
