#!/usr/bin/env python3
"""Print readings/meanings for everything currently shown on the jikan
wallpaper -- a study aid, triggered from the i3bar review button.

Pulls from the same calendar_jp.py/seasons.py functions the renderer uses,
so it can never drift out of sync with what's actually on screen.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

from calendar_jp import month_notation, pick_kanji_of_day, weekday_kanji_header
from config import load_config
from moon_phase import moon_phase
from palette import SEGMENT_LABEL_JA, SEGMENT_ORDER
from rainfall import load_rainfall_climatology
from renderer import _closest_facade_label, _is_night, resolve_palette
from seasons import current_microseason, current_term
from solar_times import solar_geometry
from weather import DEFAULT_CACHE_PATH as WEATHER_CACHE_PATH
from weather import load_cached_weather

GLOSSARY_PATH = Path(__file__).parent / "data" / "kanji_glossary.json"


def _ansi_fg(hexcolor: str) -> str:
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD = "\x1b[1m"

_WEEKDAY_GLOSS = {
    "月": "getsu / Monday (moon)", "火": "ka / Tuesday (fire)",
    "水": "sui / Wednesday (water)", "木": "moku / Thursday (wood)",
    "金": "kin / Friday (metal/gold)", "土": "do / Saturday (earth)",
    "日": "nichi / Sunday (sun)",
}


def _load_glossary() -> dict:
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _gloss(glossary: dict, ch: str) -> str:
    entry = glossary.get(ch)
    if entry is None:
        return "(no gloss available)"
    return f'{entry["reading"]} -- {entry["meaning"]}'


def explain(target_date: date, cfg, now: datetime | None = None) -> str:
    """Inherits the current segment's palette (via renderer.resolve_palette,
    the exact same function the wallpaper itself uses) so this popup feels
    like part of the same system rather than an unrelated plain terminal --
    headings/labels colored with the live accent, via ANSI truecolor."""
    palette, active_idx, sr_hour, ss_hour = resolve_palette(target_date, now, cfg)
    accent = _ansi_fg(palette.accent)
    heading = _ansi_fg(palette.primary) + _ANSI_BOLD
    label = _ansi_fg(palette.secondary)
    seg_name = SEGMENT_ORDER[active_idx] if active_idx is not None else None
    seg_suffix = f"  [{SEGMENT_LABEL_JA[seg_name]} / {seg_name}]" if seg_name else ""

    glossary = _load_glossary()
    lines = [
        f"{heading}today's kanji ({target_date.isoformat()}){seg_suffix}{_ANSI_RESET}",
        f"{accent}{'=' * 40}{_ANSI_RESET}",
        "",
    ]

    weekday_ch = weekday_kanji_header()[target_date.weekday()]
    lines.append(f"{label}weekday{_ANSI_RESET}   {weekday_ch}   {_WEEKDAY_GLOSS.get(weekday_ch, '(no gloss)')}")
    lines.append(f"{label}month{_ANSI_RESET}     {month_notation(target_date)}")
    lines.append("")

    if cfg.kanji.mode == "manual":
        kanji_ch = cfg.kanji.manual
    else:
        kanji_ch = pick_kanji_of_day(target_date, cfg.kanji.list)
    lines.append(f"{label}kanji of day{_ANSI_RESET}   {kanji_ch}   {_gloss(glossary, kanji_ch)}")
    lines.append("")

    term = current_term(target_date)
    lines.append(
        f"{label}solar term (二十四節気){_ANSI_RESET}   {term.name_ja}   "
        f"{term.reading_romaji} -- {term.gloss_en}"
    )

    micro = current_microseason(target_date)
    micro_gloss = f" -- {micro.gloss_en}" if micro.gloss_en else ""
    lines.append(
        f"{label}microseason (七十二候){_ANSI_RESET}    {micro.sub_label_ja} {micro.name_ja}   "
        f"{micro.reading_romaji}{micro_gloss}"
    )
    lines.append("")

    lines.append(f"{label}day bar labels:{_ANSI_RESET}")
    for ch in ["眠", "日出", "起", "始", "昼", "終", "日没"]:
        lines.append(f"  {ch}   {_gloss(glossary, ch)}")
    lines.append("")

    # Weather (天): read-only, no live fetch here -- reflects exactly what
    # the wallpaper's last render showed rather than a possibly-different
    # independent lookup (see weather.py's load_cached_weather docstring).
    weather = load_cached_weather(WEATHER_CACHE_PATH)
    if weather is not None:
        stale_note = "  (cached)" if weather.is_cached else ""
        lines.append(f"{label}weather (天){_ANSI_RESET}{stale_note}")
        lines.append(f"  天      {_gloss(glossary, '天')}")
        lines.append(f"  {weather.temp_c:.0f}°  {weather.condition_ja}   {_gloss(glossary, weather.condition_ja)}")
        lines.append(f"  湿 {weather.humidity_pct}%   {_gloss(glossary, '湿')}")
        lines.append(f"  雨 {weather.precip_probability_pct}%   {_gloss(glossary, '雨')}")
        lines.append("")

    # Rainfall cycle (雨): a climatological average, not a forecast.
    climatology = load_rainfall_climatology(Path(__file__).parent / cfg.rainfall.data_path)
    if climatology is not None:
        month_mm = climatology.monthly_mm[target_date.month - 1]
        lines.append(f"{label}yearly rainfall cycle (雨){_ANSI_RESET}")
        lines.append(f"  雨      {_gloss(glossary, '雨')}")
        lines.append(f"  this month's climatological average: {month_mm:.0f}mm")
        lines.append(f"  source: {climatology.source}")
        lines.append("")

    # Solar geometry (日): always computable, pure local astronomy.
    geometry = solar_geometry(target_date, cfg.location.latitude, cfg.location.longitude)
    noon_dir_ch = "北" if geometry.solar_noon_direction == "north" else "南"
    morning_face = _closest_facade_label(geometry.sunrise_azimuth_deg, cfg.house.rotation_deg)
    evening_face = _closest_facade_label(geometry.sunset_azimuth_deg, cfg.house.rotation_deg)
    lines.append(f"{label}solar geometry (日){_ANSI_RESET}")
    lines.append(f"  日      {_gloss(glossary, '日')}")
    lines.append(f"  日出    {_gloss(glossary, '日出')}   "
                 f"{geometry.sunrise_time.strftime('%H:%M')}, azimuth {geometry.sunrise_azimuth_deg:.0f}°")
    lines.append(f"  南中    {_gloss(glossary, '南中')}   "
                 f"{geometry.solar_noon_time.strftime('%H:%M')}, 高度{geometry.solar_noon_altitude_deg:.0f}° "
                 f"({_gloss(glossary, '高度')}), {noon_dir_ch} ({_gloss(glossary, noon_dir_ch)})")
    lines.append(f"  日没    {_gloss(glossary, '日没')}   "
                 f"{geometry.sunset_time.strftime('%H:%M')}, azimuth {geometry.sunset_azimuth_deg:.0f}°")

    if _is_night(target_date, now, sr_hour, ss_hour):
        # Sun's below the horizon right now -- the wallpaper shows the moon
        # phase instead of the house/compass diagram (see renderer.py's
        # _is_night / _moon_diagram_svg).
        moon = moon_phase(target_date)
        lines.append(f"  月      {_gloss(glossary, '月')}")
        lines.append(f"  {moon.name_ja}   {_gloss(glossary, moon.name_ja)}   "
                     f"{moon.illumination_pct:.0f}% illuminated, "
                     f"{'waxing' if moon.waxing else 'waning'}")
    else:
        lines.append(f"  house orientation: {morning_face}面 {_gloss(glossary, '朝陽')} / "
                     f"{evening_face}面 {_gloss(glossary, '夕陽')}  ({_gloss(glossary, '面')})")
        for ch in ["北", "東", "南", "西"]:
            lines.append(f"  {ch}   {_gloss(glossary, ch)}")
    lines.append("")

    lines.append(f"{label}lock screen (i3lock background, jikan-lock.png):{_ANSI_RESET}")
    lines.append(f"  施錠中   {_gloss(glossary, '施錠中')}")
    for ch in ["施", "錠"]:
        lines.append(f"  {ch}   {_gloss(glossary, ch)}")

    return "\n".join(lines)


def main() -> int:
    from config import DEFAULT_CONFIG_PATH
    cfg = load_config(DEFAULT_CONFIG_PATH)
    print(explain(date.today(), cfg, now=datetime.now()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
