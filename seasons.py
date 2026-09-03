"""
Solar-longitude computation for 二十四節気 (24 sekki) and 七十二候 (72 kō).

No hardcoded per-year date tables, no ephemeris dependency. Uses the standard
low-precision solar position formulas (~0.01 degree accuracy), which is far
tighter than the day-level granularity these terms are read at.

24 sekki occupy 15 degrees of solar ecliptic longitude each (24*15=360).
72 ko subdivide each sekki into exactly 3 microseasons of 5 degrees each
(72*5=360=24*15) -- an exact astronomical relationship, not a day-count
approximation.

The 24-sekki table below is stored in ASCENDING LONGITUDE order starting at
0 degrees = 春分 (spring equinox), so that table index == longitude bucket
index with no remapping. This differs from the traditional human-facing
order, which starts at 立春 (315 degrees) -- both orderings are documented
on each entry via the `traditional_index` field.
"""
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# (name_ja, reading_romaji, traditional_index)
# traditional_index: position in the classic 立春-first ordering (0-23).
# Table order below is ascending longitude, starting at 0deg = 春分.
SEKKI = [
    ("春分", "shunbun", 3, "Spring Equinox"),        # 0 deg
    ("清明", "seimei", 4, "Pure Brightness"),         # 15 deg
    ("穀雨", "kokuu", 5, "Grain Rain"),                # 30 deg
    ("立夏", "rikka", 6, "Start of Summer"),           # 45 deg
    ("小満", "shouman", 7, "Grain Full"),               # 60 deg
    ("芒種", "boushu", 8, "Grain in Ear"),               # 75 deg
    ("夏至", "geshi", 9, "Summer Solstice"),             # 90 deg
    ("小暑", "shousho", 10, "Minor Heat"),                # 105 deg
    ("大暑", "taisho", 11, "Major Heat"),                  # 120 deg
    ("立秋", "risshuu", 12, "Start of Autumn"),             # 135 deg
    ("処暑", "shosho", 13, "Limit of Heat"),                 # 150 deg
    ("白露", "hakuro", 14, "White Dew"),                      # 165 deg
    ("秋分", "shuubun", 15, "Autumn Equinox"),                 # 180 deg
    ("寒露", "kanro", 16, "Cold Dew"),                           # 195 deg
    ("霜降", "soukou", 17, "Frost's Descent"),                    # 210 deg
    ("立冬", "rittou", 18, "Start of Winter"),                     # 225 deg
    ("小雪", "shousetsu", 19, "Minor Snow"),                         # 240 deg
    ("大雪", "taisetsu", 20, "Major Snow"),                           # 255 deg
    ("冬至", "touji", 21, "Winter Solstice"),                         # 270 deg
    ("小寒", "shoukan", 22, "Minor Cold"),                             # 285 deg
    ("大寒", "daikan", 23, "Major Cold"),                               # 300 deg
    ("立春", "risshun", 0, "Start of Spring"),                          # 315 deg
    ("雨水", "usui", 1, "Rain Water"),                                   # 330 deg
    ("啓蟄", "keichitsu", 2, "Insects Awaken"),                           # 345 deg
]

SUB_LABELS = ["初候", "次候", "末候"]


@dataclass(frozen=True)
class TermInfo:
    index: int          # 0-23, ascending-longitude order
    name_ja: str
    reading_romaji: str
    traditional_index: int
    gloss_en: str
    longitude_deg: float  # actual apparent solar longitude used to compute this


@dataclass(frozen=True)
class MicroseasonInfo:
    index: int           # 0-71, global
    sekki_index: int      # 0-23, ascending-longitude order (matches TermInfo.index)
    sub_index: int         # 0/1/2
    sub_label_ja: str
    name_ja: str
    reading_romaji: str
    gloss_en: str | None
    longitude_deg: float


def _to_julian_day(dt_utc: datetime) -> float:
    """UTC datetime -> Julian Day (fractional)."""
    y, m, day = dt_utc.year, dt_utc.month, dt_utc.day
    frac_day = (dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600) / 24
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd0 = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + day + b - 1524.5
    return jd0 + frac_day


def _local_noon_as_utc(d: date) -> datetime:
    """Local noon (this machine's system timezone) of date d, converted to UTC.

    Deliberately NOT UTC noon -- a naive '+0.5 day' offset silently assumes
    UTC, which is wrong for any non-UTC timezone and can flip which side of a
    term/microseason boundary a borderline day falls on.
    """
    naive_local_noon = datetime(d.year, d.month, d.day, 12, 0, 0)
    aware_local = naive_local_noon.astimezone()  # attaches system local tzinfo
    return aware_local.astimezone(timezone.utc)


def solar_apparent_longitude(d: date) -> float:
    """Apparent geocentric ecliptic longitude of the Sun (degrees, 0-360),
    evaluated at local noon (system timezone) of the given date."""
    dt_utc = _local_noon_as_utc(d)
    jd = _to_julian_day(dt_utc)
    t = (jd - 2451545.0) / 36525.0

    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t**2
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t**2
    m_rad = math.radians(m)

    c = (
        (1.914602 - 0.004817 * t - 0.000014 * t**2) * math.sin(m_rad)
        + (0.019993 - 0.000101 * t) * math.sin(2 * m_rad)
        + 0.000289 * math.sin(3 * m_rad)
    )

    l_true = l0 + c
    omega = 125.04 - 1934.136 * t
    lam = l_true - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    return lam % 360.0


def _term_from_longitude(lam: float) -> TermInfo:
    idx = int(lam // 15) % 24
    name, romaji, trad_idx, gloss = SEKKI[idx]
    return TermInfo(index=idx, name_ja=name, reading_romaji=romaji,
                     traditional_index=trad_idx, gloss_en=gloss, longitude_deg=lam)


def current_term(d: date) -> TermInfo:
    return _term_from_longitude(solar_apparent_longitude(d))


def next_term_boundary(d: date) -> tuple[date, int]:
    """Returns (date_of_next_term_start, days_remaining)."""
    start_idx = current_term(d).index
    probe = d
    for days in range(1, 20):
        probe = d + timedelta(days=days)
        if current_term(probe).index != start_idx:
            return probe, days
    raise RuntimeError("no term boundary found within 20 days (should be impossible)")


_MICROSEASONS: list[dict] | None = None


def _load_microseasons() -> list[dict]:
    global _MICROSEASONS
    if _MICROSEASONS is None:
        with open(DATA_DIR / "72_microseasons.json", encoding="utf-8") as f:
            _MICROSEASONS = json.load(f)["microseasons"]
    return _MICROSEASONS


def _microseason_from_longitude(lam: float) -> MicroseasonInfo:
    micro_idx = int(lam // 5) % 72
    entries = _load_microseasons()
    e = entries[micro_idx]
    return MicroseasonInfo(
        index=e["index"], sekki_index=e["sekki_index"], sub_index=e["sub_index"],
        sub_label_ja=e["sub_label_ja"], name_ja=e["name_ja"],
        reading_romaji=e["reading_romaji"], gloss_en=e.get("gloss_en"),
        longitude_deg=lam,
    )


def current_microseason(d: date) -> MicroseasonInfo:
    return _microseason_from_longitude(solar_apparent_longitude(d))


def next_microseason_boundary(d: date) -> tuple[date, int]:
    start_idx = current_microseason(d).index
    probe = d
    for days in range(1, 10):
        probe = d + timedelta(days=days)
        if current_microseason(probe).index != start_idx:
            return probe, days
    raise RuntimeError("no microseason boundary found within 10 days (should be impossible)")
