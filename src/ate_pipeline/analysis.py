"""Cross-lot analysis on top of the Phase 1 tables: data checks, bin signatures,
robust capability, wafer maps and a spatial yield regression.

Every number in docs/FINDINGS.md comes from this module.

Usage:  python -m ate_pipeline.analysis data/raw/lot2.stdf data/raw/lot3.stdf --figures docs/figures
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from ate_pipeline.ingest import parse_stdf
from ate_pipeline.report import cpk_by_test, die_yield, yield_summary

EDGE_R = 0.9  # normalised radius where the edge ring starts


# --- loading and data checks ------------------------------------------------

def fingerprint(tables: dict[str, pd.DataFrame]) -> str:
    """Hash of the measured content (bins, X/Y, results), ignoring lot/part labels.

    Two files with the same fingerprint are the same wafer under different
    names: in the demo data, demofile.stdf is lot3.stdf relabelled.
    """
    h = hashlib.sha256()
    h.update(tables["parts"][["hard_bin", "soft_bin", "x", "y"]].to_numpy().tobytes())
    h.update(tables["test_results"][["test_num", "result"]].to_numpy(dtype=float).tobytes())
    return h.hexdigest()


def load_lots(paths: list[str | Path]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Parse several STDF files into one parts and one results table, skipping duplicates.

    Returns (parts, results, skipped). Both tables get a `lot` column (file stem)
    and results get `x`, `y`, `hard_bin` from their part.
    """
    parts, results, seen, skipped = [], [], {}, []
    for path in map(Path, paths):
        t = parse_stdf(path)
        fp = fingerprint(t)
        if fp in seen:
            skipped.append(f"{path.name} (same content as {seen[fp]})")
            continue
        seen[fp] = path.name
        p = t["parts"].assign(lot=path.stem)
        r = t["test_results"].merge(p[["part_index", "x", "y", "hard_bin"]], on="part_index").assign(lot=path.stem)
        parts.append(p)
        results.append(r)
    return pd.concat(parts, ignore_index=True), pd.concat(results, ignore_index=True), skipped


def datalog_coverage(parts: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """How many parts carry parametric (PTR) data, per lot.

    The demo tester datalogs every second part (GDR IMAGE_PART_ID precedes each
    logged part), so per-test statistics describe a ~50% sample, not every die.
    """
    n = results.groupby(["lot", "part_index"]).size().rename("n_ptr")
    p = parts.join(n, on=["lot", "part_index"]).fillna({"n_ptr": 0})
    p["logged"] = p["n_ptr"] > 0
    return p.groupby("lot").agg(
        parts=("part_index", "size"),
        logged=("logged", "sum"),
        logged_pct=("logged", lambda s: round(100 * s.mean(), 1)),
        odd_index_logged_pct=("logged", lambda s: round(100 * s[p.loc[s.index, "part_index"] % 2 == 1].mean(), 1)),
    )


# --- yield and bins ---------------------------------------------------------

def first_pass(parts: pd.DataFrame) -> pd.DataFrame:
    """One row per die (lot, X, Y): its first insertion, before any retest."""
    return parts.sort_values(["lot", "part_index"]).drop_duplicates(["lot", "x", "y"], keep="first")


def retest_recovery(parts: pd.DataFrame) -> pd.DataFrame:
    """For dies that failed first pass and were retested: how many passed on retest, by first-pass bin.

    A bin that mostly recovers on retest points at the test (contact,
    marginal limit, measurement noise) rather than at the silicon.
    """
    p = parts.sort_values(["lot", "part_index"])
    first = p.drop_duplicates(["lot", "x", "y"], keep="first")
    last = p.drop_duplicates(["lot", "x", "y"], keep="last")
    m = first.merge(last, on=["lot", "x", "y"], suffixes=("_first", "_last"))
    m = m[(m["part_index_first"] != m["part_index_last"]) & ~m["passed_first"].astype(bool)]
    t = m.groupby(["hard_bin_first", "lot"]).agg(retested=("x", "size"), recovered=("passed_last", "sum")).unstack("lot", fill_value=0)
    t[("recovered_pct", "all")] = (100 * t["recovered"].sum(axis=1) / t["retested"].sum(axis=1)).round(0)
    return t.sort_values(("recovered_pct", "all"), ascending=False)


def sequence_runs(parts: pd.DataFrame, n_perm: int = 2000, seed: int = 0) -> pd.DataFrame:
    """Do failures come in bursts in test order? Longest run of consecutive first-pass fails vs. shuffled order.

    A burst that random order rarely produces (small p) points at a temporal
    event during probing (contact, prober, tester) rather than at the wafer.
    """
    rng = np.random.default_rng(seed)

    def longest(a: np.ndarray) -> int:
        best = cur = 0
        for v in a:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    out = []
    for lot, g in first_pass(parts).groupby("lot"):
        f = ~g.sort_values("part_index")["passed"].astype(bool).to_numpy()
        obs = longest(f)
        sims = np.array([longest(rng.permutation(f)) for _ in range(n_perm)])
        out.append({
            "lot": lot,
            "fail_pct": round(100 * f.mean(), 1),
            "fail_after_fail_pct": round(100 * f[1:][f[:-1]].mean(), 1),
            "longest_run": obs,
            "p_value": round(float((sims >= obs).mean()), 4),
        })
    return pd.DataFrame(out).set_index("lot")


def lot_bin_delta(parts: pd.DataFrame) -> pd.DataFrame:
    """First-pass failing hard bins as % of dies per lot, sorted by change from the first lot to the last."""
    parts = first_pass(parts)
    lots = sorted(parts["lot"].unique())
    tested = parts.groupby("lot").size()
    fails = parts[~parts["passed"].astype(bool)]
    t = fails.groupby(["hard_bin", "lot"]).size().unstack("lot", fill_value=0).reindex(columns=lots, fill_value=0)
    pct = (100 * t / tested[lots]).round(2)
    pct["delta_pts"] = (pct[lots[-1]] - pct[lots[0]]).round(2)
    return pct.sort_values("delta_pts", ascending=False)


def bin_signature(results: pd.DataFrame, top: int = 2) -> pd.DataFrame:
    """For each failing hard bin, the tests that failed first on its datalogged parts."""
    f = results[results["passed"] == False]  # noqa: E712 - pandas boolean mask
    first = f.groupby(["lot", "part_index"]).first().reset_index()
    first = first[first["hard_bin"] != 1]
    sig = first.groupby(["hard_bin", "test_name"]).size().rename("parts").reset_index()
    sig["pct_of_bin"] = (100 * sig["parts"] / sig.groupby("hard_bin")["parts"].transform("sum")).round(0)
    return sig.sort_values(["hard_bin", "parts"], ascending=[True, False]).groupby("hard_bin").head(top)


# --- capability -------------------------------------------------------------

def robust_cpk(results: pd.DataFrame, min_n: int = 30) -> pd.DataFrame:
    """Classic Cpk next to a robust Cpk (median and 1.4826*MAD instead of mean and sigma).

    Classic Cpk assumes one normal population. Test data is a tight main
    population plus a tail of gross failures, and a few failures far outside
    the limits inflate sigma (and drag the mean) enough to make a healthy test
    look incapable. The robust version describes the main population; the
    tail is a yield (defect) problem, counted separately as fail_pct.
    """
    classic = cpk_by_test(results, min_n=min_n)[["n", "cpk"]].rename(columns={"cpk": "cpk_classic"})
    rows = []
    for key, g in results.dropna(subset=["result"]).groupby(["test_num", "test_name", "units"], dropna=False):
        v = g["result"].to_numpy()
        lo, hi = g["lo"].iloc[0], g["hi"].iloc[0]
        med = np.median(v)
        s = 1.4826 * np.median(np.abs(v - med))
        if s == 0:  # quantised result: fall back to the IQR
            q1, q3 = np.percentile(v, [25, 75])
            s = (q3 - q1) / 1.349
        cpu = (hi - med) / (3 * s) if s > 0 and pd.notna(hi) else np.nan
        cpl = (med - lo) / (3 * s) if s > 0 and pd.notna(lo) else np.nan
        steps = np.diff(np.unique(v))
        res = steps.min() if len(steps) else np.nan
        tol_res = (hi - lo) / res if pd.notna(lo) and pd.notna(hi) and res > 0 else np.nan
        rows.append((*key, np.fmin(cpu, cpl), round(100 * (g["passed"] == False).mean(), 2), res, tol_res))  # noqa: E712
    cols = ["test_num", "test_name", "units", "cpk_robust", "fail_pct", "resolution", "tol_to_res"]
    rob = pd.DataFrame(rows, columns=cols).set_index(cols[:3])
    # AIAG MSA rule of thumb: the gauge should resolve at least 1/10 of the tolerance.
    rob["resolution_ok"] = rob["tol_to_res"] >= 10
    return classic.join(rob, how="inner").sort_values("cpk_classic")


def centering_gain(results: pd.DataFrame, test_num: int) -> dict:
    """Fail rate now vs. the same measurements shifted so their median sits mid-limits.

    Empirical (no normality assumption): it answers "how many of these dies
    would have passed if the trim target were centred", keeping the real spread.
    """
    g = results[results["test_num"] == test_num].dropna(subset=["result"])
    v, lo, hi = g["result"].to_numpy(), g["lo"].iloc[0], g["hi"].iloc[0]
    med = np.median(v)
    shifted = v + ((lo + hi) / 2 - med)
    s = 1.4826 * np.median(np.abs(v - med))
    return {
        "test": g["test_name"].iloc[0].split("<>")[-1].strip(),
        "lo": round(float(lo), 4), "hi": round(float(hi), 4), "median": round(float(med), 4), "sigma_robust": round(float(s), 4),
        "fail_pct_now": round(100 * float(np.mean((v < lo) | (v > hi))), 2),
        "fail_pct_centred": round(100 * float(np.mean((shifted < lo) | (shifted > hi))), 2),
        "cp": round(float((hi - lo) / (6 * s)), 2),
    }


def test_num_of(results: pd.DataFrame, short_name: str) -> int:
    """TEST_NUM for a test by the short name after '<>' in TEST_TXT (e.g. 'REF')."""
    short = results["test_name"].str.split("<>").str[-1].str.strip()
    return int(results.loc[short == short_name, "test_num"].iloc[0])


# --- spatial ----------------------------------------------------------------

def add_radius(parts: pd.DataFrame) -> pd.DataFrame:
    """Normalised distance from the wafer centre (0 = centre, 1 = outermost die), centre from the die extent."""
    cx = (parts["x"].min() + parts["x"].max()) / 2
    cy = (parts["y"].min() + parts["y"].max()) / 2
    d = np.hypot(parts["x"] - cx, parts["y"] - cy)
    return parts.assign(r=d / d.max(), fail=(~parts["passed"].astype(bool)).astype(int))


def yield_by_ring(parts: pd.DataFrame, edges=(0, 0.3, 0.5, 0.7, 0.8, 0.9, 1.0)) -> pd.DataFrame:
    """First-pass fail % per radial ring."""
    p = add_radius(first_pass(parts))
    p["ring"] = pd.cut(p["r"], list(edges), include_lowest=True)
    t = p.groupby(["ring", "lot"], observed=True).agg(dies=("fail", "size"), fail_pct=("fail", "mean"))
    t["fail_pct"] = (100 * t["fail_pct"]).round(1)
    return t.unstack("lot")


def edge_regression(parts: pd.DataFrame):
    """Logistic regression of die failure on position and lot.

    Compares a smooth radial model (r + r^2) with an edge-ring indicator by AIC
    and returns both fits plus a table of odds ratios for the chosen one.
    One row per die (first pass), so retests are not counted twice. Each lot
    here is one wafer, so lot and wafer effects are not separable.
    """
    import statsmodels.formula.api as smf

    p = add_radius(first_pass(parts))
    p["edge"] = (p["r"] > EDGE_R).astype(int)
    quad = smf.logit("fail ~ r + I(r**2) + C(lot)", p).fit(disp=0)
    ring = smf.logit("fail ~ edge + C(lot)", p).fit(disp=0)
    best = ring if ring.aic <= quad.aic else quad
    ci = np.exp(best.conf_int())
    odds = pd.DataFrame({"odds_ratio": np.exp(best.params), "ci_low": ci[0], "ci_high": ci[1], "p_value": best.pvalues}).round(4)
    return {"quadratic": quad, "edge_ring": ring, "best": "edge_ring" if best is ring else "quadratic", "odds": odds}


def neighbour_clustering(parts: pd.DataFrame) -> pd.DataFrame:
    """Fail rate of dies next to a failing die (4-neighbourhood) against two baselines.

    `ratio_raw` compares with the lot's overall fail rate; `ratio_vs_position`
    compares with what the edge-ring model already predicts for those same
    dies, because next-to-a-fail dies sit near the edge more often. Clearly
    above 1 after the position correction means failures cluster beyond the
    edge effect (a scratch, a particle, a probe-card problem).
    """
    p = add_radius(first_pass(parts))
    p["edge"] = (p["r"] > EDGE_R).astype(int)
    p["expected"] = edge_regression(parts)["edge_ring"].predict(p)
    out = []
    for lot, g in p.groupby("lot"):
        fail = {(x, y) for x, y, f in g[["x", "y", "fail"]].itertuples(index=False) if f}
        nb_fail = g.apply(lambda d: any((d.x + dx, d.y + dy) in fail for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))), axis=1)
        near = g.loc[nb_fail]
        out.append({
            "lot": lot,
            "base_fail_pct": round(100 * g["fail"].mean(), 1),
            "fail_pct_next_to_fail": round(100 * near["fail"].mean(), 1),
            "ratio_raw": round(near["fail"].mean() / g["fail"].mean(), 2),
            "ratio_vs_position": round(near["fail"].mean() / near["expected"].mean(), 2),
        })
    return pd.DataFrame(out).set_index("lot")


# --- figures ----------------------------------------------------------------

def plot_wafer_maps(parts: pd.DataFrame, out: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    full = parts
    parts = first_pass(parts)
    lots = sorted(parts["lot"].unique())
    bins = sorted(parts.loc[~parts["passed"].astype(bool), "hard_bin"].unique())
    top = parts[~parts["passed"].astype(bool)]["hard_bin"].value_counts().index[:5].tolist()
    palette = ["#2a6fdb", "#e8590c", "#7048e8", "#d6336c", "#0ca678"]
    colour = {b: palette[top.index(b)] if b in top else "#868e96" for b in bins}
    fig, axes = plt.subplots(1, len(lots), figsize=(5.2 * len(lots), 5.4), constrained_layout=True)
    for ax, lot in zip(np.atleast_1d(axes), lots):
        g = parts[parts["lot"] == lot]
        good = g[g["passed"].astype(bool)]
        ax.scatter(good["x"], good["y"], s=14, marker="s", c="#dee2e6", linewidths=0)
        for b in bins:
            f = g[(~g["passed"].astype(bool)) & (g["hard_bin"] == b)]
            if len(f):
                ax.scatter(f["x"], f["y"], s=14, marker="s", c=colour[b], linewidths=0, label=f"bin {b}" if b in top else None)
        y = die_yield(full[full["lot"] == lot])
        ax.set_title(f"{lot}  first pass {y['first_pass_yield_pct']}%  final {y['final_yield_pct']}%", fontsize=11)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(False)
    handles, labels = np.atleast_1d(axes)[-1].get_legend_handles_labels()
    handles.append(plt.Line2D([], [], marker="s", ls="", color="#868e96"))
    labels.append("other fail bins")
    fig.legend(handles, labels, loc="outside right center", frameon=False, title="first-pass bin\n(grey = pass)")
    path = out / "wafer_maps.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_test_histograms(results: pd.DataFrame, test_nums: list[int], out: Path) -> Path:
    """Histogram of the main population of each test with its limits; the clipped gross-failure tail is counted in the title."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(test_nums), figsize=(4.2 * len(test_nums), 3.4), constrained_layout=True)
    for ax, tn in zip(np.atleast_1d(axes), test_nums):
        g = results[results["test_num"] == tn].dropna(subset=["result"])
        lo, hi = g["lo"].iloc[0], g["hi"].iloc[0]
        v = g["result"]
        span = (hi - lo) if pd.notna(lo) and pd.notna(hi) else v.std()
        a = (lo if pd.notna(lo) else v.min()) - 0.25 * span
        b = (hi if pd.notna(hi) else v.max()) + 0.25 * span
        inside = v[(v >= a) & (v <= b)]
        for lot, c in zip(sorted(g["lot"].unique()), ["#2a6fdb", "#e8590c"]):
            ax.hist(inside[g.loc[inside.index, "lot"] == lot], bins=60, range=(a, b), alpha=0.6, color=c, label=lot)
        for lim in (lo, hi):
            if pd.notna(lim):
                ax.axvline(lim, color="#c92a2a", lw=1.2, ls="--")
        name = g["test_name"].iloc[0].split("<>")[-1].strip()
        ax.set_title(f"{name} [{g['units'].iloc[0]}]  ({len(v) - len(inside)} off-scale)", fontsize=10)
        ax.ticklabel_format(axis="x", style="sci", scilimits=(-3, 3))
        ax.tick_params(labelsize=8)
    np.atleast_1d(axes)[0].legend(frameon=False, fontsize=8)
    path = out / "test_histograms.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_edge_effect(parts: pd.DataFrame, out: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = yield_by_ring(parts)["fail_pct"]
    fig, ax = plt.subplots(figsize=(6, 3.4), constrained_layout=True)
    labels = [f"{i.left:.1f}-{i.right:.1f}".replace("-0.0", "0.0") for i in t.index]
    for lot, c in zip(t.columns, ["#2a6fdb", "#e8590c"]):
        ax.plot(labels, t[lot], marker="o", color=c, label=lot)
    ax.set_xlabel("normalised radius (0 = centre, 1 = edge)")
    ax.set_ylabel("failing dies (%)")
    ax.set_ylim(bottom=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False)
    path = out / "edge_effect.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stdf", nargs="+")
    ap.add_argument("--figures", type=Path, help="write PNG figures to this directory")
    args = ap.parse_args()
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    parts, results, skipped = load_lots(args.stdf)
    for s in skipped:
        print(f"skipped duplicate: {s}")
    print("\n== Datalog coverage ==\n", datalog_coverage(parts, results))
    print("\n== Yield ==")
    for lot, g in parts.groupby("lot"):
        print(f"  {lot}: insertions {yield_summary(g)}  dies {die_yield(g)}")
    print("\n== Retest recovery by first-pass bin ==\n", retest_recovery(parts))
    print("\n== Failure bursts in test order (first pass) ==\n", sequence_runs(parts))
    print("\n== Failing bins, % of tested ==\n", lot_bin_delta(parts))
    print("\n== Bin signatures (first failing test on datalogged parts) ==\n", bin_signature(results).to_string(index=False))
    cpk = robust_cpk(results)
    print("\n== Capability, worst classic Cpk ==\n", cpk.head(8).round(3))
    print("\n== Capability, worst robust Cpk ==\n", cpk.sort_values("cpk_robust").head(5).round(3))
    print("\n== Resolution-limited tests (tolerance < 10 measurement steps) ==\n", cpk[~cpk["resolution_ok"]].round(4))
    ref = test_num_of(results, "REF")
    print("\n== Centring REF ==\n", centering_gain(results, ref))
    print("\n== Fail % by ring ==\n", yield_by_ring(parts))
    reg = edge_regression(parts)
    print(f"\n== Logistic regression (AIC quadratic {reg['quadratic'].aic:.1f}, edge ring {reg['edge_ring'].aic:.1f}; best: {reg['best']}) ==\n", reg["odds"])
    print("\n== Failure clustering ==\n", neighbour_clustering(parts))

    if args.figures:
        args.figures.mkdir(parents=True, exist_ok=True)
        worst = [test_num_of(results, n) for n in ("LK_PWR", "REF", "OSC_VL24", "ABS_COM")]
        for p in (plot_wafer_maps(parts, args.figures), plot_test_histograms(results, worst, args.figures), plot_edge_effect(parts, args.figures)):
            print(f"wrote {p}")


if __name__ == "__main__":
    main()
