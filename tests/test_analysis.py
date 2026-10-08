"""Pins the cross-lot findings in docs/FINDINGS.md to the demo data, plus unit checks on synthetic data."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ate_pipeline import analysis as A
from ate_pipeline.report import die_yield

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
FILES = [RAW / f for f in ("lot2.stdf", "lot3.stdf", "demofile.stdf")]
needs_data = pytest.mark.skipif(not all(f.exists() for f in FILES), reason="run `python scripts/get_sample_data.py` first")


@pytest.fixture(scope="module")
def lots():
    return A.load_lots(FILES)


@needs_data
def test_demofile_is_lot3_relabelled(lots):
    parts, _, skipped = lots
    assert skipped == ["demofile.stdf (same content as lot3.stdf)"]
    assert sorted(parts["lot"].unique()) == ["lot2", "lot3"]


@needs_data
def test_every_second_part_is_datalogged(lots):
    parts, results, _ = lots
    cov = A.datalog_coverage(parts, results)
    assert (cov["logged_pct"] == 50.0).all() and (cov["odd_index_logged_pct"] == 100.0).all()


@needs_data
def test_retests_are_counted_once_per_die(lots):
    parts, _, _ = lots
    y2 = die_yield(parts[parts["lot"] == "lot2"])
    assert (y2["insertions"], y2["dies"], y2["retested"]) == (1569, 1456, 113)
    assert (y2["first_pass_yield_pct"], y2["final_yield_pct"]) == (92.24, 95.4)


@needs_data
def test_bin_20_recovers_on_retest_and_is_resolution_limited(lots):
    parts, results, _ = lots
    rec = A.retest_recovery(parts)
    assert rec.loc[20, ("recovered_pct", "all")] >= 95
    assert rec.loc[2, ("recovered_pct", "all")] == 0  # leakage fails are real
    cpk = A.robust_cpk(results)
    limited = cpk[~cpk["resolution_ok"]].index.get_level_values("test_num")
    assert list(limited) == [A.test_num_of(results, "OSC_VL24")]


@needs_data
def test_negative_classic_cpk_is_an_outlier_artifact(lots):
    _, results, _ = lots
    row = A.robust_cpk(results).xs(A.test_num_of(results, "LK_PWR"), level="test_num").iloc[0]
    assert row["cpk_classic"] < 0 < 1.33 < row["cpk_robust"]


@needs_data
def test_centring_ref_cuts_its_fails(lots):
    _, results, _ = lots
    g = A.centering_gain(results, A.test_num_of(results, "REF"))
    assert g["fail_pct_centred"] < g["fail_pct_now"] / 4


@needs_data
def test_edge_ring_fails_more(lots):
    parts, _, _ = lots
    reg = A.edge_regression(parts)
    assert reg["best"] == "edge_ring"
    assert reg["odds"].loc["edge", "ci_low"] > 1


# --- synthetic -----------------------------------------------------------------

def _results(values, lo, hi):
    v = np.asarray(values, dtype=float)
    return pd.DataFrame({"test_num": 1, "test_name": "t <> T", "units": "v", "result": v, "lo": lo, "hi": hi, "passed": (v >= lo) & (v <= hi)})


def test_robust_cpk_ignores_gross_outliers():
    rng = np.random.default_rng(1)
    v = np.r_[rng.normal(0, 1, 500), [-200.0] * 10]  # tight population plus a gross-failure tail
    row = A.robust_cpk(_results(v, -6, 6)).iloc[0]
    assert row["cpk_classic"] < 0.5
    assert row["cpk_robust"] == pytest.approx(2.0, abs=0.25)


def test_sequence_runs_flags_a_burst():
    n = 400
    passed = np.ones(n, bool)
    passed[200:212] = False
    passed[[20, 90, 300]] = False
    parts = pd.DataFrame({"lot": "L", "part_index": range(n), "x": range(n), "y": 0, "passed": passed})
    out = A.sequence_runs(parts, n_perm=500)
    assert out.loc["L", "longest_run"] == 12 and out.loc["L", "p_value"] < 0.01
