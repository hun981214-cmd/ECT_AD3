#!/usr/bin/env python3
"""Fast interactive AD3 impedance-plane viewer."""

from __future__ import annotations

import argparse
from collections import deque
import math
import time
from typing import Deque

import matplotlib
import numpy as np

from ad3_ratiometric_lockin import AD3ContinuousRatioLockIn
from ad3_triggered_lockin import AD3TriggeredLockIn

MODE_DEFAULTS = {
    "ratio": {
        "output_channel": 0,
        "input_channel": 0,
        "reference_channel": 1,
        "reference_output_channel": 1,
        "drive_freq_hz": 110_000.0,
        "drive_amplitude_v": 0.15,
        "buffer_size": 16_384,
    },
    "triggered": {
        "output_channel": 0,
        "input_channel": 0,
        "reference_channel": None,
        "reference_output_channel": None,
        "drive_freq_hz": 10_000.0,
        "drive_amplitude_v": 1.0,
        "buffer_size": 10_000,
    },
}


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
            timeout_s=args.timeout_s,
            lockin_window_cycles=args.lockin_window_cycles,
            lockin_window_mad_threshold=args.lockin_window_mad_threshold,
            lockin_min_windows=args.lockin_min_windows,
            lockin_window_filter=not args.no_lockin_window_filter,
            max_lockin_window_phase_std_deg=args.max_lockin_window_phase_std_deg,
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
        timeout_s=args.timeout_s,
        discard_s=args.discard_s,
        lockin_window_cycles=args.lockin_window_cycles,
        lockin_window_mad_threshold=args.lockin_window_mad_threshold,
        lockin_min_windows=args.lockin_min_windows,
        lockin_window_filter=not args.no_lockin_window_filter,
        max_lockin_window_phase_std_deg=args.max_lockin_window_phase_std_deg,
    )


GUI_BACKEND_CANDIDATES = ("TkAgg", "QtAgg", "Qt5Agg", "GTK3Agg", "WXAgg", "WebAgg")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=tuple(MODE_DEFAULTS), default="ratio")
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
    parser.add_argument("--averages", type=int, default=1)
    parser.add_argument("--discard-s", type=float, default=0.001)
    parser.add_argument("--timeout-s", type=float, default=3.0)
    parser.add_argument("--interval-s", type=float, default=0.0, help="Minimum delay between displayed measurements. 0 runs as fast as AD3 allows.")
    parser.add_argument("--history", type=int, default=1000, help="Number of recent impedance points to keep on screen.")
    parser.add_argument("--autoscale-every", type=int, default=10, help="Rescale axes every N accepted points. 0 disables autoscale.")
    parser.add_argument("--margin-frac", type=float, default=0.12)
    parser.add_argument("--point-size", type=float, default=14.0)
    parser.add_argument("--line-width", type=float, default=0.8)
    parser.add_argument("--no-line", action="store_true")
    parser.add_argument("--print-every", type=int, default=10)
    parser.add_argument("--backend", default=None, help="Optional matplotlib GUI backend, for example TkAgg, QtAgg, or WebAgg.")
    parser.add_argument("--ad3-retries", type=int, default=2)
    parser.add_argument("--ad3-retry-delay-s", type=float, default=0.05)
    parser.add_argument("--ad3-recover-delay-s", type=float, default=0.15)
    parser.add_argument("--ad3-hard-reopen-after", type=int, default=2)
    parser.add_argument("--lockin-window-cycles", type=int, default=15)
    parser.add_argument("--lockin-window-mad-threshold", type=float, default=6.0)
    parser.add_argument("--lockin-min-windows", type=int, default=4)
    parser.add_argument("--max-lockin-window-phase-std-deg", type=float, default=5.0)
    parser.add_argument("--no-lockin-window-filter", action="store_true")
    return parser


def select_matplotlib_backend(requested_backend: str | None) -> str:
    if requested_backend:
        matplotlib.use(requested_backend, force=True)
        return requested_backend
    if matplotlib.get_backend().lower() != "agg":
        return matplotlib.get_backend()

    errors = []
    for backend in GUI_BACKEND_CANDIDATES:
        try:
            matplotlib.use(backend, force=True)
            return backend
        except Exception as exc:
            errors.append(f"{backend}: {type(exc).__name__}: {exc}")
    details = "\n".join(f"  - {line}" for line in errors)
    raise SystemExit(
        "A matplotlib GUI backend is required, but none could be loaded.\n"
        "If you are running from SSH, use a local desktop terminal, enable X11 forwarding, or try --backend WebAgg.\n"
        "Tried:\n"
        f"{details}"
    )


def finite_pair(x_value: float, y_value: float) -> bool:
    return math.isfinite(float(x_value)) and math.isfinite(float(y_value))


def read_measurement_with_retries(
    ad3: AD3TriggeredLockIn,
    retries: int,
    retry_delay_s: float,
    recover_delay_s: float,
    hard_reopen_after: int,
):
    attempts = max(0, retries) + 1
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            measurement, _waveform, _freqs, _amp = ad3.measure()
            return measurement, attempt
        except (TimeoutError, RuntimeError) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            reopen = hard_reopen_after > 0 and (attempt + 1) >= hard_reopen_after
            print(f"\n[warn] AD3 read failed ({exc}); recovery {'hard reopen' if reopen else 'soft reset'}.")
            ad3.recover(reopen=reopen, delay_s=recover_delay_s)
            time.sleep(max(0.0, retry_delay_s))
    assert last_exc is not None
    raise last_exc


def padded_limits(values: Deque[float], margin_frac: float) -> tuple[float, float]:
    low = min(values)
    high = max(values)
    if not math.isfinite(low) or not math.isfinite(high):
        return -1.0, 1.0
    if low == high:
        pad = max(abs(low) * 0.1, 1e-6)
    else:
        pad = (high - low) * max(0.0, margin_frac)
    return low - pad, high + pad


def main() -> int:
    args = build_parser().parse_args()
    select_matplotlib_backend(args.backend)
    import matplotlib.pyplot as plt

    if matplotlib.get_backend().lower() == "agg":
        raise SystemExit("A matplotlib GUI backend is required. Try --backend TkAgg, --backend QtAgg, or --backend WebAgg.")

    plt.ion()
    xs: Deque[float] = deque(maxlen=max(2, args.history))
    ys: Deque[float] = deque(maxlen=max(2, args.history))
    fig, ax = plt.subplots(figsize=(7.0, 6.4), dpi=110)
    if fig.canvas.manager is not None:
        fig.canvas.manager.set_window_title("AD3 Impedance Plane")
    ax.set_title("AD3 Impedance Plane")
    ax.set_xlabel("raw_x")
    ax.set_ylabel("raw_y")
    ax.axhline(0.0, color="#c7c7c7", linewidth=0.7)
    ax.axvline(0.0, color="#c7c7c7", linewidth=0.7)
    ax.grid(True, alpha=0.25)
    ax.set_aspect("equal", adjustable="datalim")
    trace_line, = ax.plot([], [], color="#496f8a", linewidth=args.line_width, alpha=0.55)
    points = ax.scatter([], [], s=args.point_size, color="#2563eb", alpha=0.82, edgecolors="none")
    latest = ax.scatter([], [], s=args.point_size * 5.0, marker="x", color="#d62728", linewidths=1.6, zorder=4)
    status_text = ax.text(
        0.02,
        0.98,
        "",
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        family="monospace",
        bbox={"facecolor": "white", "edgecolor": "#dddddd", "alpha": 0.82, "pad": 4},
    )

    running = {"value": True}
    paused = {"value": False}

    def on_key(event) -> None:
        if event.key in {"q", "escape"}:
            running["value"] = False
        elif event.key == " ":
            paused["value"] = not paused["value"]
        elif event.key == "r":
            xs.clear()
            ys.clear()
            trace_line.set_data([], [])
            points.set_offsets(np.empty((0, 2)))
            latest.set_offsets(np.empty((0, 2)))
            fig.canvas.draw_idle()

    def on_close(_event) -> None:
        running["value"] = False

    fig.canvas.mpl_connect("key_press_event", on_key)
    fig.canvas.mpl_connect("close_event", on_close)
    fig.show()

    sample_count = 0
    retry_count_total = 0
    started = time.monotonic()

    print("Opening Analog Discovery 3...")
    with build_driver(args) as ad3:
        print(f"AD3 readback: {ad3.readback()}")
        print("Interactive keys: space=pause, r=reset trace, q/esc=quit.")
        while running["value"] and plt.fignum_exists(fig.number):
            loop_started = time.monotonic()
            if paused["value"]:
                status_text.set_text(f"paused\nsamples={sample_count}")
                fig.canvas.flush_events()
                plt.pause(0.05)
                continue

            try:
                measurement, retries_used = read_measurement_with_retries(
                    ad3,
                    retries=args.ad3_retries,
                    retry_delay_s=args.ad3_retry_delay_s,
                    recover_delay_s=args.ad3_recover_delay_s,
                    hard_reopen_after=args.ad3_hard_reopen_after,
                )
            except Exception as exc:
                status_text.set_text(f"AD3 error\n{type(exc).__name__}: {exc}")
                fig.canvas.draw_idle()
                fig.canvas.flush_events()
                print(f"\n[warn] AD3 read failed after retries: {exc}")
                try:
                    ad3.recover(reopen=True, delay_s=args.ad3_recover_delay_s)
                except Exception as recover_exc:
                    print(f"[warn] AD3 hard reopen failed: {recover_exc}")
                plt.pause(0.1)
                continue

            retry_count_total += retries_used
            if finite_pair(measurement.raw_x, measurement.raw_y):
                xs.append(float(measurement.raw_x))
                ys.append(float(measurement.raw_y))
                sample_count += 1

            x_list = list(xs)
            y_list = list(ys)
            if x_list and y_list:
                if args.no_line:
                    trace_line.set_data([], [])
                else:
                    trace_line.set_data(x_list, y_list)
                points.set_offsets(list(zip(x_list, y_list)))
                latest.set_offsets([[x_list[-1], y_list[-1]]])
                if args.autoscale_every > 0 and (
                    sample_count == 1 or sample_count % args.autoscale_every == 0
                ):
                    ax.set_xlim(*padded_limits(xs, args.margin_frac))
                    ax.set_ylim(*padded_limits(ys, args.margin_frac))

            elapsed = time.monotonic() - started
            rate_hz = sample_count / elapsed if elapsed > 0 else 0.0
            status_text.set_text(
                f"n={sample_count}  {rate_hz:.2f} Hz\n"
                f"x={measurement.raw_x:+.6g}  y={measurement.raw_y:+.6g}\n"
                f"mag={measurement.magnitude:.6g}  phase={measurement.phase_deg:+.2f} deg\n"
                f"peak={measurement.peak_freq_hz:.1f} Hz  retries={retry_count_total}"
            )
            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            plt.pause(0.001)

            if args.print_every > 0 and sample_count % args.print_every == 0 and sample_count > 0:
                print(
                    f"{sample_count:06d} {rate_hz:5.2f}Hz "
                    f"x={measurement.raw_x:+.6g} y={measurement.raw_y:+.6g} "
                    f"mag={measurement.magnitude:.6g} phase={measurement.phase_deg:+.2f} "
                    f"peak={measurement.peak_freq_hz:.1f}Hz",
                    end="\r",
                )

            sleep_s = args.interval_s - (time.monotonic() - loop_started)
            if sleep_s > 0:
                time.sleep(sleep_s)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
