"""Phase 1: parse an STDF v4 file into tidy tables and write them as Parquet.

Tables
  lot           one row per file: lot, wafer, part type, tester, program, start time
  parts         one row per tested die/unit: site, hard/soft bin, pass flag, X/Y
  test_results  one row per parametric measurement (PTR): value, limits, units, pass flag
  bins          hard/soft bin summary records (HBR/SBR) as written by the tester

Usage:  python -m ate_pipeline.ingest data/raw/lot2.stdf --out data/parquet/lot2
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
from pystdf.IO import Parser

# PTR OPT_FLAG bits (STDF v4): bit 4/5 = LO/HI limit field invalid (use the
# default from the first PTR of that test); bit 6/7 = test has no LO/HI limit.
LO_INVALID, HI_INVALID, NO_LO, NO_HI = 0x10, 0x20, 0x40, 0x80
# PTR TEST_FLG bit 7 = test failed, bit 6 = pass/fail flag not valid.
TEST_FAILED, PF_INVALID = 0x80, 0x40
# PRR PART_FLG bit 3 = part failed, bit 4 = pass/fail flag not valid.
PART_FAILED, PART_PF_INVALID = 0x08, 0x10


class _Collector:
    """pystdf sink: receives every record and builds the tables in memory."""

    def __init__(self) -> None:
        self.lot: dict = {}
        self.parts: list[dict] = []
        self.results: list[dict] = []
        self.bins: list[dict] = []
        self._open: dict[tuple[int, int], list[dict]] = {}  # (head, site) -> PTRs of the part in test
        self._limits: dict[int, dict] = {}  # TEST_NUM -> default limits/units from its first PTR
        self._wafer_id: str | None = None

    # pystdf calls these hooks; only after_send carries records.
    def before_begin(self, ds): ...
    def after_begin(self, ds): ...
    def before_send(self, ds, data): ...
    def before_complete(self, ds): ...
    def after_complete(self, ds): ...

    def after_send(self, ds, data) -> None:
        rec_type, fields = data
        rec = dict(zip(rec_type.fieldNames, fields))
        handler = getattr(self, f"_on_{type(rec_type).__name__.lower()}", None)
        if handler:
            handler(rec)

    def _on_mir(self, r: dict) -> None:
        self.lot = {
            "lot_id": r["LOT_ID"],
            "sublot_id": r["SBLOT_ID"],
            "part_type": r["PART_TYP"],
            "tester_type": r["TSTR_TYP"],
            "tester_node": r["NODE_NAM"],
            "program": r["JOB_NAM"],
            "program_rev": r["JOB_REV"],
            "operation": r["OPER_NAM"],
            "start_time": dt.datetime.fromtimestamp(r["START_T"], dt.timezone.utc),
        }

    def _on_wir(self, r: dict) -> None:
        self._wafer_id = r["WAFER_ID"]

    def _on_pir(self, r: dict) -> None:
        self._open[(r["HEAD_NUM"], r["SITE_NUM"])] = []

    def _on_ptr(self, r: dict) -> None:
        test = r["TEST_NUM"]
        opt = r["OPT_FLAG"] or 0
        first = self._limits.setdefault(test, {"test_name": r["TEST_TXT"], "units": r["UNITS"], "lo": None, "hi": None})
        if r["TEST_TXT"]:
            first["test_name"] = first["test_name"] or r["TEST_TXT"]
        if r["UNITS"] and not first["units"]:
            first["units"] = r["UNITS"]
        if not opt & LO_INVALID and r["LO_LIMIT"] is not None and first["lo"] is None:
            first["lo"] = None if opt & NO_LO else r["LO_LIMIT"]
        if not opt & HI_INVALID and r["HI_LIMIT"] is not None and first["hi"] is None:
            first["hi"] = None if opt & NO_HI else r["HI_LIMIT"]
        flg = r["TEST_FLG"] or 0
        self._open.setdefault((r["HEAD_NUM"], r["SITE_NUM"]), []).append(
            {
                "test_num": test,
                "result": r["RESULT"],
                "passed": None if flg & PF_INVALID else not flg & TEST_FAILED,
            }
        )

    def _on_prr(self, r: dict) -> None:
        key = (r["HEAD_NUM"], r["SITE_NUM"])
        part_index = len(self.parts)
        flg = r["PART_FLG"] or 0
        self.parts.append(
            {
                "part_index": part_index,
                "part_id": r["PART_ID"],
                "wafer_id": self._wafer_id,
                "head": r["HEAD_NUM"],
                "site": r["SITE_NUM"],
                "hard_bin": r["HARD_BIN"],
                "soft_bin": r["SOFT_BIN"],
                "passed": None if flg & PART_PF_INVALID else not flg & PART_FAILED,
                "x": r["X_COORD"],
                "y": r["Y_COORD"],
                "num_tests": r["NUM_TEST"],
                "test_time_ms": r["TEST_T"],
            }
        )
        for m in self._open.pop(key, []):
            self.results.append({"part_index": part_index, **m})

    def _on_hbr(self, r: dict) -> None:
        self.bins.append({"kind": "hard", "head": r["HEAD_NUM"], "site": r["SITE_NUM"], "bin": r["HBIN_NUM"], "count": r["HBIN_CNT"], "pass_fail": r["HBIN_PF"]})

    def _on_sbr(self, r: dict) -> None:
        self.bins.append({"kind": "soft", "head": r["HEAD_NUM"], "site": r["SITE_NUM"], "bin": r["SBIN_NUM"], "count": r["SBIN_CNT"], "pass_fail": r["SBIN_PF"]})


def parse_stdf(path: str | Path) -> dict[str, pd.DataFrame]:
    """Parse one STDF file into the four tables."""
    sink = _Collector()
    with open(path, "rb") as f:
        parser = Parser(inp=f)
        parser.addSink(sink)
        parser.parse()

    tests = pd.DataFrame(
        [{"test_num": k, **v} for k, v in sink._limits.items()],
        columns=["test_num", "test_name", "units", "lo", "hi"],
    )
    results = pd.DataFrame(sink.results, columns=["part_index", "test_num", "result", "passed"])
    results = results.merge(tests, on="test_num", how="left")
    lot = pd.DataFrame([{**sink.lot, "source_file": Path(path).name}])
    return {
        "lot": lot,
        "parts": pd.DataFrame(sink.parts),
        "test_results": results,
        "bins": pd.DataFrame(sink.bins),
    }


def write_parquet(tables: dict[str, pd.DataFrame], out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(out / f"{name}.parquet", index=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stdf", help="path to an .stdf file")
    ap.add_argument("--out", required=True, help="output directory for the Parquet tables")
    args = ap.parse_args()
    tables = parse_stdf(args.stdf)
    write_parquet(tables, args.out)
    for name, df in tables.items():
        print(f"{name:13s} {len(df):>7,} rows")


if __name__ == "__main__":
    main()
