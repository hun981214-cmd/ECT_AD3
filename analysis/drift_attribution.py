#!/usr/bin/env python3
"""Drift attribution for ratio-mode AD3 logs: pure drift vs environment.

Given a long no-specimen run that logs the complex ratio together with ambient
temperature/humidity (RN171) and the AD3 PCB temperature, this decomposes the
observed low-frequency variation of the signal into:

- variance uniquely explained by the environment channels,
- variance uniquely explained by elapsed time (the "pure drift" proxy:
  aging/settling that no logged environment channel tracks),
- variance shared between the two (environment itself trends with time, so a
  monotonic warm-up cannot be attributed either way from one run), and
- unexplained residual (noise + unmodeled effects).

Method: commonality analysis over three OLS fits per target (env-only,
time-only, env+time). Per-environment-channel attribution uses drop-one
unique delta-R2 inside the env model. Cross-correlation of detrended series
scans for thermal lag between each environment channel and the signal.

Caveats printed with the verdict:
- If predictors are strongly collinear (|r| > 0.8) the per-channel split is
  unreliable; the env-vs-time commonality split remains valid.
- If an environment channel barely moves during the run, its attribution is
  low-confidence no matter what the regression says.
- A run shorter than a few hours mostly measures warm-up; expect a large
  shared component until at least one diurnal cycle is captured.
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

ENV_COLUMNS = ["temp_c", "humid_pct", "ad3_pcb_temp_c"]

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
INK_2 = "#5f5e57"
GRID = "#e6e5e0"
C_RAW = "#2a78d6"
C_CORRECTED = "#1baf7a"
C_ENV = {"temp_c": "#eda100", "humid_pct": "#4a3aa7", "ad3_pcb_temp_c": "#e34948"}
C_NEUTRAL = "#8a897f"


def r_squared(y: np.ndarray, X: np.ndarray) -> tuple[float, np.ndarray]:
    """OLS R^2 of y on X (X excludes the intercept; it is added here)."""
    A = np.column_stack([np.ones(len(y)), X]) if X.size else np.ones((len(y), 1))
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return r2, resid


def commonality(y: np.ndarray, env: np.ndarray, t: np.ndarray) -> dict[str, float]:
    """Unclamped commonality components; they sum to 1 exactly.

    ``shared`` can legitimately be negative (suppression: env and time carry
    opposing information); clamping it would break the identity
    unique_env + shared + unique_time + residual = 1 and hide the effect.
    """
    r2_env, _ = r_squared(y, env)
    r2_time, _ = r_squared(y, t.reshape(-1, 1))
    r2_full, resid = r_squared(y, np.column_stack([env, t]))
    return {
        "r2_env": r2_env,
        "r2_time": r2_time,
        "r2_full": r2_full,
        "unique_env": r2_full - r2_time,
        "unique_time": r2_full - r2_env,
        "shared": r2_env + r2_time - r2_full,
        "residual": 1.0 - r2_full,
        "resid_series": resid,
    }


def drop_one_unique(y: np.ndarray, env: np.ndarray, names: list[str]) -> dict[str, float]:
    r2_all, _ = r_squared(y, env)
    out = {}
    for i, name in enumerate(names):
        reduced = np.delete(env, i, axis=1)
        r2_wo, _ = r_squared(y, reduced)
        out[name] = max(0.0, r2_all - r2_wo)
    return out


def best_lag_minutes(sig: np.ndarray, env: np.ndarray, t_s: np.ndarray, max_lag_min: float) -> tuple[float, float]:
    """Peak |cross-correlation| lag (minutes; positive = env leads signal)."""
    dt = float(np.median(np.diff(t_s)))
    if not math.isfinite(dt) or dt <= 0:
        return float("nan"), float("nan")
    max_shift = int(max_lag_min * 60.0 / dt)
    if max_shift < 1 or len(sig) < 3 * max_shift:
        return float("nan"), float("nan")

    def detrend(v):
        coef = np.polyfit(t_s, v, 1)
        return v - np.polyval(coef, t_s)

    s = detrend(sig)
    e = detrend(env)
    best = (0.0, 0)
    for shift in range(-max_shift, max_shift + 1):
        if shift >= 0:
            a, b = s[shift:], e[: len(e) - shift]
        else:
            a, b = s[:shift], e[-shift:]
        if len(a) < 10 or a.std() < 1e-15 or b.std() < 1e-15:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if abs(c) > abs(best[0]):
            best = (c, shift)
    return best[1] * dt / 60.0, best[0]


def classify(unique_env: float, unique_time: float, shared: float, explained: float) -> str:
    if explained < 0.3:
        return "noise-dominated (little low-frequency drift to attribute)"
    if shared < -0.05:
        return "suppression detected: env and time carry opposing information (inspect per-channel fits)"
    if shared > 0.5 * explained:
        return "ambiguous: environment and time trend together (need a longer run with diurnal cycles)"
    if unique_env > 0.6 * explained:
        return "environment-driven"
    if unique_time > 0.6 * explained:
        return "pure sensor/electronics drift"
    return "mixed environment + pure drift"


def style_axis(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.tick_params(colors=INK_2, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.xaxis.label.set_color(INK_2)
    ax.yaxis.label.set_color(INK_2)
    ax.title.set_color(INK)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_id",
        help=(
            "run id (ad3_log_<id>.csv), a full CSV path, or a comma-separated "
            "list of run ids to stitch (watchdog segments); stitched elapsed "
            "time is rebuilt from the timestamp column"
        ),
    )
    parser.add_argument("--log-dir", default=str(LOG_DIR))
    parser.add_argument("--last-hours", type=float, default=0.0, help="analyze only the last N hours (0 = all)")
    parser.add_argument("--skip-first-min", type=float, default=0.0, help="drop the first N minutes (placement/warm-up)")
    parser.add_argument("--max-lag-min", type=float, default=30.0)
    parser.add_argument("--out-prefix", default=None)
    args = parser.parse_args()

    run_ids = [r.strip() for r in args.run_id.split(",") if r.strip()]
    frames = []
    for rid in run_ids:
        path = Path(rid)
        if not path.is_file():
            path = Path(args.log_dir) / f"ad3_log_{rid}.csv"
        frames.append(pd.read_csv(path))
    if len(frames) > 1:
        df = pd.concat(frames, ignore_index=True)
        ts = pd.to_datetime(df["timestamp"])
        df = df.assign(time_s=(ts - ts.iloc[0]).dt.total_seconds()).sort_values("time_s")
        path = Path(args.log_dir) / f"ad3_log_{run_ids[0]}_stitched{len(frames)}.csv"
    else:
        df = frames[0]
    ok = df[df["row_status"] == "ok"].copy()
    ok = ok.dropna(subset=["raw_x", "raw_y"])

    env_cols = [c for c in ENV_COLUMNS if c in ok and ok[c].notna().mean() > 0.8]
    if not env_cols:
        raise SystemExit("no usable environment columns in this run")
    ok = ok.dropna(subset=env_cols)

    t_s = ok["time_s"].astype(float).to_numpy()
    if args.skip_first_min > 0:
        keep = t_s >= t_s[0] + args.skip_first_min * 60.0
        ok, t_s = ok[keep], t_s[keep]
    if args.last_hours > 0:
        keep = t_s >= t_s[-1] - args.last_hours * 3600.0
        ok, t_s = ok[keep], t_s[keep]
    if len(ok) < 60:
        raise SystemExit(f"only {len(ok)} usable rows; need at least 60 for attribution")

    x = ok["raw_x"].astype(float).to_numpy()
    y = ok["raw_y"].astype(float).to_numpy()
    mag = np.hypot(x, y)
    phase = np.degrees(np.unwrap(np.radians(ok["phase_deg"].astype(float).to_numpy())))
    duration_h = (t_s[-1] - t_s[0]) / 3600.0

    env_raw = np.column_stack([ok[c].astype(float).to_numpy() for c in env_cols])
    env_std = (env_raw - env_raw.mean(axis=0)) / np.where(env_raw.std(axis=0) > 1e-12, env_raw.std(axis=0), 1.0)
    t_std = (t_s - t_s.mean()) / t_s.std()

    print(f"run: {path.name}  rows={len(ok)}  duration={duration_h:.2f} h")
    print("env ranges: " + "  ".join(
        f"{c}={env_raw[:, i].min():.2f}..{env_raw[:, i].max():.2f}" for i, c in enumerate(env_cols)
    ))

    corr = np.corrcoef(np.column_stack([env_std, t_std]).T)
    names = env_cols + ["time"]
    collinear_pairs = [
        (names[i], names[j], corr[i, j])
        for i in range(len(names))
        for j in range(i + 1, len(names))
        if abs(corr[i, j]) > 0.8
    ]

    results = {}
    for target_name, target in (("magnitude", mag), ("phase_deg", phase), ("raw_x", x), ("raw_y", y)):
        com = commonality(target, env_std, t_std)
        per_env = drop_one_unique(target, env_std, env_cols)
        resid = com.pop("resid_series")
        raw_p95 = float(np.percentile(target, 95) - np.percentile(target, 5))
        resid_p95 = float(np.percentile(resid, 95) - np.percentile(resid, 5))
        # env-only correction (what a raw-space env correction could remove)
        _, env_resid = r_squared(target, env_std)
        env_corr_p95 = float(np.percentile(env_resid, 95) - np.percentile(env_resid, 5))
        results[target_name] = {
            **{k: float(v) for k, v in com.items()},
            "per_env_unique_dr2": {k: float(v) for k, v in per_env.items()},
            "raw_p95_p05": raw_p95,
            "env_corrected_p95_p05": env_corr_p95,
            "full_residual_p95_p05": resid_p95,
        }

    lags = {}
    for i, c in enumerate(env_cols):
        lag_min, lag_corr = best_lag_minutes(mag, env_raw[:, i], t_s, args.max_lag_min)
        lags[c] = {"lag_min": lag_min, "corr_at_lag": lag_corr}

    m = results["magnitude"]
    explained = m["r2_full"]
    verdict = classify(m["unique_env"], m["unique_time"], m["shared"], explained)

    print("\n=== variance decomposition (share of total variance) ===")
    print(f"{'target':>10} {'unique_env':>11} {'shared':>8} {'unique_time':>12} {'residual':>9}")
    for name, r in results.items():
        print(f"{name:>10} {r['unique_env']:>11.3f} {r['shared']:>8.3f} {r['unique_time']:>12.3f} {r['residual']:>9.3f}")
    print("\nper-env unique dR2 on magnitude: " + "  ".join(
        f"{k}={v:.3f}" for k, v in m["per_env_unique_dr2"].items()))
    print("thermal lag scan vs magnitude: " + "  ".join(
        f"{c}: {v['lag_min']:+.1f} min (r={v['corr_at_lag']:+.2f})" for c, v in lags.items()))
    print(f"\nmagnitude p95-p05: raw={m['raw_p95_p05']/np.mean(mag)*100:.4f}%  "
          f"env-corrected={m['env_corrected_p95_p05']/np.mean(mag)*100:.4f}%  "
          f"env+time-corrected={m['full_residual_p95_p05']/np.mean(mag)*100:.4f}%")
    print(f"\nVERDICT (magnitude): {verdict}")
    print(f"  explained {explained*100:.0f}% of variance: "
          f"env-unique {m['unique_env']*100:.0f}%p, shared {m['shared']*100:.0f}%p, "
          f"time-unique {m['unique_time']*100:.0f}%p")
    for a, b, c in collinear_pairs:
        print(f"  [caution] {a} and {b} are collinear (r={c:+.2f}); their split is unreliable")
    for i, c in enumerate(env_cols):
        if env_raw[:, i].max() - env_raw[:, i].min() < {"temp_c": 0.5, "humid_pct": 2.0, "ad3_pcb_temp_c": 0.5}.get(c, 0.0):
            print(f"  [caution] {c} moved little this run; its attribution is low-confidence")
    if duration_h < 6:
        print(f"  [caution] {duration_h:.1f} h is short; expect warm-up to dominate until diurnal cycles accumulate")

    # ---- figure ------------------------------------------------------------
    fig = plt.figure(figsize=(14, 9.5), dpi=140)
    fig.patch.set_facecolor(SURFACE)
    n_env = len(env_cols)
    gs = fig.add_gridspec(2 + n_env, 2, width_ratios=[2.2, 1.0], hspace=0.65, wspace=0.25)
    t_h = (t_s - t_s[0]) / 3600.0

    ax = fig.add_subplot(gs[0:2, 0])
    style_axis(ax)
    rel = (mag / mag.mean() - 1.0) * 100
    _, env_resid = r_squared(mag, env_std)
    rel_corr = env_resid / mag.mean() * 100
    ax.plot(t_h, rel, color=C_RAW, linewidth=1.4)
    ax.plot(t_h, rel_corr - rel_corr.mean() + rel.mean(), color=C_CORRECTED, linewidth=1.4)
    ax.annotate("raw", (t_h[-1], rel[-1]), textcoords="offset points", xytext=(8, 10),
                fontsize=9, color=C_RAW, fontweight="bold")
    ax.annotate("env-corrected", (t_h[-1], (rel_corr - rel_corr.mean() + rel.mean())[-1]),
                textcoords="offset points", xytext=(8, -16), fontsize=9,
                color=C_CORRECTED, fontweight="bold")
    ax.set_xlabel("elapsed (h)")
    ax.set_ylabel("|ratio| deviation (%)")
    ax.set_title(f"|ratio| drift, raw vs env-corrected - {path.stem}")

    for i, c in enumerate(env_cols):
        ax = fig.add_subplot(gs[2 + i, 0])
        style_axis(ax)
        ax.plot(t_h, env_raw[:, i], color=C_ENV.get(c, C_NEUTRAL), linewidth=1.2)
        ax.set_ylabel(c, fontsize=8)
        if i == len(env_cols) - 1:
            ax.set_xlabel("elapsed (h)")

    ax = fig.add_subplot(gs[0:2, 1])
    style_axis(ax)
    parts = [("env-unique", m["unique_env"], C_CORRECTED),
             ("shared", m["shared"], C_NEUTRAL),
             ("time-unique", m["unique_time"], C_RAW),
             ("residual", m["residual"], GRID)]
    bottom = 0.0
    for label, value, color in parts:
        shown = max(0.0, value)  # display only; printed/JSON values stay signed
        ax.bar([0], [shown], bottom=[bottom], color=color, width=0.5,
               edgecolor=SURFACE, linewidth=1.5)
        if shown > 0.03:
            ax.text(0, bottom + shown / 2, f"{label}\n{value*100:.0f}%",
                    ha="center", va="center", fontsize=9, color=INK)
        bottom += shown
    if m["shared"] < -0.03:
        ax.text(0, -0.06, f"suppression: shared = {m['shared']*100:.0f}%",
                ha="center", va="top", fontsize=8, color=INK_2)
    ax.set_xlim(-0.7, 0.7)
    ax.set_xticks([])
    ax.set_ylabel("share of |ratio| variance")
    ax.set_title("attribution")

    ax = fig.add_subplot(gs[2:, 1])
    style_axis(ax)
    top_env = max(m["per_env_unique_dr2"], key=m["per_env_unique_dr2"].get)
    idx = env_cols.index(top_env)
    ax.scatter(env_raw[:, idx], rel, s=6, color=C_RAW, alpha=0.4)
    coef = np.polyfit(env_raw[:, idx], rel, 1)
    xs = np.linspace(env_raw[:, idx].min(), env_raw[:, idx].max(), 50)
    ax.plot(xs, np.polyval(coef, xs), color=INK_2, linewidth=1.4, linestyle=(0, (4, 3)))
    ax.set_xlabel(top_env)
    ax.set_ylabel("|ratio| deviation (%)")
    ax.set_title(f"strongest env channel: {top_env}")

    prefix = args.out_prefix or str(ANALYSIS_DIR / f"drift_attribution_{path.stem.replace('ad3_log_', '')}")
    fig.savefig(f"{prefix}.png", facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)

    summary = {
        "run": path.name,
        "rows": int(len(ok)),
        "duration_h": duration_h,
        "env_columns": env_cols,
        "results": results,
        "lags_vs_magnitude": lags,
        "collinear_pairs": [[a, b, float(c)] for a, b, c in collinear_pairs],
        "verdict_magnitude": verdict,
    }
    Path(f"{prefix}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nwrote {prefix}.png")
    print(f"wrote {prefix}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
