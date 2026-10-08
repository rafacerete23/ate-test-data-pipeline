"""Download the three public demo STDF files shipped with pystdf (Galaxy Semiconductor demo data).

They are not committed here: they belong to the pystdf repository. Pinned to a
commit so the dataset never changes under your analysis.

Usage:  python scripts/get_sample_data.py
"""
from pathlib import Path
from urllib.request import urlopen

COMMIT = "2215079c00e2f7b4c0be133baef4bf2a70d25859"
BASE = f"https://raw.githubusercontent.com/cmars/pystdf/{COMMIT}/data"
FILES = ["demofile.stdf", "lot2.stdf", "lot3.stdf"]

dest = Path(__file__).resolve().parent.parent / "data" / "raw"
dest.mkdir(parents=True, exist_ok=True)
for name in FILES:
    target = dest / name
    if target.exists():
        print(f"skip {name} (already downloaded)")
        continue
    with urlopen(f"{BASE}/{name}", timeout=60) as r:
        target.write_bytes(r.read())
    print(f"got  {name} ({target.stat().st_size:,} bytes)")
