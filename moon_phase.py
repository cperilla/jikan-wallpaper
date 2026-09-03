"""Moon phase for the night-time swap of the solar-geometry diagram (日
module): once the sun is below the horizon the house/compass diagram isn't
very informative, so it's replaced by the current moon phase instead.

Evaluated at local noon of the target date (same convention as
seasons.py's solar-term math) rather than the live moment -- stable across
the 15-minute regeneration cycle, consistent with the rest of this
project's date-based (not moment-based) astronomy. Pure function of date,
no ephemeris file -- same low-precision-formula philosophy as the rest of
this project (accurate to within roughly a day of true phase boundaries,
plenty for a decorative wallpaper icon, not for eclipse prediction).
"""
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from seasons import _local_noon_as_utc, _to_julian_day
from solar_times import (
    _OBLIQUITY_J2000,
    _greenwich_sidereal_time_deg,
    _horizontal_from_equatorial,
    _sun_apparent_ecliptic_longitude,
)

_SYNODIC_MONTH_DAYS = 29.530588853
# A well-known reference new moon: 2000-01-06 18:14 UTC (JD computed via
# this project's own _to_julian_day, not hand-derived, so it's consistent
# with the rest of the Julian-day machinery).
_REFERENCE_NEW_MOON_JD = 2451550.2597

# Boundaries are upper-exclusive fractions of the synodic month; names
# deliberately mix the 4 standard quarter-phase terms with plain
# descriptive terms for the in-between waxing/waning buckets, to avoid
# misusing a specific traditional festival term (e.g. 十三夜) for a
# generic bucket it doesn't actually describe.
_PHASE_NAMES_JA = [
    (0.0625, "新月"),
    (0.1875, "三日月"),
    (0.3125, "上弦の月"),
    (0.4375, "満ちゆく月"),
    (0.5625, "満月"),
    (0.6875, "欠けゆく月"),
    (0.8125, "下弦の月"),
    (0.9375, "有明の月"),
]


@dataclass(frozen=True)
class MoonPhase:
    phase_fraction: float    # 0..1, 0/1=new, 0.5=full
    illumination_pct: float  # 0..100
    name_ja: str
    waxing: bool


def _phase_name(p: float) -> str:
    for boundary, name in _PHASE_NAMES_JA:
        if p < boundary:
            return name
    return "新月"  # wraps back around to new moon just before p=1.0


def moon_phase(d: date) -> MoonPhase:
    dt_utc = _local_noon_as_utc(d)
    jd = _to_julian_day(dt_utc)
    days_since_reference = jd - _REFERENCE_NEW_MOON_JD
    p = (days_since_reference % _SYNODIC_MONTH_DAYS) / _SYNODIC_MONTH_DAYS

    illumination_pct = (1 - math.cos(2 * math.pi * p)) / 2 * 100
    return MoonPhase(
        phase_fraction=p,
        illumination_pct=illumination_pct,
        name_ja=_phase_name(p),
        waxing=p < 0.5,
    )


def moon_position(dt: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Approximate current (azimuth_deg, altitude_deg) of the moon, for the
    house diagram's live moon-position marker and illuminated-facade
    highlight at night.

    Low-precision: derives the moon's apparent ecliptic longitude as the
    sun's apparent ecliptic longitude plus the current elongation
    (phase_fraction * 360 degrees -- the same sun/moon angular-separation
    relationship moon_phase()'s own illumination formula already relies
    on), and ignores the moon's ~5 degree ecliptic-latitude wobble
    entirely. Good enough to place a decorative wallpaper marker on a
    compass ring (typically within a few degrees of the moon's true
    azimuth); not for real astronomy or eclipse prediction, matching this
    project's existing low-precision-formula philosophy throughout."""
    # .astimezone() on a naive datetime presumes local system time and
    # converts correctly -- see solar_position()'s identical fix in
    # solar_times.py for why a bare .replace(tzinfo=utc) is wrong here.
    dt_utc = dt.astimezone(timezone.utc)

    jd = _to_julian_day(dt_utc)
    days_since_reference = jd - _REFERENCE_NEW_MOON_JD
    p = (days_since_reference % _SYNODIC_MONTH_DAYS) / _SYNODIC_MONTH_DAYS

    sun_lam, _t = _sun_apparent_ecliptic_longitude(dt_utc)
    moon_lam = math.radians((sun_lam + p * 360.0) % 360.0)
    eps = math.radians(_OBLIQUITY_J2000)

    decl = math.degrees(math.asin(math.sin(eps) * math.sin(moon_lam)))
    ra = math.degrees(math.atan2(math.sin(moon_lam) * math.cos(eps), math.cos(moon_lam))) % 360.0

    lst = (_greenwich_sidereal_time_deg(dt_utc) + lon) % 360.0
    hour_angle_deg = (lst - ra + 540.0) % 360.0 - 180.0  # wrap to +/-180

    return _horizontal_from_equatorial(decl, lat, hour_angle_deg)


def moon_rise_set_around(dt: datetime, lat: float, lon: float) -> tuple[datetime | None, datetime | None]:
    """Moonrise immediately before `dt` and moonset immediately after it --
    the bracketing edges of whichever continuous above-horizon window `dt`
    falls in. Only meaningful to call when the moon is actually up at `dt`
    (nothing to bracket otherwise).

    Found by numeric sampling + linear interpolation of moon_position()'s
    altitude over a +/-15h window, since (unlike the sun, which has a
    closed-form hour-angle formula) the moon's rise/set times need an
    actual search -- its position moves too fast relative to the clock to
    invert analytically at this project's precision tier. +/-15h safely
    covers a full above-horizon session even on nights it runs long (this
    project's other durations top out well under that), without the cost
    of scanning a much wider window."""
    step = timedelta(minutes=5)
    t = dt - timedelta(hours=15)
    end = dt + timedelta(hours=15)
    rises, sets = [], []
    prev_t, prev_alt = None, None
    while t <= end:
        _, alt = moon_position(t, lat, lon)
        if prev_alt is not None:
            if prev_alt <= 0 < alt:
                frac = -prev_alt / (alt - prev_alt)
                rises.append(prev_t + (t - prev_t) * frac)
            elif prev_alt > 0 >= alt:
                frac = prev_alt / (prev_alt - alt)
                sets.append(prev_t + (t - prev_t) * frac)
        prev_t, prev_alt = t, alt
        t += step
    rise = max((r for r in rises if r <= dt), default=None)
    set_ = min((st for st in sets if st >= dt), default=None)
    return rise, set_
