#!/usr/bin/env python3
"""Hands-free specimen check with live plastic-strain readout.

The ECT_AD3 counterpart of `ECT/sensor_cal.py`: start it with the probe
EMPTY, wait for the automatic baseline capture, then swap specimens freely
and watch the live table. No keyboard interaction; Ctrl+C to stop.

Per row it prints the temperature-corrected complex ratio, the delta from
the session baseline, and a unified plastic-strain estimate: the Keyence
laser measures the cross section, the area picks the wire type, and the
baseline-delta projects onto that type's PE trajectory
(analysis/fit_pe_ratio_model.py, LOO RMSE ~3 %p).

  area  : thickness x width from the Keyence laser (mm^2)
  type  : wire type classified from the area
  PE%   : unified plastic-strain estimate
  nearest : closest reference specimen when on-trajectory, `-` when far
            from both (e.g. empty probe or mid-swap)

Without the laser (--no-laser or Keyence offline) it falls back to printing
PE_t1/PE_t2, the per-type estimates - read the column of the inserted type.

The measurement loop runs in a supervised child process: libdwf can segfault
the interpreter during degraded-USB episodes, so the parent restarts the
worker automatically; the baseline is saved and restored across restarts
(no re-capture with a specimen inserted). All rows are appended to a session
CSV under logs/.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from ad3_ratiometric_lockin import AD3ContinuousRatioLockIn  # noqa: E402
from ad3_timeseries_logger import (  # noqa: E402
    open_atmosphere,
    open_laser,
    read_atmosphere,
    read_laser,
)

TEMP_MODEL_PATH = SCRIPT_DIR / "analysis" / "temp_response_20260713.json"
REFERENCE_PATH = SCRIPT_DIR / "analysis" / "ratio_probe_specimens_20260714.json"
PE_MODEL_PATH = SCRIPT_DIR / "analysis" / "pe_ratio_model.json"
LOG_DIR = SCRIPT_DIR / "logs"


class TempCorrector:
    """Correct raw_x/raw_y to a reference temperature with the measured
    lag-quadratic response. Spot checks ignore the ~5 min thermal lag, which
    is fine in a quasi-static room."""

    def __init__(self, path: Path, ref_temp_c: float) -> None:
        self.ok = False
        self.ref_temp_c = ref_temp_c
        try:
            model = json.loads(path.read_text())["results"]
            self.coef = {
                axis: (
                    model[axis]["lag_quadratic"]["temp_coef"],
                    model[axis]["lag_quadratic"]["temp2_coef"],
                )
                for axis in ("raw_x", "raw_y")
            }
            self.ok = True
        except Exception as exc:
            print(f"[warn] temperature model unavailable ({exc}); showing raw values only")

    def correct(self, x: float, y: float, temp_c: float) -> tuple[float, float]:
        if not self.ok or not math.isfinite(temp_c):
            return x, y
        t, t0 = temp_c, self.ref_temp_c
        cx1, cx2 = self.coef["raw_x"]
        cy1, cy2 = self.coef["raw_y"]
        return (
            x - (cx1 * (t - t0) + cx2 * (t * t - t0 * t0)),
            y - (cy1 * (t - t0) + cy2 * (t * t - t0 * t0)),
        )


class ReferenceTrajectories:
    """Per-type PE trajectories in baseline-delta space, from the 2026-07-10
    reference sweep. PE is estimated by projecting a measured delta onto a
    type's polyline (mild extrapolation on the end segments)."""

    EXTRAPOLATE_PE = 5.0  # allow estimates a little beyond 0..30 %

    def __init__(self, path: Path) -> None:
        self.paths: dict[str, list[tuple[float, float, float]]] = {}
        self.specimens: list[dict] = []
        self.on_path_threshold = float("nan")
        try:
            data = json.loads(path.read_text())
            base = data["baseline"]
            steps = []
            for spec in sorted(data["specimens"], key=lambda s: (s["type"], s["pe_pct"])):
                self.paths.setdefault(spec["type"], []).append(
                    (float(spec["pe_pct"]), spec["x"] - base["x"], spec["y"] - base["y"])
                )
                self.specimens.append({
                    "label": f"{spec['type']}_pe{spec['pe_pct']:02d}",
                    "dx": spec["x"] - base["x"],
                    "dy": spec["y"] - base["y"],
                })
            for pts in self.paths.values():
                steps += [
                    math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts, pts[1:])
                ]
            # "on trajectory" = within half of a typical 10 % PE step
            self.on_path_threshold = 0.5 * float(np.median(steps)) if steps else float("nan")
        except Exception as exc:
            print(f"[warn] reference sweep unavailable ({exc}); PE estimation disabled")

    def project(self, spec_type: str, dx: float, dy: float) -> tuple[float, float]:
        """Return (pe_estimate_pct, distance_off_path)."""
        pts = self.paths.get(spec_type)
        if not pts:
            return float("nan"), float("nan")
        best_d, best_pe = float("inf"), float("nan")
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            pe_a, xa, ya = a
            pe_b, xb, yb = b
            vx, vy = xb - xa, yb - ya
            seg_len2 = vx * vx + vy * vy
            if seg_len2 <= 0:
                continue
            t = ((dx - xa) * vx + (dy - ya) * vy) / seg_len2
            pe_span = pe_b - pe_a
            slack = self.EXTRAPOLATE_PE / pe_span if pe_span > 0 else 0.0
            lo = -slack if i == 0 else 0.0
            hi = 1.0 + slack if i == len(pts) - 2 else 1.0
            t = min(max(t, lo), hi)
            px, py = xa + t * vx, ya + t * vy
            d = math.hypot(dx - px, dy - py)
            if d < best_d:
                best_d, best_pe = d, pe_a + t * pe_span
        return best_pe, best_d

    def nearest_label(self, dx: float, dy: float,
                      spec_type: str | None = None) -> tuple[str, float]:
        pool = [s for s in self.specimens
                if spec_type is None or s["label"].startswith(spec_type)]
        if not pool:
            return "-", float("nan")
        best = min(pool, key=lambda s: math.hypot(dx - s["dx"], dy - s["dy"]))
        return best["label"], math.hypot(dx - best["dx"], dy - best["dy"])

    def reference_delta(self, label: str) -> tuple[float, float] | None:
        for s in self.specimens:
            if s["label"] == label:
                return s["dx"], s["dy"]
        return None


class PEModel:
    """Unified PE: laser area picks the wire type, the delta projects onto
    that type's PE trajectory (analysis/fit_pe_ratio_model.py, LOO ~3 %p)."""

    def __init__(self, path: Path, trajectories: ReferenceTrajectories) -> None:
        self.ok = False
        self.trajectories = trajectories
        try:
            model = json.loads(path.read_text())
            self.area_threshold = float(model["area_threshold_mm2"])
            self.loo_rmse = float(model["loo_rmse_pct"])
            self.ok = True
        except Exception as exc:
            print(f"[warn] PE model unavailable ({exc}); unified PE disabled")

    def estimate(self, dx: float, dy: float, area_mm2: float) -> tuple[float, str]:
        if not self.ok or not math.isfinite(area_mm2):
            return float("nan"), "?"
        spec_type = "type1" if area_mm2 > self.area_threshold else "type2"
        pe, _off = self.trajectories.project(spec_type, dx, dy)
        return pe, spec_type


def supervise(args: argparse.Namespace) -> int:
    """Respawn the measurement worker when libdwf kills it (SIGSEGV)."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = LOG_DIR / f"specimen_check_{stamp}_session.json"
    csv_path = LOG_DIR / f"specimen_check_{stamp}_rows.csv"
    cmd = [
        sys.executable, "-u", str(Path(__file__).resolve()), "--worker",
        "--session", str(session_path),
        "--rows-csv", str(csv_path),
        "--interval", str(args.interval),
        "--baseline-rows", str(args.baseline_rows),
        "--averages", str(args.averages),
        "--ref-temp-c", str(args.ref_temp_c),
        "--duration-s", str(args.duration_s),
    ]
    if args.no_temp_correct:
        cmd.append("--no-temp-correct")
    if args.no_atmosphere:
        cmd.append("--no-atmosphere")
    if args.no_laser:
        cmd.append("--no-laser")
    if args.reuse_baseline:
        cmd.append("--reuse-baseline")
    if args.anchor:
        cmd += ["--anchor", args.anchor]
    fast_fails = 0
    while True:
        spawn = time.monotonic()
        try:
            result = subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            return 0
        if result.returncode == 0:
            return 0
        # A worker that dies immediately is a configuration error, not a USB
        # crash - restarting would loop forever.
        fast_fails = fast_fails + 1 if time.monotonic() - spawn < 10.0 else 0
        if fast_fails >= 2:
            print(f"[supervisor] worker keeps failing at startup "
                  f"(exit {result.returncode}); giving up")
            return result.returncode
        print(f"\n[supervisor] worker died (exit {result.returncode}, likely a "
              f"libdwf USB crash); restarting in 3 s - baseline is kept")
        time.sleep(3.0)


def fmt(val: float, width: int = 11, decimals: int = 6) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return f"{'N/A':>{width}}"
    return f"{val:>{width}.{decimals}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--baseline-rows", type=int, default=10,
                        help="rows averaged for the automatic startup baseline (default 10)")
    parser.add_argument("--averages", type=int, default=16,
                        help="AD3 captures pooled per row (default 16 for ~0.7 s rows)")
    parser.add_argument("--ref-temp-c", type=float, default=27.0,
                        help="temperature all values are corrected to (default 27.0, the 2026-07-10 sweep condition)")
    parser.add_argument("--no-temp-correct", action="store_true")
    parser.add_argument("--no-atmosphere", action="store_true")
    parser.add_argument("--no-laser", action="store_true",
                        help="skip the Keyence laser; PE falls back to per-type columns")
    parser.add_argument("--reuse-baseline", action="store_true",
                        help="reuse the most recent session baseline instead of "
                             "recapturing (safe only if the probe/fixture was not "
                             "touched since - handling shifts the baseline ~1%%)")
    parser.add_argument("--anchor", default=None, metavar="LABEL",
                        help="declare that the FIRST specimen inserted after the "
                             "baseline is this reference (e.g. type1_pe00); its "
                             "measured-vs-reference delta becomes a session offset "
                             "applied to all later measurements. Fixes the ~1%% "
                             "day-to-day trajectory shift that hits type2 hardest")
    parser.add_argument("--duration-s", type=float, default=0.0,
                        help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--rows-csv", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.worker:
        return supervise(args)

    corrector = TempCorrector(TEMP_MODEL_PATH, args.ref_temp_c)
    if args.no_temp_correct:
        corrector.ok = False
    reference = ReferenceTrajectories(REFERENCE_PATH)
    pe_model = PEModel(PE_MODEL_PATH, reference)

    atmo = None if args.no_atmosphere else open_atmosphere(
        argparse.Namespace(no_atmosphere=False, atmo_host="192.168.0.34", atmo_port=502))
    laser = None if args.no_laser else open_laser(
        argparse.Namespace(no_laser=False, laser_host="192.168.0.111",
                           laser_port=64000, laser_timeout=2.0, laser_settle_s=0.08))
    laser_on = laser is not None and pe_model.ok

    session_path = Path(args.session) if args.session else (
        LOG_DIR / f"specimen_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}_session.json"
    )
    csv_path = Path(args.rows_csv) if args.rows_csv else session_path.with_suffix(".csv")
    LOG_DIR.mkdir(exist_ok=True)

    baseline: dict | None = None
    baseline_rows: list[tuple[float, float]] = []
    offset_xy = (0.0, 0.0)
    anchor_label = args.anchor
    anchor_done = anchor_label is None
    anchor_rows: list[tuple[float, float]] = []
    if anchor_label and reference.reference_delta(anchor_label) is None:
        known = ", ".join(s["label"] for s in reference.specimens)
        raise SystemExit(f"unknown --anchor '{anchor_label}'; choose one of: {known}")

    def save_session() -> None:
        session_path.write_text(json.dumps({
            "baseline": baseline,
            "offset_xy": list(offset_xy),
            "anchor_label": anchor_label if anchor_done and anchor_label else None,
        }, indent=2), encoding="utf-8")

    if session_path.exists():
        try:
            session = json.loads(session_path.read_text())
            baseline = session.get("baseline")
            if baseline:
                print(f"[resume] baseline restored: x={baseline['x']:+.6f} y={baseline['y']:+.6f}")
            if session.get("anchor_label"):
                offset_xy = tuple(session.get("offset_xy", (0.0, 0.0)))
                anchor_label = session["anchor_label"]
                anchor_done = True
                print(f"[resume] session offset restored from anchor {anchor_label}: "
                      f"({offset_xy[0]:+.2e}, {offset_xy[1]:+.2e})")
        except Exception as exc:
            print(f"[warn] could not restore session ({exc})")
    if baseline is None and args.reuse_baseline:
        candidates = sorted(
            (p for p in LOG_DIR.glob("specimen_check_*_session.json") if p != session_path),
            key=lambda p: p.stat().st_mtime,
        )
        for prev in reversed(candidates):
            try:
                old = json.loads(prev.read_text()).get("baseline")
            except Exception:
                continue
            if old:
                baseline = old
                save_session()
                print(f"[reuse] baseline from {prev.name} "
                      f"(captured {old.get('captured', '?')}): "
                      f"x={old['x']:+.6f} y={old['y']:+.6f} - only valid if the "
                      f"fixture was not touched since")
                break
        if baseline is None:
            print("[warn] --reuse-baseline: no previous session baseline found; capturing fresh")

    csv_new = not csv_path.exists()
    csv_file = csv_path.open("a", newline="")
    writer = csv.writer(csv_file)
    if csv_new:
        writer.writerow(["timestamp", "x_corr", "y_corr", "magnitude", "phase_deg",
                         "delta_base_pct", "pe_t1_pct", "pe_t2_pct",
                         "area_mm2", "pe_pct", "spec_type",
                         "nearest", "temp_c"])

    print("Opening Analog Discovery 3 (ratio mode, probe standard)...")
    # Generous in-row capture retries: on this Jetson the USB link throws
    # frequent transient FDwf errors; absorbing them inside measure() avoids
    # noisy recover() cycles (each of which restarts the AWG for ~1 s).
    driver = AD3ContinuousRatioLockIn(
        averages=args.averages, capture_retries=12, capture_retry_delay_s=0.05
    )
    driver.open()
    print(f"AD3 readback: {driver.readback()}")
    if baseline is None:
        print(f"\n>>> PROBE MUST BE EMPTY: capturing baseline from the next "
              f"{args.baseline_rows} rows <<<\n")
    if laser_on:
        print(f"{'time':>8} {'x_corr':>11} {'y_corr':>11} {'d_base%':>8} "
              f"{'area':>6} {'type':>6} {'PE%':>6} {'nearest':>11} {'temp':>6}")
    else:
        print(f"{'time':>8} {'x_corr':>11} {'y_corr':>11} {'d_base%':>8} "
              f"{'PE_t1%':>7} {'PE_t2%':>7} {'nearest':>11} {'temp':>6}")

    started = time.monotonic()
    try:
        while True:
            loop_start = time.monotonic()
            if args.duration_s > 0 and loop_start - started >= args.duration_s:
                break
            try:
                measurement, *_ = driver.measure()
            except (RuntimeError, TimeoutError) as exc:
                print(f"[warn] AD3 read failed ({str(exc).splitlines()[0]}); recovering...")
                try:
                    driver.recover()
                except Exception:
                    driver.recover(reopen=True)
                continue

            temp_c, _humid = read_atmosphere(atmo)
            x, y = corrector.correct(measurement.raw_x, measurement.raw_y, temp_c)
            mag = math.hypot(x, y)
            phase = math.degrees(math.atan2(y, x))

            if baseline is None:
                baseline_rows.append((x, y))
                note = f"baseline {len(baseline_rows)}/{args.baseline_rows}"
                print(f"{datetime.now().strftime('%H:%M:%S'):>8} {fmt(x)} {fmt(y)} "
                      f"{'':>8} {'':>7} {'':>7} {'':>11} {fmt(temp_c, 6, 2)}  {note}")
                if len(baseline_rows) >= args.baseline_rows:
                    xs, ys = (np.array(v) for v in zip(*baseline_rows))
                    baseline = {"x": float(xs.mean()), "y": float(ys.mean()),
                                "mag": float(math.hypot(xs.mean(), ys.mean())),
                                "captured": datetime.now().isoformat(timespec="seconds")}
                    save_session()
                    print(f"\n[baseline] x={baseline['x']:+.6f} y={baseline['y']:+.6f} "
                          f"|r|={baseline['mag']:.6f}"
                          + (f" - insert the ANCHOR specimen ({anchor_label}) now\n"
                             if not anchor_done else " - insert specimens now\n"))
                continue

            dx, dy = x - baseline["x"], y - baseline["y"]

            # Anchor: the first specimen inserted is the declared reference;
            # its measured-vs-reference delta becomes the session offset that
            # compensates the day-to-day trajectory shift (~1 % of |r|).
            if not anchor_done:
                raw_pct = math.hypot(dx, dy) / baseline["mag"] * 100
                if raw_pct > 5.0:
                    anchor_rows.append((dx, dy))
                    if len(anchor_rows) >= 13:  # skip 3 settling rows, avg 10
                        axs, ays = (np.array(v) for v in zip(*anchor_rows[3:]))
                        ref_d = reference.reference_delta(anchor_label)
                        offset_xy = (float(axs.mean() - ref_d[0]),
                                     float(ays.mean() - ref_d[1]))
                        anchor_done = True
                        save_session()
                        print(f"\n[anchor] session offset from {anchor_label}: "
                              f"({offset_xy[0]:+.2e}, {offset_xy[1]:+.2e}) = "
                              f"{math.hypot(*offset_xy)/baseline['mag']*100:.3f}% of |r| "
                              f"- swap specimens freely now\n")
                    else:
                        print(f"{datetime.now().strftime('%H:%M:%S'):>8} {fmt(x)} {fmt(y)} "
                              f"{fmt(raw_pct, 8, 3)}  anchor {anchor_label} "
                              f"{len(anchor_rows)}/13 - keep it still")
                        continue
                else:
                    anchor_rows.clear()

            dx, dy = dx - offset_xy[0], dy - offset_xy[1]
            delta_pct = math.hypot(dx, dy) / baseline["mag"] * 100
            pe_t1, off1 = reference.project("type1", dx, dy)
            pe_t2, off2 = reference.project("type2", dx, dy)
            on_path = min(off1, off2) <= reference.on_path_threshold
            if not on_path:
                # far from both trajectories (empty probe, mid-swap, or an
                # unknown object): a projected PE would be meaningless
                pe_t1 = pe_t2 = float("nan")

            area_mm2 = float("nan")
            pe_unified = float("nan")
            spec_type = "?"
            type_hint = None
            if laser_on:
                thickness_mm, width_mm = read_laser(laser)
                area_mm2 = thickness_mm * width_mm
                if math.isfinite(area_mm2) and pe_model.ok:
                    type_hint = "type1" if area_mm2 > pe_model.area_threshold else "type2"
                if on_path:
                    pe_unified, spec_type = pe_model.estimate(dx, dy, area_mm2)
            label, _dist = reference.nearest_label(dx, dy, type_hint)
            shown_label = label if on_path else "-"

            stamp_now = datetime.now()
            if laser_on:
                print(f"{stamp_now.strftime('%H:%M:%S'):>8} {fmt(x)} {fmt(y)} "
                      f"{fmt(delta_pct, 8, 3)} {fmt(area_mm2, 6, 2)} {spec_type:>6} "
                      f"{fmt(pe_unified, 6, 1)} {shown_label:>11} {fmt(temp_c, 6, 2)}")
            else:
                print(f"{stamp_now.strftime('%H:%M:%S'):>8} {fmt(x)} {fmt(y)} "
                      f"{fmt(delta_pct, 8, 3)} {fmt(pe_t1, 7, 1)} {fmt(pe_t2, 7, 1)} "
                      f"{shown_label:>11} {fmt(temp_c, 6, 2)}")
            writer.writerow([stamp_now.isoformat(timespec="milliseconds"),
                             f"{x:.8f}", f"{y:.8f}", f"{mag:.8f}", f"{phase:.5f}",
                             f"{delta_pct:.5f}", f"{pe_t1:.3f}", f"{pe_t2:.3f}",
                             f"{area_mm2:.4f}", f"{pe_unified:.3f}", spec_type,
                             shown_label, f"{temp_c:.2f}"])
            csv_file.flush()

            sleep_s = args.interval - (time.monotonic() - loop_start)
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        driver.close()
        for sensor in (atmo, laser):
            close = getattr(sensor, "close", None)
            if callable(close):
                close()
        csv_file.close()
        print(f"rows saved: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
