#!/usr/bin/env python3
"""Fit a unified plastic-strain model for the AD3 ratio path.

Combines the 2026-07-10 8-specimen ratio sweep (temperature-corrected complex
ratio, ECT_AD3/logs) with per-specimen cross sections measured by the Keyence
laser during the 2026-03-31 EW-8SCT calibration (ECT/logs). Specimen order in
the 3/31 files is thick-type PE 0/10/20/30 then thin-type PE 0/10/20/30;
the thick type is the 7/10 sweep's "type1" (confirmed by the user).

Candidate models (leave-one-out validated):
  A  PE = C_PH*phase_rad + C_AMP*|r| + C_AREA*area + C_PXAR*(phase_rad*area) + C0
     (the ECT/sensor_cal.py form, on absolute corrected values)
  B  PE = a*dx + b*dy + c*area + C0
     (baseline-delta features - immune to day-to-day absolute offset)
  C  PE = a*dx + b*dy + c*area + d*(dx*area) + C0
  D  area -> type classification (threshold between the two clusters), then
     projection of (dx, dy) onto that type's PE trajectory polyline
     (the two trajectories nearly continue each other in the complex plane,
     so linear pooled models blur across types; area separates them cleanly)

The chosen model (lowest LOO RMSE) is written to pe_ratio_model.json for
specimen_check.py to consume.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ANALYSIS_DIR = Path(__file__).resolve().parent
LOG_DIR = ANALYSIS_DIR.parent / "logs"
EW_LOG_DIR = ANALYSIS_DIR.parents[1] / "ECT" / "logs"
TEMP_MODEL_PATH = ANALYSIS_DIR / "temp_response_20260713.json"
REFERENCE_PATH = ANALYSIS_DIR / "ratio_probe_specimens_20260710.json"

# 3/31 calibration file order -> (sweep type label, PE %)
EW_FILE_ORDER = [
    ("type1", 0), ("type1", 10), ("type1", 20), ("type1", 30),
    ("type2", 0), ("type2", 10), ("type2", 20), ("type2", 30),
]
SPECIMEN_RUNS = {
    ("type1", 0): "spec_t1_pe00",
    ("type1", 10): "spec_t1_pe10",
    ("type1", 20): "spec_t1_pe20_r2",
    ("type1", 30): "spec_t1_pe30",
    ("type2", 0): "spec_t2_pe00",
    ("type2", 10): "spec_t2_pe10",
    ("type2", 20): "spec_t2_pe20",
    ("type2", 30): "spec_t2_pe30",
}


def temp_correct_factory():
    model = json.loads(TEMP_MODEL_PATH.read_text())["results"]
    coef = {a: (model[a]["lag_quadratic"]["temp_coef"], model[a]["lag_quadratic"]["temp2_coef"])
            for a in ("raw_x", "raw_y")}

    def correct(x, y, t, t0=27.0):
        cx1, cx2 = coef["raw_x"]
        cy1, cy2 = coef["raw_y"]
        return (x - (cx1 * (t - t0) + cx2 * (t * t - t0 * t0)),
                y - (cy1 * (t - t0) + cy2 * (t * t - t0 * t0)))
    return correct


def load_specimen(run_id: str, correct) -> tuple[float, float]:
    df = pd.read_csv(LOG_DIR / f"ad3_log_{run_id}.csv")
    ok = df[df["row_status"] == "ok"]
    if "is_stable" in ok and int(ok["is_stable"].sum()) >= 5:
        ok = ok[ok["is_stable"] == 1]
    t = float(ok["temp_c"].astype(float).mean())
    x = float(ok["raw_x"].astype(float).mean())
    y = float(ok["raw_y"].astype(float).mean())
    return correct(x, y, t)


def load_areas() -> list[float]:
    files = sorted(EW_LOG_DIR.glob("data_20260331_13*.csv"))[:8]
    if len(files) != 8:
        raise SystemExit(f"expected 8 EW calibration files, found {len(files)}")
    areas = []
    for f in files:
        df = pd.read_csv(f, comment="#")
        areas.append(float(df["laser1_mm"].mean() * df["laser2_mm"].mean()))
    return areas


def project_polyline(pts: list[tuple[float, float, float]], dx: float, dy: float,
                     extrapolate_pe: float = 5.0) -> float:
    """PE from projecting (dx, dy) onto a PE-parameterized polyline."""
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
        slack = extrapolate_pe / pe_span if pe_span > 0 else 0.0
        lo = -slack if i == 0 else 0.0
        hi = 1.0 + slack if i == len(pts) - 2 else 1.0
        t = min(max(t, lo), hi)
        d = math.hypot(dx - (xa + t * vx), dy - (ya + t * vy))
        if d < best_d:
            best_d, best_pe = d, pe_a + t * pe_span
    return best_pe


def loo_trajectory(df: pd.DataFrame, area_threshold: float) -> tuple[float, float, np.ndarray]:
    preds = np.empty(len(df))
    for i in range(len(df)):
        held = df.iloc[i]
        spec_type = "type1" if held["area_mm2"] > area_threshold else "type2"
        rest = df.drop(df.index[i])
        pts = [
            (float(r["pe_pct"]), float(r["dx"]), float(r["dy"]))
            for _, r in rest[rest["type"] == spec_type].sort_values("pe_pct").iterrows()
        ]
        preds[i] = project_polyline(pts, float(held["dx"]), float(held["dy"]),
                                    extrapolate_pe=12.0)
    err = preds - df["pe_pct"].to_numpy()
    return float(np.sqrt(np.mean(err**2))), float(np.max(np.abs(err))), preds


def loo_rmse(X: np.ndarray, y: np.ndarray) -> tuple[float, float, np.ndarray]:
    preds = np.empty_like(y)
    for i in range(len(y)):
        mask = np.arange(len(y)) != i
        coef, *_ = np.linalg.lstsq(X[mask], y[mask], rcond=None)
        preds[i] = X[i] @ coef
    err = preds - y
    return float(np.sqrt(np.mean(err**2))), float(np.max(np.abs(err))), preds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ANALYSIS_DIR / "pe_ratio_model.json"))
    args = parser.parse_args()

    correct = temp_correct_factory()
    base = json.loads(REFERENCE_PATH.read_text())["baseline"]
    base_x, base_y = base["x"], base["y"]

    areas = load_areas()
    rows = []
    for (spec, area) in zip(EW_FILE_ORDER, areas):
        x, y = load_specimen(SPECIMEN_RUNS[spec], correct)
        rows.append({
            "type": spec[0], "pe_pct": float(spec[1]), "area_mm2": area,
            "x": x, "y": y, "dx": x - base_x, "dy": y - base_y,
            "amp": math.hypot(x, y), "phase_rad": math.atan2(y, x),
        })
    df = pd.DataFrame(rows)
    print(df[["type", "pe_pct", "area_mm2", "amp", "phase_rad"]].to_string(index=False))

    y = df["pe_pct"].to_numpy()
    candidates = {
        "A_sensorcal_form": np.column_stack([
            df["phase_rad"], df["amp"], df["area_mm2"],
            df["phase_rad"] * df["area_mm2"], np.ones(len(df)),
        ]),
        "B_delta_linear": np.column_stack([
            df["dx"], df["dy"], df["area_mm2"], np.ones(len(df)),
        ]),
        "C_delta_interaction": np.column_stack([
            df["dx"], df["dy"], df["area_mm2"], df["dx"] * df["area_mm2"],
            np.ones(len(df)),
        ]),
    }
    feature_names = {
        "A_sensorcal_form": ["phase_rad", "amp", "area_mm2", "phase_rad*area_mm2", "offset"],
        "B_delta_linear": ["dx", "dy", "area_mm2", "offset"],
        "C_delta_interaction": ["dx", "dy", "area_mm2", "dx*area_mm2", "offset"],
    }

    area_threshold = float(
        (df[df["type"] == "type1"]["area_mm2"].min()
         + df[df["type"] == "type2"]["area_mm2"].max()) / 2.0
    )

    print("\n=== leave-one-out validation ===")
    results = {}
    for name, X in candidates.items():
        rmse, max_err, preds = loo_rmse(X, y)
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        results[name] = {"loo_rmse": rmse, "loo_max_err": max_err,
                         "coef": coef, "preds": preds}
        print(f"{name:24}: LOO RMSE {rmse:.3f} %p, LOO max |err| {max_err:.3f} %p")
    rmse_d, max_d, preds_d = loo_trajectory(df, area_threshold)
    results["D_area_type_trajectory"] = {"loo_rmse": rmse_d, "loo_max_err": max_d,
                                         "coef": None, "preds": preds_d}
    print(f"{'D_area_type_trajectory':24}: LOO RMSE {rmse_d:.3f} %p, "
          f"LOO max |err| {max_d:.3f} %p (area threshold {area_threshold:.3f} mm2)")

    best_name = min(results, key=lambda n: results[n]["loo_rmse"])
    best = results[best_name]
    print(f"\nselected: {best_name}")
    if best["coef"] is not None:
        for fname, c in zip(feature_names[best_name], best["coef"]):
            print(f"  {fname:20} {c:+.6e}")
    print("\nper-specimen LOO predictions:")
    for r, p in zip(rows, best["preds"]):
        print(f"  {r['type']}_pe{int(r['pe_pct']):02d}: true {r['pe_pct']:5.1f}  "
              f"pred {p:6.2f}  err {p - r['pe_pct']:+.2f}")

    payload = {
        "fitted": "2026-07-14",
        "model": best_name,
        "features": feature_names.get(best_name),
        "coefficients": ([float(c) for c in best["coef"]]
                         if best["coef"] is not None else None),
        "area_threshold_mm2": area_threshold,
        "trajectories": {
            spec_type: [
                [float(r["pe_pct"]), float(r["dx"]), float(r["dy"])]
                for _, r in df[df["type"] == spec_type].sort_values("pe_pct").iterrows()
            ]
            for spec_type in ("type1", "type2")
        },
        "loo_rmse_pct": best["loo_rmse"],
        "loo_max_err_pct": best["loo_max_err"],
        "loo_all_models": {n: {"rmse": v["loo_rmse"], "max": v["loo_max_err"]}
                           for n, v in results.items()},
        "baseline_ref": {"x": base_x, "y": base_y,
                         "note": "2026-07-10 no-specimen baseline; delta models use the live session baseline at runtime"},
        "ref_temp_c": 27.0,
        "specimens": [
            {k: r[k] for k in ("type", "pe_pct", "area_mm2", "x", "y", "dx", "dy")}
            for r in rows
        ],
        "area_source": "ECT/logs data_20260331_13* Keyence laser1*laser2 per specimen",
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
