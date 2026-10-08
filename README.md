# ATE Test Data Pipeline

From raw ATE tester output (STDF v4) to yield analytics: a hands-on learning
project that applies data engineering (Python, SQL, PySpark, Databricks) to
semiconductor test data.

## Data

Three public demo STDF files from wafer sort (Galaxy Semiconductor demo data,
shipped with [pystdf](https://github.com/cmars/pystdf)): lot `GAL-LOT`, part
`GOLD8BAR`, tester `A530`. About 1,570 dies per file with X/Y wafer
coordinates, ~52,000 parametric measurements with limits and units, plus the
tester's own hard/soft bin summaries.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
python scripts/get_sample_data.py
pytest
```

## Phase 1 - Ingest (done: the reference to build on)

```bash
python -m ate_pipeline.ingest data/raw/lot2.stdf --out data/parquet/lot2
python -m ate_pipeline.report data/parquet/lot2
```

`ingest.py` turns one STDF file into four Parquet tables:

| Table | Grain | Key columns |
|-------|-------|-------------|
| `lot` | one per file | lot_id, part_type, tester_type, program, start_time |
| `parts` | one per die | part_index, wafer_id, site, hard_bin, soft_bin, passed, x, y |
| `test_results` | one per PTR measurement | part_index, test_num, test_name, result, lo, hi, units, passed |
| `bins` | tester's HBR/SBR records | kind, head, site, bin, count |

The tests check the parser against the tester's own bookkeeping: computed good
dies must equal the tester's bin-1 count (lot2: 1,389 of 1,569, 88.53% yield).

STDF details worth knowing before you extend it:
- A PTR carries limits and units only on the first occurrence of each test;
  later PTRs set `OPT_FLAG` bits 4/5 and inherit them (`_on_ptr`).
- Measurements arrive between a PIR and a PRR per (head, site); the PRR closes
  the part and carries its bin, pass flag and X/Y (`_on_prr`).
- `HEAD_NUM == 255` on HBR/SBR means "summary across all sites".

## Phase 2 - Pipeline in PySpark / Databricks (your turn)

Goal: process all three lots as one dataset.
- [ ] Ingest all files in `data/raw/` with a `lot_id`/`source_file` column on every table
- [ ] Load the Parquet into Spark (`spark.read.parquet`) - locally with `pip install pyspark`, or in Databricks Free Edition
- [ ] Write them as Delta tables in a bronze/silver layout: bronze = as parsed, silver = typed, deduplicated, with `wafer_id` + `part_id` as the key
- [ ] Make the job idempotent: re-running on the same file must not duplicate rows

## Phase 3 - Analytics in SQL (your turn)

Rebuild `report.py` in SQL over the silver tables, then go further. Use
`report.py`'s output as the answer key.
- [ ] Yield per lot and per wafer
- [ ] Hard-bin Pareto with cumulative %
- [ ] Cpk per test (window functions), worst 10 per lot
- [ ] Drift: per-test mean and sigma per lot - which tests move between lots?
- [ ] Site-to-site comparison (multi-site testers mask problems in one site)

## Phase 4 - Dashboard (your turn)

- [ ] Wafer map: X/Y coloured by hard bin (matplotlib or plotly)
- [ ] Yield trend and bin Pareto per lot
- [ ] Databricks SQL dashboard or a small Streamlit app

## Phase 5 - Yield prediction (stretch)

- [ ] [UCI SECOM](https://archive.ics.uci.edu/dataset/179/secom): 1,567 runs, 591 sensor features, 104 failures
- [ ] Handle missing values and the 14:1 class imbalance; report precision/recall, not accuracy

## Learning resources

- [Data Engineering Zoomcamp](https://github.com/DataTalksClub/data-engineering-zoomcamp) (free, self-paced): Docker, dbt, Spark, Kafka - make this project your final project
- STDF V4 specification (search "STDF V4 specification Teradyne") for every record field used here

## License

GPL-2.0-or-later (see `LICENSE`), matching pystdf, which this project imports.
