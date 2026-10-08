"""Checks the parser against the tester's own bookkeeping in the same file."""
from pathlib import Path

import pytest

from ate_pipeline.ingest import parse_stdf
from ate_pipeline.report import bin_pareto, cpk_by_test, yield_summary

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "raw" / "lot2.stdf"
pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="run `python scripts/get_sample_data.py` first")


@pytest.fixture(scope="module")
def tables():
    return parse_stdf(SAMPLE)


def test_part_count_matches_wafer_record(tables):
    # WRR/PCR in lot2.stdf report 1,569 parts tested.
    assert len(tables["parts"]) == 1569


def test_good_parts_match_tester_bin_1(tables):
    # The tester's summary HBR (head 255 = all sites) for bin 1 is the good-die count.
    bins = tables["bins"]
    tester_good = bins.query("kind == 'hard' and head == 255 and bin == 1")["count"].sum()
    assert yield_summary(tables["parts"])["good"] == tester_good


def test_every_measurement_belongs_to_a_part(tables):
    r = tables["test_results"]
    assert len(r) == 52403
    assert r["part_index"].isin(tables["parts"]["part_index"]).all()


def test_limits_carried_forward_from_first_ptr(tables):
    first = tables["test_results"].query("test_num == 1000")
    assert first["lo"].notna().all() and first["hi"].notna().all()
    assert first["units"].iloc[0] == "v"


def test_report_helpers(tables):
    assert bin_pareto(tables["parts"])["cum_pct"].iloc[-1] == pytest.approx(100, abs=0.2)
    cpk = cpk_by_test(tables["test_results"])
    assert not cpk.empty and cpk["cpk"].is_monotonic_increasing
