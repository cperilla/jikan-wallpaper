#!/usr/bin/env python3
"""One-off fetch: 12-month rainfall climatology for the configured location,
via NASA POWER's climatology endpoint (20-year MERRA-2 monthly means, no API
key required). Writes data/rainfall_climatology.json.

Deliberately NOT run as part of the normal render pipeline (see rainfall.py
and README.md) -- climate normals barely move year to year, so this is a
manual/occasional refresh, not a per-render network call.

Usage:
    python scripts/fetch_rainfall_climatology.py [--config path/to/config.toml]
"""
import argparse
import calendar
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DEFAULT_CONFIG_PATH, load_config

POWER_URL = (
    "https://power.larc.nasa.gov/api/temporal/climatology/point"
    "?parameters=PRECTOTCORR&community=AG&format=JSON"
    "&latitude={lat}&longitude={lon}&start=2001&end=2020"
)

_MONTH_KEYS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def fetch(lat: float, lon: float, timeout: float = 15.0) -> dict:
    url = POWER_URL.format(lat=lat, lon=lon)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)
    mm_per_day = payload["properties"]["parameter"]["PRECTOTCORR"]

    # mm/day -> mm/month using a fixed non-leap reference year (climatology
    # is already an average across 20 years, so the +/-1 day in leap
    # February is negligible noise on top of that).
    monthly_mm = []
    for i, key in enumerate(_MONTH_KEYS, start=1):
        days_in_month = calendar.monthrange(2019, i)[1]
        monthly_mm.append(round(mm_per_day[key] * days_in_month, 1))

    return {
        "source": "NASA POWER Climatology API (MERRA-2), 2001-2020 monthly means",
        "period": "2001-2020",
        "latitude": lat,
        "longitude": lon,
        "unit": "mm",
        "monthly_mm": monthly_mm,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path,
                         default=Path(__file__).parent.parent / "data" / "rainfall_climatology.json")
    args = parser.parse_args()

    cfg = load_config(args.config)
    data = fetch(cfg.location.latitude, cfg.location.longitude)

    args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    print(json.dumps(data["monthly_mm"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
