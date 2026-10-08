"""Phase 1 sanity report on the Parquet tables: yield, bin Pareto and the worst Cpk tests.

This is the minimal reference version. Phase 3 rebuilds these numbers in SQL
(and adds drift across lots); keep this one as the answer key to check against.

Usage:  python -m ate_pipeline.report data/parquet/lot2
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def load(dir_: str | Path) -> dict[str, pd.DataFrame]:
    d = Path(dir_)
    return {p.stem: pd.read_parquet(p) for p in d.glob("*.parquet")}


def yield_summary(parts: pd.DataFrame) -> dict:
    tested = len(parts)
    good = int(parts["passed"].sum())
    return {"tested": tested, "good": good, "yield_pct": round(100 * good / tested, 2) if tested else None}


def die_yield(parts: pd.DataFrame) -> dict:
    """Yield per physical die (X/Y) instead of per test insertion.

    Dies that fail are often re-probed at the end of the wafer, so the PRR
    count (and the tester's own HBR/SBR summary) counts them twice.
    First-pass yield uses each die's first insertion, final yield its last.
    """
    p = parts.sort_values("part_index")
    first = p.drop_duplicates(["x", "y"], keep="first")
    last = p.drop_duplicates(["x", "y"], keep="last")
    return {
        "insertions": len(p),
        "dies": len(first),
        "retested": len(p) - len(first),
        "first_pass_yield_pct": round(100 * float(first["passed"].mean()), 2),
        "final_yield_pct": round(100 * float(last["passed"].mean()), 2),
    }


def bin_pareto(parts: pd.DataFrame) -> pd.DataFrame:
    fails = parts[parts["passed"] == False]  # noqa: E712 - pandas boolean mask
    p = fails.groupby("hard_bin").size().sort_values(ascending=False).rename("count").to_frame()
    p["pct_of_fails"] = (100 * p["count"] / p["count"].sum()).round(1)
    p["cum_pct"] = p["pct_of_fails"].cumsum().round(1)
    return p


def cpk_by_test(results: pd.DataFrame, min_n: int = 30) -> pd.DataFrame:
    """Cpk = min(HI - mean, mean - LO) / (3 * sigma), one-sided when a limit is missing."""
    g = results.dropna(subset=["result"]).groupby(["test_num", "test_name", "units"], dropna=False)
    s = g.agg(n=("result", "size"), mean=("result", "mean"), sigma=("result", "std"), lo=("lo", "first"), hi=("hi", "first"))
    s = s[(s["n"] >= min_n) & (s["sigma"] > 0)]
    cpu = (s["hi"] - s["mean"]) / (3 * s["sigma"])
    cpl = (s["mean"] - s["lo"]) / (3 * s["sigma"])
    s["cpk"] = np.fmin(cpu, cpl)  # fmin ignores a NaN side (missing limit)
    return s.dropna(subset=["cpk"]).sort_values("cpk")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("parquet_dir")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    t = load(args.parquet_dir)
    lot = t["lot"].iloc[0]
    print(f"Lot {lot['lot_id']}  part {lot['part_type']}  tester {lot['tester_type']}  program {lot['program']} rev {lot['program_rev']}")
    print("Insertion yield (what the tester's bin summary counts):", yield_summary(t["parts"]))
    print("Die yield:", die_yield(t["parts"]))
    print("\nHard-bin Pareto (failing parts):")
    print(bin_pareto(t["parts"]).head(args.top).to_string())
    print(f"\n{args.top} lowest-Cpk tests:")
    cols = ["n", "mean", "sigma", "lo", "hi", "cpk"]
    print(cpk_by_test(t["test_results"])[cols].head(args.top).round(4).to_string())


if __name__ == "__main__":
    main()
