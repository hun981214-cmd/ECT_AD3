#!/usr/bin/env python3
"""Specimen sensitivity analysis for the 2026-07-10 ratiometric probe runs.

Reads the no-specimen baseline plus the eight specimen runs (two wire types x
0/10/20/30 % plastic strain), reports per-specimen complex-ratio means,
per-type PE sensitivity, and specimen separability relative to measurement
noise. Optionally compares discriminability against the EW-8SCT 2026-03-31
8-specimen calibration logs.

Outputs a summary table to stdout, a JSON summary, and a three-panel figure
under ECTv2/analysis/.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ANALYSIS_DIR = Path(__file__).resolve().parent
LOG_DIR = ANALYSIS_DIR.parent / "logs"
TEMP_MODEL_PATH = ANALYSIS_DIR / "temp_response_20260713.json"
REF_TEMP_C = 27.0


def temp_corrector():
    """x/y correction to REF_TEMP_C; identity if the temp model is missing.

    The runtime (specimen_check.py) compares temperature-corrected deltas, so
    the stored reference must be corrected the same way - otherwise ambient
    drift during the reference sweep contaminates the trajectories.
    """
    try:
        model = json.loads(TEMP_MODEL_PATH.read_text())["results"]
    except Exception:
        return lambda x, y, t: (x, y)
    cx1 = model["raw_x"]["lag_quadratic"]["temp_coef"]
    cx2 = model["raw_x"]["lag_quadratic"]["temp2_coef"]
    cy1 = model["raw_y"]["lag_quadratic"]["temp_coef"]
    cy2 = model["raw_y"]["lag_quadratic"]["temp2_coef"]

    def correct(x, y, t):
        import math as _math
        if not _math.isfinite(t):
            return x, y
        t0 = REF_TEMP_C
        return (x - (cx1 * (t - t0) + cx2 * (t * t - t0 * t0)),
                y - (cy1 * (t - t0) + cy2 * (t * t - t0 * t0)))
    return correct


CORRECT = temp_corrector()

BASELINE_RUN = "ratio_probe_20260710_s01"
SPECIMEN_RUNS = {
    ("type1", 0): "refsweep_20260710_t1_pe00",
    ("type1", 10): "refsweep_20260710_t1_pe10",
    ("type1", 20): "refsweep_20260710_t1_pe20_r2",
    ("type1", 30): "refsweep_20260710_t1_pe30",
    ("type2", 0): "refsweep_20260710_t2_pe00",
    ("type2", 10): "refsweep_20260710_t2_pe10",
    ("type2", 20): "refsweep_20260710_t2_pe20",
    ("type2", 30): "refsweep_20260710_t2_pe30",
}

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
INK_2 = "#5f5e57"
GRID = "#e6e5e0"
TYPE_COLOR = {"type1": "#2a78d6", "type2": "#1baf7a"}
BASE_COLOR = "#8a897f"


def load_run(run_id: str) -> dict[str, float]:
    df = pd.read_csv(LOG_DIR / f"ad3_log_{run_id}.csv")
    ok = df[df["row_status"] == "ok"]
    basis = "all_ok"
    if "is_stable" in ok and int(ok["is_stable"].sum()) >= 5:
        ok = ok[ok["is_stable"] == 1]
        basis = "stable"
    x_raw = ok["raw_x"].astype(float)
    y_raw = ok["raw_y"].astype(float)
    # Means use temperature-corrected values (the runtime compares corrected
    # deltas); noise statistics use raw rows so RN171 reading noise does not
    # inflate them through the per-row correction.
    x, y = x_raw, y_raw
    if "temp_c" in ok and ok["temp_c"].notna().mean() > 0.8:
        cx, cy = zip(*(CORRECT(float(a), float(b), float(t))
                       for a, b, t in zip(x_raw, y_raw, ok["temp_c"].astype(float))))
        x = pd.Series(cx, index=ok.index)
        y = pd.Series(cy, index=ok.index)
    mag = np.hypot(x, y)
    # sigma_window (std over the run) includes any settling/thermal drift
    # inside the window; sigma_step (first-difference based) is drift-free
    # and estimates per-row measurement noise. Separability uses both.
    sigma_window = float(np.hypot(x_raw.std(ddof=1), y_raw.std(ddof=1)))
    if len(x_raw) >= 3:
        sigma_step = float(
            np.hypot(np.diff(x_raw).std(ddof=1), np.diff(y_raw).std(ddof=1)) / math.sqrt(2)
        )
    else:
        sigma_step = sigma_window
    return {
        "run_id": run_id,
        "n": int(len(ok)),
        "basis": basis,
        "x": float(x.mean()),
        "y": float(y.mean()),
        "x_std": float(x_raw.std(ddof=1)),
        "y_std": float(y_raw.std(ddof=1)),
        "magnitude": float(mag.mean()),
        "phase_deg": float(math.degrees(math.atan2(y.mean(), x.mean()))),
        "sigma": sigma_window,
        "sigma_step": sigma_step,
    }


def load_ew_specimens(ew_dir: Path) -> list[dict[str, float]]:
    # The 2026-03-31 8-specimen calibration set is the first eight files;
    # they start with `#` comment lines before the CSV header.
    files = sorted(ew_dir.glob("data_20260331_13*.csv"))[:8]
    out = []
    for i, path in enumerate(files):
        try:
            df = pd.read_csv(path, comment="#")
        except Exception as exc:
            print(f"[warn] {path.name}: unreadable ({exc})")
            continue
        cols = {c.lower(): c for c in df.columns}
        xc = cols.get("ect_x") or cols.get("x")
        yc = cols.get("ect_y") or cols.get("y")
        if xc is None or yc is None:
            print(f"[warn] {path.name}: no ect_x/ect_y columns ({list(df.columns)[:8]}...)")
            continue
        x = df[xc].astype(float)
        y = df[yc].astype(float)
        out.append(
            {
                "file": path.name,
                "index": i,
                "x": float(x.mean()),
                "y": float(y.mean()),
                "sigma": float(np.hypot(x.std(ddof=1), y.std(ddof=1))),
                "sigma_step": float(
                    np.hypot(np.diff(x).std(ddof=1), np.diff(y).std(ddof=1)) / math.sqrt(2)
                ) if len(x) >= 3 else float("nan"),
                "n": int(len(df)),
            }
        )
    return out


def separability(points: list[tuple[float, float]], sigmas: list[float]) -> tuple[float, float]:
    """Minimum pairwise distance and its ratio to the worst per-run sigma."""
    dmin = float("inf")
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
            dmin = min(dmin, d)
    worst_sigma = max(s for s in sigmas if math.isfinite(s))
    return dmin, dmin / worst_sigma if worst_sigma > 0 else float("nan")


def style_axis(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.tick_params(colors=INK_2, labelsize=9)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)
    ax.title.set_color(INK)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ew-dir", default=str(ANALYSIS_DIR.parents[1] / "ECT" / "logs"))
    parser.add_argument("--baseline-run", default=BASELINE_RUN)
    parser.add_argument("--run-prefix", default=None,
                        help="use <prefix>_t{1,2}_pe{00,10,20,30} run ids instead of the 2026-07-10 set")
    parser.add_argument("--out-prefix", default=str(ANALYSIS_DIR / "ratio_probe_specimens_20260710"))
    args = parser.parse_args()

    runs = SPECIMEN_RUNS
    if args.run_prefix:
        runs = {(f"type{t}", pe): f"{args.run_prefix}_t{t}_pe{pe:02d}"
                for t in (1, 2) for pe in (0, 10, 20, 30)}
    baseline = load_run(args.baseline_run)
    rows = []
    for (spec_type, pe), run_id in runs.items():
        r = load_run(run_id)
        r["type"] = spec_type
        r["pe_pct"] = pe
        r["dx"] = r["x"] - baseline["x"]
        r["dy"] = r["y"] - baseline["y"]
        r["delta_mag"] = math.hypot(r["dx"], r["dy"])
        rows.append(r)

    print(f"baseline {baseline['run_id']}: x={baseline['x']:+.6f} y={baseline['y']:+.6f} "
          f"|r|={baseline['magnitude']:.6f} sigma_win={baseline['sigma']:.2e} "
          f"sigma_step={baseline['sigma_step']:.2e}")
    print(f"{'type':>6} {'PE%':>4} {'x':>10} {'y':>10} {'|ratio|':>9} {'phase':>8} "
          f"{'|d|_from_base':>13} {'sig_win':>9} {'sig_step':>9} {'n':>3} {'basis':>7}")
    for r in rows:
        print(
            f"{r['type']:>6} {r['pe_pct']:>4} {r['x']:>10.6f} {r['y']:>10.6f} "
            f"{r['magnitude']:>9.6f} {r['phase_deg']:>8.3f} {r['delta_mag']:>13.6f} "
            f"{r['sigma']:>9.2e} {r['sigma_step']:>9.2e} {r['n']:>3} {r['basis']:>7}"
        )

    # Per-type PE sensitivity: complex-plane step per 10 % PE.
    sens = {}
    for spec_type in ("type1", "type2"):
        seq = sorted((r for r in rows if r["type"] == spec_type), key=lambda r: r["pe_pct"])
        steps = [
            math.hypot(b["x"] - a["x"], b["y"] - a["y"])
            for a, b in zip(seq, seq[1:])
        ]
        pes = np.array([r["pe_pct"] for r in seq], dtype=float)
        mags = np.array([r["magnitude"] for r in seq], dtype=float)
        slope = float(np.polyfit(pes, mags, 1)[0])
        sens[spec_type] = {
            "complex_step_per_10pct": [float(s) for s in steps],
            "mean_complex_step_per_10pct": float(np.mean(steps)),
            "mag_slope_per_pct": slope,
            "monotonic_mag": bool(np.all(np.diff(mags) > 0)),
        }
        worst_sigma = max(r["sigma"] for r in seq)
        worst_sigma_step = max(r["sigma_step"] for r in seq)
        print(
            f"{spec_type}: mean complex step per 10% PE = {np.mean(steps):.6f} "
            f"({np.mean(steps)/baseline['magnitude']*100:.2f}% of baseline |r|), "
            f"step/sigma_win = {np.mean(steps)/worst_sigma:.0f}, "
            f"step/sigma_step = {np.mean(steps)/worst_sigma_step:.0f}, "
            f"monotonic={sens[spec_type]['monotonic_mag']}"
        )

    pts = [(r["x"], r["y"]) for r in rows]
    dmin, ratio_win = separability(pts, [r["sigma"] for r in rows])
    _, ratio_step = separability(pts, [r["sigma_step"] for r in rows])
    print(f"AD3 ratio probe: min pairwise specimen distance = {dmin:.6f} "
          f"({dmin/baseline['magnitude']*100:.2f}% of |r|), "
          f"min-distance/worst-sigma_win = {ratio_win:.0f} "
          f"(incl. in-window settling drift), min-distance/worst-sigma_step = {ratio_step:.0f} "
          f"(per-row noise)")

    ew_summary = None
    ew_dir = Path(args.ew_dir)
    if ew_dir.is_dir():
        ew = load_ew_specimens(ew_dir)
        if len(ew) >= 4:
            ew_pts = [(e["x"], e["y"]) for e in ew]
            ew_dmin, ew_ratio_win = separability(ew_pts, [e["sigma"] for e in ew])
            _, ew_ratio_step = separability(ew_pts, [e["sigma_step"] for e in ew])
            ew_summary = {
                "n_specimens": len(ew),
                "min_distance": ew_dmin,
                "min_distance_over_sigma_window": ew_ratio_win,
                "min_distance_over_sigma_step": ew_ratio_step,
            }
            print(f"EW-8SCT 2026-03-31 ({len(ew)} specimens): min pairwise distance = {ew_dmin:.1f} counts, "
                  f"min-distance/worst-sigma_win = {ew_ratio_win:.0f}, "
                  f"min-distance/worst-sigma_step = {ew_ratio_step:.0f}")

    # ---- figure -----------------------------------------------------------
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), dpi=150)
    fig.patch.set_facecolor(SURFACE)

    ax = axes[0]
    style_axis(ax)
    ax.scatter([baseline["x"]], [baseline["y"]], s=70, color=BASE_COLOR, zorder=3)
    ax.annotate("no specimen", (baseline["x"], baseline["y"]),
                textcoords="offset points", xytext=(8, -12), fontsize=9, color=INK_2)
    ax.margins(0.14)
    label_offset = {"type1": (-30, -3), "type2": (8, 4)}
    for spec_type in ("type1", "type2"):
        seq = sorted((r for r in rows if r["type"] == spec_type), key=lambda r: r["pe_pct"])
        xs = [r["x"] for r in seq]
        ys = [r["y"] for r in seq]
        color = TYPE_COLOR[spec_type]
        ax.plot(xs, ys, color=color, linewidth=2, alpha=0.85, zorder=2)
        ax.scatter(xs, ys, s=64, color=color, zorder=3, edgecolors=SURFACE, linewidths=1.5)
        for r in seq:
            ax.annotate(f"{r['pe_pct']}%", (r["x"], r["y"]),
                        textcoords="offset points", xytext=label_offset[spec_type],
                        fontsize=9, color=INK)
    seq1 = sorted((r for r in rows if r["type"] == "type1"), key=lambda r: r["pe_pct"])
    ax.annotate("type1", (seq1[0]["x"], seq1[0]["y"]), textcoords="offset points",
                xytext=(10, -4), fontsize=10, color=TYPE_COLOR["type1"], fontweight="bold")
    seq2 = sorted((r for r in rows if r["type"] == "type2"), key=lambda r: r["pe_pct"])
    ax.annotate("type2", (seq2[-1]["x"], seq2[-1]["y"]), textcoords="offset points",
                xytext=(8, -16), fontsize=10, color=TYPE_COLOR["type2"], fontweight="bold")
    ax.set_xlabel("ratio x (pickup/drive)")
    ax.set_ylabel("ratio y (pickup/drive)")
    ax.set_title("Complex ratio plane: baseline + 2 types x 4 PE levels")

    ax = axes[1]
    style_axis(ax)
    for spec_type in ("type1", "type2"):
        seq = sorted((r for r in rows if r["type"] == spec_type), key=lambda r: r["pe_pct"])
        pes = [r["pe_pct"] for r in seq]
        mags = [r["magnitude"] for r in seq]
        color = TYPE_COLOR[spec_type]
        ax.plot(pes, mags, color=color, linewidth=2, marker="o", markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=1.5)
        ax.annotate(spec_type, (pes[-1], mags[-1]), textcoords="offset points",
                    xytext=(6, 6), fontsize=10, color=color, fontweight="bold")
    ax.axhline(baseline["magnitude"], color=BASE_COLOR, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.annotate("no specimen", (0, baseline["magnitude"]), textcoords="offset points",
                xytext=(2, 5), fontsize=9, color=INK_2)
    ax.set_xlabel("plastic strain (%)")
    ax.set_ylabel("|ratio|")
    ax.set_title("|ratio| vs plastic strain")
    ax.set_xticks([0, 10, 20, 30])

    ax = axes[2]
    style_axis(ax)
    for spec_type in ("type1", "type2"):
        seq = sorted((r for r in rows if r["type"] == spec_type), key=lambda r: r["pe_pct"])
        pes = [r["pe_pct"] for r in seq]
        phases = [r["phase_deg"] for r in seq]
        color = TYPE_COLOR[spec_type]
        ax.plot(pes, phases, color=color, linewidth=2, marker="o", markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=1.5)
        ax.annotate(spec_type, (pes[-1], phases[-1]), textcoords="offset points",
                    xytext=(6, 6), fontsize=10, color=color, fontweight="bold")
    ax.axhline(baseline["phase_deg"], color=BASE_COLOR, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.annotate("no specimen", (0, baseline["phase_deg"]), textcoords="offset points",
                xytext=(2, 5), fontsize=9, color=INK_2)
    ax.set_xlabel("plastic strain (%)")
    ax.set_ylabel("ratio phase (deg)")
    ax.set_title("Phase vs plastic strain")
    ax.set_xticks([0, 10, 20, 30])

    fig.suptitle("AD3 ratiometric probe, 120 kHz twin-drive - specimen response (2026-07-10)",
                 color=INK, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out_png = Path(f"{args.out_prefix}.png")
    fig.savefig(out_png, facecolor=SURFACE)
    plt.close(fig)

    summary = {
        "baseline": baseline,
        "specimens": rows,
        "sensitivity": sens,
        "ad3_min_pairwise_distance": dmin,
        "ad3_min_distance_over_sigma_window": ratio_win,
        "ad3_min_distance_over_sigma_step": ratio_step,
        "ew8sct_20260331": ew_summary,
    }
    out_json = Path(f"{args.out_prefix}.json")
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"wrote {out_png}")
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
