"""Load config.toml into a typed Config object with validation."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.toml"


@dataclass(frozen=True)
class DisplayConfig:
    width: int = 2560
    height: int = 1440


@dataclass(frozen=True)
class FontsConfig:
    display: list[str] = field(default_factory=lambda: ["Dela Gothic One", "Zen Kaku Gothic New", "Noto Sans JP"])
    text: list[str] = field(default_factory=lambda: ["Zen Kaku Gothic New", "Noto Sans JP"])


@dataclass(frozen=True)
class KanjiConfig:
    mode: str = "daily"  # "daily" | "manual"
    manual: str = "整"
    list: list[str] = field(default_factory=lambda: ["作", "読", "整", "技", "静", "遊", "鍛", "学", "休"])


@dataclass(frozen=True)
class ModulesConfig:
    calendar: bool = True
    kanji: bool = True
    solar_term: bool = True
    microseason: bool = True
    year_progress: bool = True
    day_bar: bool = True
    english_translation: bool = False
    weather: bool = True
    rainfall: bool = True
    solar_geometry: bool = True


@dataclass(frozen=True)
class LocationConfig:
    latitude: float = 35.6762
    longitude: float = 139.6503


@dataclass(frozen=True)
class ScheduleConfig:
    wake: str = "08:00"
    work_start: str = "10:00"
    lunch: str = "12:00"
    lunch_duration_minutes: int = 60
    work_end: str = "19:00"
    sleep: str = "00:00"


@dataclass(frozen=True)
class WallpaperConfig:
    setter: str = "feh"


@dataclass(frozen=True)
class WeatherConfig:
    provider: str = "open-meteo"
    timeout_seconds: float = 4.0


@dataclass(frozen=True)
class RainfallConfig:
    data_path: str = "data/rainfall_climatology.json"


@dataclass(frozen=True)
class HouseConfig:
    # Rotation of the house's cardinal faces, degrees clockwise from true
    # north (0 = perfectly N/E/S/W-aligned rectangle). Only used by the
    # solar-geometry house/compass diagram.
    rotation_deg: float = 0.0


@dataclass(frozen=True)
class Config:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    fonts: FontsConfig = field(default_factory=FontsConfig)
    kanji: KanjiConfig = field(default_factory=KanjiConfig)
    modules: ModulesConfig = field(default_factory=ModulesConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    wallpaper: WallpaperConfig = field(default_factory=WallpaperConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    rainfall: RainfallConfig = field(default_factory=RainfallConfig)
    house: HouseConfig = field(default_factory=HouseConfig)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        return Config()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    if raw.get("kanji", {}).get("mode") not in (None, "daily", "manual"):
        raise ValueError(f"[kanji].mode must be 'daily' or 'manual', got {raw['kanji']['mode']!r}")

    return Config(
        display=DisplayConfig(**raw.get("display", {})),
        fonts=FontsConfig(**raw.get("fonts", {})),
        kanji=KanjiConfig(**raw.get("kanji", {})),
        modules=ModulesConfig(**raw.get("modules", {})),
        location=LocationConfig(**raw.get("location", {})),
        schedule=ScheduleConfig(**raw.get("schedule", {})),
        wallpaper=WallpaperConfig(**raw.get("wallpaper", {})),
        weather=WeatherConfig(**raw.get("weather", {})),
        rainfall=RainfallConfig(**raw.get("rainfall", {})),
        house=HouseConfig(**raw.get("house", {})),
    )
