#!/usr/bin/env python3
"""AD3 self-temperature channels vs the RN171 ambient sensor.

Question: the AD3 reports its own PCB and FPGA temperatures every row
(`ad3_pcb_temp_c`, `ad3_fpga_temp_c`). How well do they track the RN171
ambient reading (`temp_c`) that the current temperature correction uses,
and could they serve as a faster (or complementary) temperature input?

For each recorded run group this script reports:

1. Tracking: Pearson correlation of pcb/fpga vs RN171, raw and highpass
   (2 h rolling-mean removed, isolating HVAC-cycle content), plus the
   regression gain d(pcb)/d(RN171) at the best lag.
2. Lag: scan of the shift (predictor moved earlier/later) that maximizes
   R2 of (a) RN171 predicted from pcb/fpga and (b) signal magnitude
   predicted from each temperature channel. Positive lag = the predictor
   LEADS the target by that many minutes.
3. Explanatory power for the measurand: best-lag R2 of |ratio| (as % of
   median) against each temperature channel alone, and the incremental
   R2 of adding the pcb channel to the best-lag RN171 model.

Outputs: analysis/ad3_selftemp_vs_rn171_<date>.{png,json}
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ANALYSIS_DIR = Path(__file__).resolve().parent
LOG_DIR = ANALYSIS_DIR.parent / "logs"

RUN_GROUPS = {
    "drift24_20260710": [f"drift24_20260710_s{i:02d}" for i in range(1, 7)],
    "tempsweep_20260713": ["tempsweep_20260713_s01", "tempsweep_20260713_s03", "tempsweep_20260713_s04"],
    "airres_20260714": ["airres_20260714_s02"],
    "tempsweep_20260715": [f"tempsweep_20260715_s{i:02d}" for i in range(1, 5)],
}

GRID_S = 10.0
LAG_SCAN_MIN = 20.0
HIGHPASS_WIN_S = 2 * 3600.0

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
INK_2 = "#5f5e57"
GRID = "#e6e5e0"
C_RN = "#eda100"
C_PCB = "#2a78d6"
C_FPGA = "#1baf7a"
C_MAG = "#8a897f"


def load_stitched(run_ids: list[str]) -> pd.DataFrame:
    frames = []
    for rid in run_ids:
        path = LOG_DIR / f"ad3_log_{rid}.csv"
        if path.is_file():
            frames.append(pd.read_csv(path))
    if not frames:
        raise FileNotFoundError(f"no logs for {run_ids}")
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["timestamp"])
    df = df.assign(abs_s=(ts - ts.min()).dt.total_seconds()).sort_values("abs_s")
    ok = df[df["row_status"] == "ok"].dropna(
        subset=["magnitude", "temp_c", "ad3_pcb_temp_c", "ad3_fpga_temp_c"]
    )
    return ok.reset_index(drop=True)


def to_grid(df: pd.DataFrame) -> pd.DataFrame:
    t = df["abs_s"].to_numpy()
    grid = np.arange(t[0], t[-1], GRID_S)
    out = {"t": grid}
    for col in ["temp_c", "ad3_pcb_temp_c", "ad3_fpga_temp_c", "magnitude", "phase_deg"]:
        out[col] = np.interp(grid, t, df[col].to_numpy())
    g = pd.DataFrame(out)
    g["mag_pct"] = 100.0 * (g["magnitude"] / np.median(g["magnitude"]) - 1.0)
    return g


def highpass(x: np.ndarray) -> np.ndarray:
    win = max(3, int(round(HIGHPASS_WIN_S / GRID_S)))
    s = pd.Series(x)
    return (s - s.rolling(win, center=True, min_periods=win // 4).mean()).to_numpy()


def lag_scan(target: np.ndarray, pred: np.ndarray) -> dict:
    """Positive lag = predictor leads target (predictor shifted later matches)."""
    n = len(target)
    max_shift = int(round(LAG_SCAN_MIN * 60.0 / GRID_S))
    best = {"lag_min": 0.0, "r2": -np.inf, "slope": np.nan}
    curve = []
    for k in range(-max_shift, max_shift + 1):
        if n - abs(k) < 50:
            continue
        # predictor leads by k*GRID_S: pred value at t-k*dt explains target at t
        if k >= 0:
            y, x = target[k:], pred[: n - k]
        else:
            y, x = target[:n + k], pred[-k:]
        m = np.isfinite(y) & np.isfinite(x)
        if m.sum() < 50:
            continue
        y, x = y[m], x[m]
        if np.std(x) < 1e-12 or np.std(y) < 1e-12:
            continue
        slope, icpt = np.polyfit(x, y, 1)
        r = y - (slope * x + icpt)
        r2 = 1.0 - np.var(r) / np.var(y)
        curve.append((k * GRID_S / 60.0, r2))
        if r2 > best["r2"]:
            best = {"lag_min": k * GRID_S / 60.0, "r2": float(r2), "slope": float(slope)}
    best["curve"] = curve
    return best


def two_pred_r2(target: np.ndarray, p1: np.ndarray, lag1_min: float,
                p2: np.ndarray, lag2_min: float) -> float:
    n = len(target)
    k1 = int(round(lag1_min * 60.0 / GRID_S))
    k2 = int(round(lag2_min * 60.0 / GRID_S))
    idx = np.arange(n)
    i1, i2 = idx - k1, idx - k2
    m = (i1 >= 0) & (i1 < n) & (i2 >= 0) & (i2 < n)
    y = target[m]
    X = np.column_stack([p1[i1[m]], p2[i2[m]], np.ones(m.sum())])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ beta
    return float(1.0 - np.var(r) / np.var(y))


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 10:
        return float("nan")
    return float(np.corrcoef(a[m], b[m])[0, 1])


def analyze_group(name: str, run_ids: list[str]) -> dict:
    g = to_grid(load_stitched(run_ids))
    rn, pcb, fpga, mag = (g[c].to_numpy() for c in
                          ["temp_c", "ad3_pcb_temp_c", "ad3_fpga_temp_c", "mag_pct"])
    rn_hp, pcb_hp, fpga_hp, mag_hp = (highpass(x) for x in (rn, pcb, fpga, mag))

    res = {
        "runs": run_ids,
        "hours": float((g["t"].iloc[-1] - g["t"].iloc[0]) / 3600.0),
        "rows": int(len(g)),
        "spans_c": {
            "rn171": float(np.ptp(rn)),
            "pcb": float(np.ptp(pcb)),
            "fpga": float(np.ptp(fpga)),
        },
        "means_c": {
            "rn171": float(np.mean(rn)),
            "pcb": float(np.mean(pcb)),
            "fpga": float(np.mean(fpga)),
        },
        "corr_raw": {
            "pcb_vs_rn171": pearson(pcb, rn),
            "fpga_vs_rn171": pearson(fpga, rn),
            "fpga_vs_pcb": pearson(fpga, pcb),
        },
        "corr_highpass_2h": {
            "pcb_vs_rn171": pearson(pcb_hp, rn_hp),
            "fpga_vs_rn171": pearson(fpga_hp, rn_hp),
        },
    }

    trk_pcb = lag_scan(rn, pcb)
    trk_fpga = lag_scan(rn, fpga)
    res["tracking_rn171_from"] = {
        "pcb": {k: trk_pcb[k] for k in ("lag_min", "r2", "slope")},
        "fpga": {k: trk_fpga[k] for k in ("lag_min", "r2", "slope")},
    }

    sig = {}
    curves = {}
    for label, series in [("rn171", rn), ("pcb", pcb), ("fpga", fpga)]:
        sc = lag_scan(mag, series)
        curves[label] = sc.pop("curve")
        sig[label] = sc
    res["mag_fit"] = sig
    ph = g["phase_deg"].to_numpy()
    ph_hp = highpass(ph)
    res["phase_fit"] = {
        label: {k: v for k, v in lag_scan(ph, series).items() if k != "curve"}
        for label, series in [("rn171", rn), ("pcb", pcb), ("fpga", fpga)]
    }
    # cycle-band (2 h highpass) fits: deployment-relevant HVAC tracking with
    # slow drift/warm-up removed on both sides
    res["mag_fit_highpass"] = {
        label: {k: v for k, v in lag_scan(mag_hp, series).items() if k != "curve"}
        for label, series in [("rn171", rn_hp), ("pcb", pcb_hp), ("fpga", fpga_hp)]
    }
    res["phase_fit_highpass"] = {
        label: {k: v for k, v in lag_scan(ph_hp, series).items() if k != "curve"}
        for label, series in [("rn171", rn_hp), ("pcb", pcb_hp), ("fpga", fpga_hp)]
    }
    res["mag_fit_incremental"] = {
        "rn171_plus_pcb_r2": two_pred_r2(
            mag, rn, sig["rn171"]["lag_min"], pcb, sig["pcb"]["lag_min"]),
        "rn171_alone_r2": sig["rn171"]["r2"],
        "pcb_alone_r2": sig["pcb"]["r2"],
    }
    # highpass-domain lag between pcb and rn171 (cycle tracking, drift removed)
    res["tracking_highpass"] = {
        "pcb": {k: v for k, v in lag_scan(rn_hp, pcb_hp).items() if k != "curve"},
        "fpga": {k: v for k, v in lag_scan(rn_hp, fpga_hp).items() if k != "curve"},
    }
    return res, g, curves


def plot_group(axrow, name: str, g: pd.DataFrame, res: dict, curves: dict) -> None:
    t_h = (g["t"] - g["t"].iloc[0]) / 3600.0

    ax = axrow[0]
    for col, c, lab in [("temp_c", C_RN, "RN171"),
                        ("ad3_pcb_temp_c", C_PCB, "AD3 PCB"),
                        ("ad3_fpga_temp_c", C_FPGA, "AD3 FPGA")]:
        x = g[col].to_numpy()
        z = (x - x.mean()) / (x.std() if x.std() > 0 else 1.0)
        ax.plot(t_h, z, color=c, lw=0.8, label=lab)
    mz = g["mag_pct"].to_numpy()
    mz = (mz - mz.mean()) / (mz.std() if mz.std() > 0 else 1.0)
    ax.plot(t_h, mz, color=C_MAG, lw=0.6, alpha=0.6, label="|ratio|")
    ax.set_ylabel(f"{name}\nz-score")
    ax.set_xlabel("hours")
    if name == list(RUN_GROUPS)[0]:
        ax.legend(fontsize=7, ncol=4, loc="upper right")

    ax = axrow[1]
    ax.scatter(g["temp_c"], g["ad3_pcb_temp_c"], s=2, c=t_h, cmap="viridis", alpha=0.5)
    trk = res["tracking_rn171_from"]["pcb"]
    ax.set_xlabel("RN171 temp_c [C]")
    ax.set_ylabel("AD3 PCB [C]")
    ax.set_title(
        f"gain(d pcb/d rn)={1.0/trk['slope']:.2f}  R2={trk['r2']:.3f}  "
        f"lag={trk['lag_min']:+.1f} min", fontsize=8)

    ax = axrow[2]
    for label, c in [("rn171", C_RN), ("pcb", C_PCB), ("fpga", C_FPGA)]:
        cv = np.array(curves[label])
        ax.plot(cv[:, 0], cv[:, 1], color=c, lw=1.0, label=label)
        best = res["mag_fit"][label]
        ax.plot(best["lag_min"], best["r2"], "o", color=c, ms=4)
    ax.set_xlabel("predictor lead vs |ratio| [min]")
    ax.set_ylabel("R2 of |ratio| fit")
    ax.set_ylim(bottom=0)
    if name == list(RUN_GROUPS)[0]:
        ax.legend(fontsize=7)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="20260721")
    args = parser.parse_args()

    results = {}
    fig, axes = plt.subplots(len(RUN_GROUPS), 3, figsize=(13, 3.0 * len(RUN_GROUPS)))
    fig.patch.set_facecolor(SURFACE)
    for axrow in axes:
        for ax in axrow:
            ax.set_facecolor(SURFACE)
            ax.grid(color=GRID, lw=0.5)
            ax.tick_params(labelsize=7, colors=INK_2)
            for s in ax.spines.values():
                s.set_color(GRID)

    for row, (name, run_ids) in enumerate(RUN_GROUPS.items()):
        res, g, curves = analyze_group(name, run_ids)
        results[name] = res
        plot_group(axes[row], name, g, res, curves)
        print(f"[{name}] {res['hours']:.1f} h, {res['rows']} rows")
        print(f"  corr raw   pcb~rn={res['corr_raw']['pcb_vs_rn171']:+.3f} "
              f"fpga~rn={res['corr_raw']['fpga_vs_rn171']:+.3f}")
        print(f"  corr hp2h  pcb~rn={res['corr_highpass_2h']['pcb_vs_rn171']:+.3f} "
              f"fpga~rn={res['corr_highpass_2h']['fpga_vs_rn171']:+.3f}")
        print(f"  rn171 from pcb: R2={res['tracking_rn171_from']['pcb']['r2']:.3f} "
              f"lag={res['tracking_rn171_from']['pcb']['lag_min']:+.1f} min")
        mf = res["mag_fit"]
        print("  |ratio| fit R2 (lag min): " + "  ".join(
            f"{k}={v['r2']:.3f} ({v['lag_min']:+.1f})" for k, v in mf.items()))
        inc = res["mag_fit_incremental"]
        print(f"  rn171+pcb R2={inc['rn171_plus_pcb_r2']:.3f} "
              f"(rn alone {inc['rn171_alone_r2']:.3f})")

    fig.suptitle("AD3 self-temperature (PCB/FPGA) vs RN171 ambient", fontsize=11, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    png = ANALYSIS_DIR / f"ad3_selftemp_vs_rn171_{args.tag}.png"
    fig.savefig(png, dpi=140)
    out = ANALYSIS_DIR / f"ad3_selftemp_vs_rn171_{args.tag}.json"
    out.write_text(json.dumps({"grid_s": GRID_S, "lag_scan_min": LAG_SCAN_MIN,
                               "highpass_win_s": HIGHPASS_WIN_S,
                               "lag_convention": "positive = predictor leads target",
                               "groups": results}, indent=2))
    print(f"wrote {png.name}, {out.name}")


if __name__ == "__main__":
    main()
