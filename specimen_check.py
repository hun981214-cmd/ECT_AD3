#!/usr/bin/env python3
"""Interactive specimen check for the AD3 ratiometric ECT path.

The ECT_AD3 counterpart of `ECT/sensor_cal.py`: run it, insert specimens, and
watch live numbers. On top of the live table it adds what reproducibility
checking needs:

- session baseline capture and delta display (specimen effect, not absolute),
- temperature correction to a reference temperature using the measured
  quadratic response (`analysis/temp_response_20260713.json`), so numbers are
  comparable across days with different room temperature,
- automatic matching of recorded specimens against the 2026-07-10 reference
  sweep (`analysis/ratio_probe_specimens_20260710.json`) in
  baseline-delta space, with the distance expressed in reference sigmas.

Usage:
  python specimen_check.py                 # live table, interactive commands
  python specimen_check.py --interval 1.0 --avg-rows 10

Commands (type + Enter while running):
  b             capture session BASELINE (no specimen on the probe)
  m <label>     record a labeled measurement (e.g. `m t1_pe10`), averaged
                over --avg-rows rows, printed vs baseline and vs reference
  r             re-print the record table
  q             quit (records saved to logs/specimen_check_*_records.json)
"""

from __future__ import annotations

import argparse
import json
import math
import select
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from ad3_ratiometric_lockin import AD3ContinuousRatioLockIn  # noqa: E402
from ad3_timeseries_logger import open_atmosphere, read_atmosphere  # noqa: E402

TEMP_MODEL_PATH = SCRIPT_DIR / "analysis" / "temp_response_20260713.json"
REFERENCE_PATH = SCRIPT_DIR / "analysis" / "ratio_probe_specimens_20260710.json"
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


def load_reference(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        base = data["baseline"]
        refs = []
        for spec in data["specimens"]:
            refs.append({
                "label": f"{spec['type']}_pe{spec['pe_pct']:02d}",
                "dx": spec["x"] - base["x"],
                "dy": spec["y"] - base["y"],
                "sigma": max(spec.get("sigma_step", spec["sigma"]), 1e-9),
            })
        return {"specimens": refs, "baseline_mag": math.hypot(base["x"], base["y"])}
    except Exception as exc:
        print(f"[warn] reference sweep unavailable ({exc}); matching disabled")
        return {"specimens": [], "baseline_mag": float("nan")}


def read_command() -> str | None:
    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if not line:
            return "q"  # EOF (piped input exhausted)
        return line.strip()
    return None


def fmt(val: float, width: int = 11, decimals: int = 6) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return f"{'N/A':>{width}}"
    return f"{val:>{width}.{decimals}f}"


def print_records(records: list[dict]) -> None:
    if not records:
        print("(no records yet)")
        return
    print(f"\n{'label':>12} {'x_corr':>11} {'y_corr':>11} {'|d|/base%':>10} "
          f"{'match':>10} {'dist(sig)':>10} {'temp':>6}")
    for r in records:
        print(f"{r['label']:>12} {fmt(r['x_corr'])} {fmt(r['y_corr'])} "
              f"{fmt(r.get('delta_pct', float('nan')), 10, 3)} "
              f"{r.get('match', '-'):>10} {fmt(r.get('match_sigma', float('nan')), 10, 1)} "
              f"{fmt(r.get('temp_c', float('nan')), 6, 2)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--avg-rows", type=int, default=10,
                        help="rows averaged per `b`/`m` capture (default 10)")
    parser.add_argument("--averages", type=int, default=16,
                        help="AD3 captures pooled per row (default 16 for ~0.7 s rows)")
    parser.add_argument("--ref-temp-c", type=float, default=27.0,
                        help="temperature all values are corrected to (default 27.0, the 2026-07-10 sweep condition)")
    parser.add_argument("--no-temp-correct", action="store_true")
    parser.add_argument("--no-atmosphere", action="store_true")
    args = parser.parse_args()

    corrector = TempCorrector(TEMP_MODEL_PATH, args.ref_temp_c)
    if args.no_temp_correct:
        corrector.ok = False
    reference = load_reference(REFERENCE_PATH)

    atmo = None if args.no_atmosphere else open_atmosphere(
        argparse.Namespace(no_atmosphere=False, atmo_host="192.168.0.34", atmo_port=502))

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    records_path = LOG_DIR / f"specimen_check_{stamp}_records.json"

    records: list[dict] = []
    baseline: dict | None = None
    pending: dict | None = None  # {"label": str, "rows": [(x, y, temp)]}

    print("Opening Analog Discovery 3 (ratio mode, probe standard)...")
    driver = AD3ContinuousRatioLockIn(averages=args.averages)
    driver.open()
    print(f"AD3 readback: {driver.readback()}")
    print(__doc__.split("Commands", 1)[1].join(["Commands", ""]))
    print(f"{'time':>8} {'x_corr':>11} {'y_corr':>11} {'|ratio|':>10} {'phase':>9} "
          f"{'d_base%':>8} {'temp':>6} {'note':>14}")

    try:
        while True:
            loop_start = time.monotonic()
            command = read_command()
            if command is not None:
                if command == "q":
                    break
                elif command in ("b", ""):
                    pending = {"label": "baseline", "rows": []}
                    print(f"[capture] baseline: averaging next {args.avg_rows} rows - keep the probe empty")
                elif command.startswith("m"):
                    label = command[1:].strip() or f"spec{len(records):02d}"
                    pending = {"label": label, "rows": []}
                    print(f"[capture] '{label}': averaging next {args.avg_rows} rows - keep the specimen still")
                elif command == "r":
                    print_records(records)

            try:
                measurement, *_ = driver.measure()
            except (RuntimeError, TimeoutError) as exc:
                print(f"[warn] AD3 read failed ({exc}); recovering...")
                try:
                    driver.recover()
                except Exception:
                    driver.recover(reopen=True)
                continue

            temp_c, _humid = read_atmosphere(atmo)
            x_raw, y_raw = measurement.raw_x, measurement.raw_y
            x, y = corrector.correct(x_raw, y_raw, temp_c)
            mag = math.hypot(x, y)
            phase = math.degrees(math.atan2(y, x))

            note = ""
            delta_pct = float("nan")
            if baseline is not None:
                d = math.hypot(x - baseline["x_corr"], y - baseline["y_corr"])
                delta_pct = d / baseline["mag"] * 100

            if pending is not None:
                pending["rows"].append((x, y, temp_c))
                note = f"cap {len(pending['rows'])}/{args.avg_rows}"
                if len(pending["rows"]) >= args.avg_rows:
                    xs, ys, ts = (np.array(v, dtype=float) for v in zip(*pending["rows"]))
                    record = {
                        "label": pending["label"],
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "x_corr": float(xs.mean()),
                        "y_corr": float(ys.mean()),
                        "x_std": float(xs.std(ddof=1)),
                        "y_std": float(ys.std(ddof=1)),
                        "temp_c": float(np.nanmean(ts)),
                        "temp_corrected_to_c": args.ref_temp_c if corrector.ok else None,
                        "n_rows": int(len(xs)),
                    }
                    if pending["label"] == "baseline":
                        record["mag"] = math.hypot(record["x_corr"], record["y_corr"])
                        baseline = record
                        print(f"[baseline] x={record['x_corr']:+.6f} y={record['y_corr']:+.6f} "
                              f"|r|={record['mag']:.6f} (scatter {record['x_std']:.2e}/{record['y_std']:.2e})")
                    else:
                        if baseline is not None:
                            dx = record["x_corr"] - baseline["x_corr"]
                            dy = record["y_corr"] - baseline["y_corr"]
                            record["delta_pct"] = math.hypot(dx, dy) / baseline["mag"] * 100
                            if reference["specimens"]:
                                best = min(
                                    reference["specimens"],
                                    key=lambda s: math.hypot(dx - s["dx"], dy - s["dy"]),
                                )
                                dist = math.hypot(dx - best["dx"], dy - best["dy"])
                                record["match"] = best["label"]
                                record["match_sigma"] = dist / best["sigma"]
                                record["match_dist_pct_of_base"] = (
                                    dist / reference["baseline_mag"] * 100
                                )
                        else:
                            print("[warn] no session baseline - capture one with `b` for deltas/matching")
                        records.append(record)
                        print(f"[record] {record['label']}: x={record['x_corr']:+.6f} "
                              f"y={record['y_corr']:+.6f}"
                              + (f"  d_base={record['delta_pct']:.3f}%" if "delta_pct" in record else "")
                              + (f"  nearest ref: {record['match']} at {record['match_sigma']:.1f} sigma"
                                 f" ({record['match_dist_pct_of_base']:.3f}% of base)"
                                 if "match" in record else ""))
                    pending = None

            print(f"{datetime.now().strftime('%H:%M:%S'):>8} {fmt(x)} {fmt(y)} "
                  f"{fmt(mag, 10)} {fmt(phase, 9, 4)} {fmt(delta_pct, 8, 3)} "
                  f"{fmt(temp_c, 6, 2)} {note:>14}")

            sleep_s = args.interval - (time.monotonic() - loop_start)
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        driver.close()
        close = getattr(atmo, "close", None)
        if callable(close):
            close()
        payload = {
            "started": stamp,
            "ref_temp_c": args.ref_temp_c if corrector.ok else None,
            "baseline": baseline,
            "records": records,
        }
        records_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print_records(records)
        print(f"records saved: {records_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
