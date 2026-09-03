"""Current weather for the 天 module (Open-Meteo, no API key needed).

Ambient info only, not a forecast: temperature, a short condition word,
humidity, precipitation chance -- deliberately not duplicating anything the
i3bar already shows.

Failure handling: `get_weather()` never raises. It tries one live fetch;
on any failure (network down, timeout, bad response) it falls back to the
last successful response cached on disk; if neither is available it
returns None, and the caller (renderer.py's `_weather_svg`) omits the
module entirely rather than showing a placeholder.
"""
import json
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_CACHE_PATH = Path.home() / ".local" / "state" / "jikan" / "weather_cache.json"

_API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,relative_humidity_2m,precipitation_probability,weather_code"
    "&timezone=auto"
)

# WMO weather_code -> short Japanese condition label. Every label used here
# has an entry in data/kanji_glossary.json.
_CONDITION_JA = {
    0: "晴", 1: "晴", 2: "多雲", 3: "曇",
    45: "霧", 48: "霧",
    51: "霧雨", 53: "霧雨", 55: "霧雨", 56: "霧雨", 57: "霧雨",
    61: "雨", 63: "雨", 65: "雨", 66: "雨", 67: "雨",
    71: "雪", 73: "雪", 75: "雪", 77: "雪",
    80: "にわか雨", 81: "にわか雨", 82: "にわか雨",
    85: "雪", 86: "雪",
    95: "雷雨", 96: "雷雨", 99: "雷雨",
}


@dataclass(frozen=True)
class WeatherInfo:
    temp_c: float
    condition_code: int
    condition_ja: str
    humidity_pct: int
    precip_probability_pct: int
    fetched_at: str  # ISO timestamp -- cache age is visible/debuggable
    is_cached: bool = False


def _condition_label(code: int) -> str:
    return _CONDITION_JA.get(code, "不明")


def fetch_weather(lat: float, lon: float, timeout: float = 4.0) -> WeatherInfo:
    """One live Open-Meteo call. Raises on any failure -- the caller
    (get_weather) decides the fallback."""
    url = _API_URL.format(lat=lat, lon=lon)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)
    current = payload["current"]
    code = int(current["weather_code"])
    return WeatherInfo(
        temp_c=round(float(current["temperature_2m"])),
        condition_code=code,
        condition_ja=_condition_label(code),
        humidity_pct=round(float(current["relative_humidity_2m"])),
        precip_probability_pct=round(float(current["precipitation_probability"])),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        is_cached=False,
    )


def _save_cache(info: WeatherInfo, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(asdict(info), ensure_ascii=False), encoding="utf-8")


def load_cached_weather(cache_path: Path = DEFAULT_CACHE_PATH) -> "WeatherInfo | None":
    """Pure read, no network -- used by explain.py so the popup reflects
    exactly what the wallpaper's last render showed, rather than
    triggering its own (possibly different) live fetch."""
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        raw["is_cached"] = True
        return WeatherInfo(**raw)
    except Exception:
        return None


def get_weather(lat: float, lon: float, cache_path: Path = DEFAULT_CACHE_PATH,
                 timeout: float = 4.0) -> "WeatherInfo | None":
    """Fetch-with-fallback: live data if reachable, else the last
    successful response, else None (module omitted entirely). Never
    raises -- this is the only function main.py should call."""
    try:
        info = fetch_weather(lat, lon, timeout=timeout)
        _save_cache(info, cache_path)
        return info
    except Exception:
        return load_cached_weather(cache_path)
