"""Yearly rainfall cycle for the 雨 module: a static 12-month climatology,
not a live forecast.

Many locations' meaningful annual environmental cycle isn't the four-season
model the rest of this project's Japanese-calendar content otherwise
assumes (e.g. tropical locations are often wet/dry, not spring/summer/fall/
winter) -- this module is the deliberate exception, showing the configured
location's actual rainfall shape across the year instead.

Deliberately static/local: climate normals barely move year to year, so
this is loaded from a pre-fetched JSON file (see
scripts/fetch_rainfall_climatology.py) rather than hit over the network on
every render. Missing/malformed data degrades the same way weather.py
does -- the caller gets None and omits the module.
"""
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RainfallClimatology:
    monthly_mm: list[float]  # length 12, Jan..Dec
    source: str
    latitude: float
    longitude: float


def load_rainfall_climatology(path: Path | str) -> "RainfallClimatology | None":
    """Pure read of the static climatology file. Returns None on any
    missing/malformed data -- never raises."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        monthly_mm = [float(v) for v in raw["monthly_mm"]]
        if len(monthly_mm) != 12:
            return None
        return RainfallClimatology(
            monthly_mm=monthly_mm,
            source=raw.get("source", "unknown"),
            latitude=float(raw.get("latitude", 0.0)),
            longitude=float(raw.get("longitude", 0.0)),
        )
    except Exception:
        return None
