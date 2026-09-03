"""Sunrise/sunset/solar-geometry computation for the 24-hour day bar and the
日 (solar geometry) module.

Reuses seasons.py's Julian-day machinery rather than duplicating it.
Low-precision solar position (~0.01 degree accuracy) -- same tolerance
seasons.py already uses for solar terms, appropriate for a decorative
wallpaper element, not for navigation. Verified against real published
sunrise/sunset times for a mid-latitude reference location on 2026-09-01 --
within a few minutes, the expected error band for this class of simplified
formula.
"""
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from seasons import _local_noon_as_utc, _to_julian_day

_OBLIQUITY_J2000 = 23.439291  # degrees, obliquity of the ecliptic at J2000
_ZENITH_SUNRISE_SUNSET = 90.833  # accounts for atmospheric refraction + solar disk radius


def _sun_apparent_ecliptic_longitude(dt_utc: datetime) -> tuple[float, float]:
    """Returns (apparent_ecliptic_longitude_deg, t) -- `t` (Julian centuries
    since J2000) is returned too since _solar_declination_and_eot needs it
    again for the equation-of-time terms and moon_position() needs the
    longitude alone, so both can share this one computation instead of
    each re-deriving l0/m/c by hand."""
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
    lam = (l_true - 0.00569 - 0.00478 * math.sin(math.radians(omega))) % 360.0
    return lam, t


def _solar_declination_and_eot(dt_utc: datetime) -> tuple[float, float]:
    """Returns (declination_deg, equation_of_time_minutes)."""
    lam, t = _sun_apparent_ecliptic_longitude(dt_utc)
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t**2
    m = 357.52911 + 35999.05029 * t - 0.0001537 * t**2
    m_rad = math.radians(m)

    eps = _OBLIQUITY_J2000 - 0.0130042 * t
    eps_rad = math.radians(eps)

    e = 0.016708634 - 0.000042037 * t - 0.0000001267 * t**2
    y = math.tan(eps_rad / 2) ** 2
    l0_rad = math.radians(l0)
    eot_deg = (
        y * math.sin(2 * l0_rad)
        - 2 * e * math.sin(m_rad)
        + 4 * e * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * e * e * math.sin(2 * m_rad)
    )
    eot_minutes = 4 * math.degrees(eot_deg)

    decl = math.degrees(math.asin(math.sin(eps_rad) * math.sin(math.radians(lam))))
    return decl, eot_minutes


def _horizontal_from_equatorial(decl_deg: float, lat_deg: float, hour_angle_deg: float) -> tuple[float, float]:
    """(declination, observer latitude, hour angle) -> (azimuth_deg,
    altitude_deg). Azimuth clockwise from true north (0=N/90=E/180=S/
    270=W, same convention as _solar_azimuth), fully resolved via atan2 --
    unlike _solar_azimuth's fixed-horizon-altitude formula (only valid at
    the sunrise/sunset crossing), this works for any altitude at any
    moment, so it backs both solar_position() and moon_position()."""
    lat, decl, h = math.radians(lat_deg), math.radians(decl_deg), math.radians(hour_angle_deg)
    alt = math.asin(math.sin(decl) * math.sin(lat) + math.cos(decl) * math.cos(lat) * math.cos(h))
    sin_az = -math.cos(decl) * math.sin(h) / math.cos(alt)
    cos_az = (math.sin(decl) - math.sin(alt) * math.sin(lat)) / (math.cos(alt) * math.cos(lat))
    az = math.degrees(math.atan2(sin_az, cos_az)) % 360.0
    return az, math.degrees(alt)


def _greenwich_sidereal_time_deg(dt_utc: datetime) -> float:
    """GMST in degrees -- only needed by moon_position() (the moon's
    apparent motion isn't locked to clock time the way the sun's is, so
    its hour angle needs right ascension + sidereal time; the sun's
    functions above sidestep this entirely by working in solar-time-of-day
    directly)."""
    jd = _to_julian_day(dt_utc)
    t = (jd - 2451545.0) / 36525.0
    gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t**2 - t**3 / 38710000.0
    return gmst % 360.0


def _sun_event(d: date, lat: float, lon: float, rising: bool) -> datetime:
    noon_utc = _local_noon_as_utc(d)
    decl, eot = _solar_declination_and_eot(noon_utc)

    # Solar noon in UTC minutes-of-day. Longitude positive=East, negative=West;
    # a location east of Greenwich reaches solar noon earlier in UTC.
    solar_noon_utc_minutes = 12 * 60 - 4 * lon - eot

    cos_h = (
        math.cos(math.radians(_ZENITH_SUNRISE_SUNSET))
        - math.sin(math.radians(lat)) * math.sin(math.radians(decl))
    ) / (math.cos(math.radians(lat)) * math.cos(math.radians(decl)))
    cos_h = max(-1.0, min(1.0, cos_h))  # clamp: polar day/night edge cases
    h_deg = math.degrees(math.acos(cos_h))

    event_utc_minutes = solar_noon_utc_minutes + (-4 * h_deg if rising else 4 * h_deg)

    base_utc_midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    event_utc = base_utc_midnight + timedelta(minutes=event_utc_minutes)
    return event_utc.astimezone()  # convert to this machine's local timezone


def sunrise(d: date, lat: float, lon: float) -> datetime:
    return _sun_event(d, lat, lon, rising=True)


def sunset(d: date, lat: float, lon: float) -> datetime:
    return _sun_event(d, lat, lon, rising=False)


def _solar_azimuth(decl_deg: float, lat_deg: float, altitude_deg: float, morning: bool) -> float:
    """Standard solar azimuth formula (degrees, clockwise from true north,
    0=N/90=E/180=S/270=W). `morning` picks which half of the acos
    ambiguity applies (before vs. after solar noon)."""
    lat, decl, alt = math.radians(lat_deg), math.radians(decl_deg), math.radians(altitude_deg)
    cos_az = (math.sin(decl) - math.sin(alt) * math.sin(lat)) / (math.cos(alt) * math.cos(lat))
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.degrees(math.acos(cos_az))
    return az if morning else 360.0 - az


@dataclass(frozen=True)
class SolarGeometry:
    sunrise_time: datetime
    sunrise_azimuth_deg: float
    solar_noon_time: datetime
    solar_noon_altitude_deg: float
    solar_noon_direction: str  # "north" | "south" -- which way an observer looks at local noon
    sunset_time: datetime
    sunset_azimuth_deg: float


def solar_geometry(d: date, lat: float, lon: float) -> SolarGeometry:
    """Sunrise/solar-noon/sunset times, azimuths, and solar-noon altitude +
    north/south direction -- everything the 日 module and its house/compass
    diagram need. Pure function of date/lat/lon, no I/O, no external API --
    reuses the same declination/equation-of-time machinery as sunrise()/
    sunset() above.

    Near the equator (roughly within +/-23.5 degrees latitude, the tropics)
    solar declination crosses the observer's latitude twice a year, so
    solar_noon_direction genuinely flips between "north" and "south" over
    the year -- this isn't always the same answer the way it would be at
    higher latitudes.
    """
    noon_utc = _local_noon_as_utc(d)
    decl, eot = _solar_declination_and_eot(noon_utc)

    horizon_altitude = 90.0 - _ZENITH_SUNRISE_SUNSET  # ~-0.833 deg
    sr = sunrise(d, lat, lon)
    ss = sunset(d, lat, lon)
    sr_az = _solar_azimuth(decl, lat, horizon_altitude, morning=True)
    ss_az = _solar_azimuth(decl, lat, horizon_altitude, morning=False)

    solar_noon_utc_minutes = 12 * 60 - 4 * lon - eot
    base_utc_midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    solar_noon = (base_utc_midnight + timedelta(minutes=solar_noon_utc_minutes)).astimezone()

    altitude = 90.0 - abs(lat - decl)
    direction = "north" if decl > lat else "south"

    return SolarGeometry(
        sunrise_time=sr, sunrise_azimuth_deg=sr_az,
        solar_noon_time=solar_noon, solar_noon_altitude_deg=altitude, solar_noon_direction=direction,
        sunset_time=ss, sunset_azimuth_deg=ss_az,
    )


def solar_position(dt: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Current (azimuth_deg, altitude_deg) of the sun at an arbitrary
    moment -- generalizes solar_geometry()'s fixed sunrise/noon/sunset
    events to any clock time, for the house diagram's live "current sun
    position" marker and illuminated-facade highlight."""
    # .astimezone() on a naive datetime presumes it's already in this
    # system's local timezone (per the stdlib docs) and converts correctly
    # from there -- unlike a bare .replace(tzinfo=utc), which would just
    # relabel the local wall-clock value as UTC and shift the result by
    # the system's UTC offset (confirmed live: 5 hours off on this
    # UTC-5 machine).
    dt_utc = dt.astimezone(timezone.utc)
    decl, eot = _solar_declination_and_eot(dt_utc)
    solar_noon_utc_minutes = 12 * 60 - 4 * lon - eot
    now_utc_minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60
    # 4 minutes of time = 1 degree of hour angle; wrap to +/-720 minutes
    # (+/-180 degrees) first so a `now` near UTC midnight doesn't produce
    # a huge spurious hour angle against a solar-noon time on the other
    # side of the day boundary.
    hour_angle_deg = ((now_utc_minutes - solar_noon_utc_minutes + 720) % 1440 - 720) * 0.25
    return _horizontal_from_equatorial(decl, lat, hour_angle_deg)
