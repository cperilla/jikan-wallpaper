"""SVG composition builder + rasterizer.

Pillow is used only offline, as a font-metrics oracle (to figure out what
font-size makes a glyph N pixels tall) -- it never renders the final image.
Rasterization is done via the inkscape CLI (see rasterize() docstring for why
not cairosvg).

Theming: every module in this file is driven by a `palette.Palette` --
a full 8-token per-segment palette (background/ghost/tertiary/secondary/
primary/accent/glow/timeline_active), smoothly interpolated in OKLab across
segment boundaries (see palette.py). There is no more per-element "static
config color vs. one live accent" split -- the active day segment drives
the whole scene, at a hierarchy of strengths (background/ghost/term/now-
marker strongly themed; calendar/microseason moderately; ticks/grid lightly).
"""
import functools
import math
import os
import subprocess
import tempfile
from datetime import date, datetime

import numpy as np
from PIL import Image, ImageFont

from calendar_jp import MONTH_KANJI_DIGITS, month_grid, month_notation, pick_kanji_of_day, weekday_kanji_header
from config import Config
from generative import build_variation, make_rng
from moon_phase import MoonPhase, moon_phase, moon_position, moon_rise_set_around
from palette import Palette, SEGMENT_ORDER, SEGMENT_PALETTES, current_palette, current_segment_name, hex_to_oklab, lerp_oklab, oklab_to_hex
from rainfall import RainfallClimatology
from currency import CurrencyInfo
from seasons import current_microseason, current_term, next_term_boundary
from solar_times import SolarGeometry, solar_geometry, solar_position, sunrise, sunset
from weather import WeatherInfo

# dominant-baseline="central" centers on font ascent/descent metrics, not
# glyph ink bounds -- empirically ~0.8% of font-size too high under
# inkscape's renderer (measured directly against inkscape, NOT cairosvg --
# the two renderers have different baseline-centering behavior).
_BASELINE_CENTRAL_BIAS = -0.0083

# Fallback when rendering for a date that isn't "now" (--date/--output
# testing) -- "day" is the most neutral/stable segment, a sensible default.
_FALLBACK_SEGMENT = "day"


def _parse_hhmm(s: str) -> float:
    h, m = s.split(":")
    return int(h) + int(m) / 60.0


@functools.lru_cache(maxsize=32)
def _resolve_font_path(family: str) -> str:
    """First installed font file matching `family`, via fontconfig."""
    result = subprocess.run(
        ["fc-match", "-f", "%{file}", family],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _font_family_css(families: list[str]) -> str:
    """CSS-style comma-separated fallback list for the SVG font-family attribute."""
    return ", ".join(families)


def _glyph_height_ratio(font_path: str, sample_char: str, ref_size: int = 1000) -> float:
    font = ImageFont.truetype(font_path, ref_size)
    bbox = font.getbbox(sample_char)
    return (bbox[3] - bbox[1]) / ref_size


def _font_size_for_height(font_path: str, sample_char: str, target_px: float) -> float:
    ratio = _glyph_height_ratio(font_path, sample_char)
    return target_px / ratio


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Semantic trend tints for the 為替 module: up = green, down = red, flat =
# gray. The one place this project uses fixed non-palette hues, because
# financial up/down carries a near-universal color meaning a mood-shifting
# palette shouldn't override. Muted so they sit in the low-contrast look.
_TREND_COLORS = {
    "up": "#5FAF7A",
    "down": "#C25B5B",
    "flat": "#8A8A8A",
}


def _trend_color(trend: str, palette: "Palette") -> str:
    return _TREND_COLORS.get(trend, _TREND_COLORS["flat"])


def _blend_hex(hexcolor: str, mix_with: str | None = None, mix_t: float = 0.0) -> str:
    """hex -> hex, optionally OKLab-blended toward `mix_with` first (used
    for "moderately"/"lightly" themed elements that shouldn't take the
    full palette token, just a nudge toward it).

    Deliberately returns a *solid* hex color rather than an rgba(...)
    string: inkscape's CLI PNG export (this project's whole rasterize()
    path) silently fails to honor embedded-alpha rgba() colors in SVG
    fill/stroke attributes -- strokes render fully invisible, fills
    render solid black, verified via isolated test SVGs at multiple
    alpha/width values. A plain solid color plus a *separate*
    fill-opacity/stroke-opacity attribute (see callers) renders the
    mathematically-correct alpha blend instead."""
    if mix_with is not None and mix_t > 0:
        L1, a1, b1 = hex_to_oklab(hexcolor)
        L2, a2, b2 = hex_to_oklab(mix_with)
        hexcolor = oklab_to_hex(L1 + (L2 - L1) * mix_t, a1 + (a2 - a1) * mix_t, b1 + (b2 - b1) * mix_t)
    return hexcolor


class _Layout:
    """Precomputed pixel positions, scaled from the 2560x1440 reference
    design to whatever canvas size the config specifies."""

    def __init__(self, cfg: Config):
        w, h = cfg.display.width, cfg.display.height
        sx, sy = w / 2560, h / 1440
        self.w, self.h = w, h
        self.kanji_cx = 1590 * sx
        self.kanji_cy = 760 * sy
        self.kanji_target_h = h * 0.62
        self.left_x = 140 * sx
        self.term_y = 260 * sy
        self.hair_w = 420 * sx
        self.micro_y = 420 * sy
        self.cal_x0 = 140 * sx
        self.cal_y0 = 1120 * sy
        self.yp_x0, self.yp_y0 = 220 * sx, 120 * sy
        self.yp_x1, self.yp_y1 = w - 220 * sx, h - 140 * sy
        self.scale = min(sx, sy)  # for font sizes, keep proportions sane
        self.bar_x = w - 90 * sx
        self.bar_y0, self.bar_y1 = 120 * sy, 1320 * sy

        # Environmental modules (weather/rainfall/solar geometry): the gap
        # between the giant kanji's right shoulder and the day bar. env_x1
        # is kept well clear of the day bar's live "now" time label (which
        # can extend to roughly bar_x-125 at its widest, e.g. "14:00"), not
        # just the narrower always-there hour-tick numbers -- a wider
        # column previously let the solar-geometry compass ring's "東"
        # label collide with that live label whenever "now" fell in the
        # diagram's vertical band. env_x0 widens the column by extending
        # LEFT instead (into the giant kanji's own footprint) rather than
        # right -- ghost kanji is designed to be seen through, same way
        # the term block/calendar already sit in front of it, so this side
        # has no collision risk to budget around.
        self.env_x0 = 1980 * sx
        self.env_x1 = 2320 * sx
        self.currency_y0 = 140 * sy
        self.weather_y0 = 330 * sy
        self.rainfall_y0 = 460 * sy
        self.solar_y0 = 690 * sy


def _year_progress_svg(target_date: date, variation, layout: _Layout, palette: Palette) -> str:
    year = target_date.year
    day_of_year = target_date.timetuple().tm_yday

    cols = 31
    rows = 12
    col_pitch = (layout.yp_x1 - layout.yp_x0) / (cols - 1)
    row_pitch = (layout.yp_y1 - layout.yp_y0) / (rows - 1)
    radius = 2.6 * layout.scale

    import calendar as _cal
    days_in_month = [_cal.monthrange(year, m)[1] for m in range(1, 13)]

    # Elapsed dots: moderately themed (nudged toward palette.accent).
    # Future dots: barely themed (tiny nudge toward palette.tertiary only).
    elapsed_color = (_blend_hex("#B8BCBE", mix_with=palette.accent, mix_t=0.30), 0.20)
    future_color = (_blend_hex("#B8BCBE", mix_with=palette.tertiary, mix_t=0.12), 0.05)

    circles = []
    day_counter = 0
    for row in range(rows):
        ndays = days_in_month[row]
        phase = (variation.year_progress_row_phase + row * 0.13) % 1.0
        row_jitter_x = (phase - 0.5) * 6.0 * layout.scale
        for col in range(ndays):
            day_counter += 1
            elapsed = day_counter <= day_of_year
            color, alpha = elapsed_color if elapsed else future_color
            cx = layout.yp_x0 + col * col_pitch + row_jitter_x
            cy = layout.yp_y0 + row * row_pitch
            circles.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.2f}" '
                f'fill="{color}" fill-opacity="{alpha}"/>'
            )

    cx_center, cy_center = layout.w / 2, layout.h / 2
    group = "\n".join(circles)
    return (
        f'<g transform="rotate({variation.year_progress_rotation_deg:.3f} '
        f'{cx_center} {cy_center})">\n{group}\n</g>'
    )


def _kanji_svg(target_date: date, cfg: Config, variation, layout: _Layout, palette: Palette) -> str:
    if cfg.kanji.mode == "manual":
        ch = cfg.kanji.manual
    else:
        ch = pick_kanji_of_day(target_date, cfg.kanji.list)

    font_path = _resolve_font_path(cfg.fonts.display[0])
    font_family = _font_family_css(cfg.fonts.display)

    target_h = layout.kanji_target_h * variation.kanji_scale_pct
    size = _font_size_for_height(font_path, ch, target_h)

    cx = layout.kanji_cx
    cy = layout.kanji_cy + variation.kanji_baseline_shift_px * layout.scale - _BASELINE_CENTRAL_BIAS * size

    return (
        f'<text x="{cx:.1f}" y="{cy:.1f}" font-family="{font_family}" '
        f'font-size="{size:.1f}" fill="{palette.ghost}" text-anchor="middle" '
        f'dominant-baseline="central">{_esc(ch)}</text>'
    )


def _term_block_svg(target_date: date, cfg: Config, variation, layout: _Layout, palette: Palette) -> str:
    term = current_term(target_date)
    next_date, days_left = next_term_boundary(target_date)
    next_term = current_term(next_date)

    display_path = _resolve_font_path(cfg.fonts.display[0])
    display_family = _font_family_css(cfg.fonts.display)
    text_family = _font_family_css(cfg.fonts.text)

    x = layout.left_x
    y_term = layout.term_y
    term_size = _font_size_for_height(display_path, term.name_ja[0], 130 * layout.scale)

    y_hair = y_term + 40 * layout.scale + variation.guideline_y_offset_px * layout.scale
    hair_w = layout.hair_w
    hairline_color = _blend_hex(palette.tertiary)

    y_next = y_term + 90 * layout.scale
    next_size = 30 * layout.scale

    return "\n".join([
        f'<text x="{x:.1f}" y="{y_term:.1f}" font-family="{display_family}" '
        f'font-size="{term_size:.1f}" fill="{palette.accent}">{_esc(term.name_ja)}</text>',
        f'<line x1="{x:.1f}" y1="{y_hair:.1f}" x2="{x + hair_w:.1f}" y2="{y_hair:.1f}" '
        f'stroke="{hairline_color}" stroke-opacity="0.35" stroke-width="1"/>',
        f'<text x="{x:.1f}" y="{y_next:.1f}" font-family="{text_family}" '
        f'font-size="{next_size:.1f}" fill="{palette.secondary}">'
        f'次 {_esc(next_term.name_ja)} {days_left}日後</text>',
    ])


def _microseason_svg(target_date: date, cfg: Config, layout: _Layout, palette: Palette) -> str:
    micro = current_microseason(target_date)
    text_family = _font_family_css(cfg.fonts.text)
    x = layout.left_x
    y = layout.micro_y
    size = 58 * layout.scale

    lines = [
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{text_family}" '
        f'font-size="{size:.1f}" fill="{palette.secondary}">'
        f'{_esc(micro.sub_label_ja)}　{_esc(micro.name_ja)}</text>'
    ]
    if cfg.modules.english_translation and micro.gloss_en:
        lines.append(
            f'<text x="{x:.1f}" y="{y + 40 * layout.scale:.1f}" font-family="{text_family}" '
            f'font-size="{22 * layout.scale:.1f}" fill="{palette.tertiary}">{_esc(micro.gloss_en)}</text>'
        )
    return "\n".join(lines)


def _calendar_svg(target_date: date, cfg: Config, variation, layout: _Layout, palette: Palette) -> str:
    display_path = _resolve_font_path(cfg.fonts.display[0])
    display_family = _font_family_css(cfg.fonts.display)
    text_family = _font_family_css(cfg.fonts.text)

    x0 = layout.cal_x0 + variation.calendar_shift_x_px * layout.scale
    y0 = layout.cal_y0 + variation.calendar_shift_y_px * layout.scale

    month_size = _font_size_for_height(display_path, "9", 46 * layout.scale)
    header_y = y0
    weekday_y = y0 + 46 * layout.scale
    grid_start_y = y0 + 92 * layout.scale

    col_pitch = 46 * layout.scale
    row_pitch = 46 * layout.scale

    parts = [
        f'<text x="{x0:.1f}" y="{header_y:.1f}" font-family="{display_family}" '
        f'font-size="{month_size:.1f}" fill="{palette.primary}">{_esc(month_notation(target_date))}</text>'
    ]

    weekdays = weekday_kanji_header()
    for i, wd in enumerate(weekdays):
        wx = x0 + i * col_pitch
        parts.append(
            f'<text x="{wx:.1f}" y="{weekday_y:.1f}" font-family="{text_family}" '
            f'font-size="{26 * layout.scale:.1f}" fill="{palette.tertiary}">{wd}</text>'
        )

    grid = month_grid(target_date)
    for row_idx, week in enumerate(grid.weeks):
        wy = grid_start_y + row_idx * row_pitch
        for col_idx, day in enumerate(week):
            if day is None:
                continue
            wx = x0 + col_idx * col_pitch
            is_today = day == grid.today_day
            color = palette.primary if is_today else palette.tertiary
            parts.append(
                f'<text x="{wx:.1f}" y="{wy:.1f}" font-family="{text_family}" '
                f'font-size="{24 * layout.scale:.1f}" fill="{color}">{day}</text>'
            )
            if is_today:
                parts.append(
                    f'<line x1="{wx:.1f}" y1="{wy + 6 * layout.scale:.1f}" '
                    f'x2="{wx + 20 * layout.scale:.1f}" y2="{wy + 6 * layout.scale:.1f}" '
                    f'stroke="{palette.primary}" stroke-width="2"/>'
                )

    return "\n".join(parts)


def _weather_svg(cfg: Config, layout: _Layout, palette: Palette, weather: WeatherInfo | None) -> str:
    """天 module: current conditions only, not a forecast. Omitted entirely
    (not a placeholder) if `weather` is None -- see weather.py's failure
    handling."""
    if weather is None:
        return ""
    text_family = _font_family_css(cfg.fonts.text)
    display_family = _font_family_css(cfg.fonts.display)
    s = layout.scale
    x = layout.env_x0

    header_size = 62 * s
    body_size = 24 * s
    y0 = layout.weather_y0
    # temp + condition on the header baseline; humidity/precip share one
    # line below -- two rows instead of four, freeing vertical room.
    inline_x = x + 74 * s
    detail_y = y0 + 34 * s
    precip_x = x + 118 * s

    return "\n".join([
        f'<text x="{x:.1f}" y="{y0:.1f}" font-family="{display_family}" '
        f'font-size="{header_size:.1f}" fill="{palette.accent}">天</text>',
        f'<text x="{inline_x:.1f}" y="{y0:.1f}" font-family="{text_family}" '
        f'font-size="{body_size:.1f}" fill="{palette.secondary}">'
        f'{weather.temp_c:.0f}°　{_esc(weather.condition_ja)}</text>',
        f'<text x="{x:.1f}" y="{detail_y:.1f}" font-family="{text_family}" '
        f'font-size="{body_size:.1f}" fill="{palette.secondary}">湿 {weather.humidity_pct}%</text>',
        f'<text x="{precip_x:.1f}" y="{detail_y:.1f}" font-family="{text_family}" '
        f'font-size="{body_size:.1f}" fill="{palette.secondary}">雨 {weather.precip_probability_pct}%</text>',
    ])


def _currency_svg(cfg: Config, layout: _Layout, palette: Palette,
                  currency: "CurrencyInfo | None") -> str:
    """為替 module: today's USD/COP official TRM with its min/max and an
    up/down trend arrow, plus a GitHub-style weekly strip -- one cell per
    previous day, tinted by that day's rate, labelled with the weekday
    kanji.

    DELIBERATE EXCEPTION to the "omit on failure" rule the other
    environmental modules follow (see weather/rainfall): a silently
    vanished exchange rate is exactly what you'd want to notice, so this
    module ALWAYS renders and surfaces its state. Three branches, keyed on
    `currency.status`:

        live         -> rate in accent, min/max + trend arrow, tinted week strip
        stale        -> same, muted (tertiary) + a 古 stale marker and the
                        as-of date, so old data reads as old
        unavailable  -> a visible 取得不可 stub in the glow (attention) token
                        instead of disappearing

    A None currency (no data object at all) is treated as unavailable
    rather than omitted, to preserve that promise."""
    text_family = _font_family_css(cfg.fonts.text)
    display_family = _font_family_css(cfg.fonts.display)
    s = layout.scale
    x = layout.env_x0
    x1 = layout.env_x1
    pair = _esc(cfg.currency.pair_label)

    header_size = 62 * s
    body_size = 24 * s
    small_size = 18 * s
    y0 = layout.currency_y0
    y_spot = y0 + 44 * s       # 現在 + 公式 side by side -- the rate line
    y_trm = y_spot + 30 * s     # week 安/高 (one row up now that 現在/公式 share a line)
    strip_y = y_trm + 20 * s    # top of the weekly strip cells
    stale_y = strip_y + 54 * s

    status = getattr(currency, "status", "unavailable") if currency is not None else "unavailable"

    # --- unavailable: visible stub, never empty ---
    if status == "unavailable" or currency is None or currency.current_rate is None:
        # Even with no official TRM, a live spot may still exist -- show it
        # if so, else the N/A stub.
        spot_rate = getattr(currency, "spot_rate", None) if currency is not None else None
        lines_out = [
            f'<text x="{x:.1f}" y="{y0:.1f}" font-family="{display_family}" '
            f'font-size="{header_size:.1f}" fill="{palette.glow}">為替</text>',
        ]
        if spot_rate is not None:
            lines_out.append(
                f'<text x="{x:.1f}" y="{y_spot:.1f}" font-family="{text_family}" '
                f'font-size="{body_size:.1f}" fill="{palette.secondary}">現在 {spot_rate:,.0f}</text>')
            lines_out.append(
                f'<text x="{x:.1f}" y="{y_trm:.1f}" font-family="{text_family}" '
                f'font-size="{body_size:.1f}" fill="{palette.glow}">公式 取得不可</text>')
        else:
            lines_out.append(
                f'<text x="{x:.1f}" y="{y_spot:.1f}" font-family="{text_family}" '
                f'font-size="{body_size:.1f}" fill="{palette.glow}">{pair}</text>')
            lines_out.append(
                f'<text x="{x:.1f}" y="{y_trm:.1f}" font-family="{text_family}" '
                f'font-size="{body_size:.1f}" fill="{palette.glow}">取得不可 N/A</text>')
        return "\n".join(lines_out)

    is_stale = status == "stale"
    header_color = palette.tertiary if is_stale else palette.accent
    trm_color = palette.tertiary if is_stale else palette.secondary
    detail_color = palette.tertiary if is_stale else palette.secondary

    trm_str = f"{currency.current_rate:,.2f}"
    lo = f"{currency.week_min:,.0f}" if currency.week_min is not None else "—"
    hi = f"{currency.week_max:,.0f}" if currency.week_max is not None else "—"

    # 現在 (live spot): the headline. Its arrow is spot-vs-TRM (green when
    # the market trades over the official rate). Muted/omitted per its own
    # state, independent of the TRM's.
    spot_rate = getattr(currency, "spot_rate", None)
    spot_status = getattr(currency, "spot_status", "unavailable")
    spot_trend = getattr(currency, "spot_trend", "")
    spot_glyph = {"up": "▲", "down": "▼"}.get(spot_trend, "—")

    parts = [
        f'<text x="{x:.1f}" y="{y0:.1f}" font-family="{display_family}" '
        f'font-size="{header_size:.1f}" fill="{header_color}">為替</text>',
    ]
    # 現在 (live spot) and 公式 (official TRM) side by side on one line: the
    # live figure leads, the official sits to its right as a muted anchor.
    trm_tspan = (f'　<tspan font-size="{small_size:.1f}" fill="{trm_color}">'
                 f'公式 {trm_str}</tspan>')
    if spot_rate is not None:
        spot_muted = spot_status == "stale"
        spot_num_color = palette.tertiary if spot_muted else palette.primary
        spot_arrow_color = palette.tertiary if spot_muted else _trend_color(spot_trend, palette)
        stale_tag = ' <tspan font-size="{:.1f}" fill="{}">古</tspan>'.format(small_size, palette.tertiary) if spot_muted else ""
        parts.append(
            f'<text x="{x:.1f}" y="{y_spot:.1f}" font-family="{text_family}" '
            f'font-size="{body_size:.1f}" fill="{spot_num_color}">現在 {spot_rate:,.0f} '
            f'<tspan fill="{spot_arrow_color}">{spot_glyph}</tspan>{stale_tag}{trm_tspan}</text>')
    else:
        # No spot -> show the 現在 placeholder, still with 公式 beside it.
        parts.append(
            f'<text x="{x:.1f}" y="{y_spot:.1f}" font-family="{text_family}" '
            f'font-size="{body_size:.1f}" fill="{palette.tertiary}">現在 —{trm_tspan}</text>')
    # week min / max (安 / 高), now directly under the combined rate line.
    parts.append(
        f'<text x="{x:.1f}" y="{y_trm:.1f}" font-family="{text_family}" '
        f'font-size="{small_size:.1f}" fill="{detail_color}">安 {lo}　高 {hi}</text>')

    # --- GitHub-style weekly strip: one small cell per day, tinted by that
    # day's direction vs. the day before (green up / red down / gray flat).
    # The first shown day has no predecessor, so it's flat/gray. ---
    days = list(getattr(currency, "days", []) or [])
    if days:
        n = len(days)
        gap = 5 * s
        # Smaller cells than before: cap at 22px, still fit the column width.
        avail = (x1 - x)
        cell = min(22 * s, (avail - gap * (n - 1)) / n)
        weekday_kanji = weekday_kanji_header()  # Mon..Sun
        prev_rate = None
        for i, (iso_date, rate) in enumerate(days):
            cx = x + i * (cell + gap)
            if prev_rate is None:
                day_dir = "flat"
            elif rate > prev_rate:
                day_dir = "up"
            elif rate < prev_rate:
                day_dir = "down"
            else:
                day_dir = "flat"
            prev_rate = rate
            fill = palette.tertiary if is_stale else _trend_color(day_dir, palette)
            opacity = 0.45 if is_stale else 0.85
            parts.append(
                f'<rect x="{cx:.1f}" y="{strip_y:.1f}" width="{cell:.1f}" height="{cell:.1f}" '
                f'rx="{2.5 * s:.1f}" fill="{fill}" opacity="{opacity:.2f}"/>'
            )
            # Weekday kanji embedded in the cell.
            try:
                wd = date.fromisoformat(iso_date).weekday()  # 0=Mon
                ch = weekday_kanji[wd]
            except Exception:
                ch = ""
            if ch:
                parts.append(
                    f'<text x="{cx + cell / 2:.1f}" y="{strip_y + cell / 2 + 4 * s:.1f}" '
                    f'font-family="{text_family}" font-size="{11 * s:.1f}" '
                    f'fill="{palette.background}" text-anchor="middle">{ch}</text>'
                )

    if is_stale:
        # 古 = "old": mark the data as no longer fresh, with its as-of date.
        as_of = _esc(currency.as_of_date or "")
        parts.append(
            f'<text x="{x:.1f}" y="{stale_y:.1f}" font-family="{text_family}" '
            f'font-size="{small_size:.1f}" fill="{palette.tertiary}">古 {as_of}</text>'
        )
    return "\n".join(parts)


def _rainfall_svg(cfg: Config, layout: _Layout, palette: Palette, target_date: date,
                    climatology: RainfallClimatology | None) -> str:
    """雨 module: a 12-month rainfall climatology sparkline, showing
    whatever annual rainfall shape the configured location actually has.
    Static data (see rainfall.py); omitted entirely if `climatology` is
    None."""
    if climatology is None:
        return ""
    text_family = _font_family_css(cfg.fonts.text)
    display_family = _font_family_css(cfg.fonts.display)
    s = layout.scale
    x0, x1 = layout.env_x0, layout.env_x1
    width = x1 - x0

    header_size = 62 * s
    bars_h = 110 * s
    y0 = layout.rainfall_y0
    bars_top = y0 + 40 * s
    labels_y = bars_top + bars_h + 28 * s

    n = 12
    pitch = width / n
    bar_w = pitch * 0.6
    max_mm = max(climatology.monthly_mm) or 1.0
    current_month = target_date.month

    parts = [
        f'<text x="{x0:.1f}" y="{y0:.1f}" font-family="{display_family}" '
        f'font-size="{header_size:.1f}" fill="{palette.accent}">雨</text>'
    ]
    for i, mm in enumerate(climatology.monthly_mm):
        is_current = (i + 1) == current_month
        bar_h = max(2.0, bars_h * (mm / max_mm))
        bx = x0 + i * pitch + (pitch - bar_w) / 2
        by = bars_top + (bars_h - bar_h)
        fill = palette.accent if is_current else palette.tertiary
        opacity = 0.9 if is_current else 0.45
        parts.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'fill="{fill}" opacity="{opacity}"/>'
        )
        lx = x0 + i * pitch + pitch / 2
        label_color = palette.accent if is_current else palette.tertiary
        # 十一/十二 are 2 characters wide -- a smaller size than the single-
        # character months keeps every label clear of its neighbors within
        # the per-month pitch.
        label_size = (11 if len(MONTH_KANJI_DIGITS[i]) > 1 else 16) * s
        parts.append(
            f'<text x="{lx:.1f}" y="{labels_y:.1f}" font-family="{text_family}" '
            f'font-size="{label_size:.1f}" fill="{label_color}" text-anchor="middle">'
            f'{MONTH_KANJI_DIGITS[i]}</text>'
        )
    return "\n".join(parts)


def _polar_xy(cx: float, cy: float, r: float, azimuth_deg: float) -> tuple[float, float]:
    """azimuth 0=N(up)/90=E(right)/180=S(down)/270=W(left), standard
    compass convention, mapped to SVG's y-down coordinate space."""
    theta = math.radians(azimuth_deg)
    return cx + r * math.sin(theta), cy - r * math.cos(theta)


def _angular_diff(a_deg: float, b_deg: float) -> float:
    d = abs(a_deg - b_deg) % 360.0
    return min(d, 360.0 - d)


def _ellipse_xy(cx: float, cy: float, rx: float, ry: float, azimuth_deg: float) -> tuple[float, float]:
    """Point on the perspective horizon/ground-plane ellipse -- same
    0=N(up)/90=E(right)/180=S(down)/270=W(left) convention as _polar_xy,
    just with independent x/y radii so the ellipse can read as a ground
    plane tilted away from the viewer (flattened vertically) rather than a
    flat top-down compass ring."""
    theta = math.radians(azimuth_deg)
    return cx + rx * math.sin(theta), cy - ry * math.cos(theta)


_SKY_APEX_FRAC = 2.0  # how high above the ellipse's center a straight-
# overhead (alt=90) point sits, as a fraction of the outer sky-sphere
# radius -- deliberately past 1.0, so a high-altitude arc bulges beyond
# the dotted boundary circle rather than staying pinned inside it.


def _sky_point(cx: float, cy: float, rx: float, ry: float, radius: float,
                az: float, alt: float) -> tuple[float, float]:
    """Stylized sky placement for a given azimuth/altitude, used for every
    sun/moon marker and arc anchor in the celestial-dome diagram.

    Deliberately NOT a rigorous sky projection: a point at alt=0 sits
    exactly on the horizon ellipse at its true azimuth (see _ellipse_xy);
    as altitude climbs toward 90 it lifts straight up (x unchanged) toward
    a fixed apex height (see _SKY_APEX_FRAC) above the ellipse's center.
    This is what turns "sunrise/sunset azimuth" into "a graceful arc
    reaching up into the dome" without needing real 3D sky-sphere math --
    see _house_diagram_svg's docstring for the composition this serves."""
    t = max(0.0, min(1.0, alt / 90.0))
    ex, ey = _ellipse_xy(cx, cy, rx, ry, az)
    apex_y = cy - radius * _SKY_APEX_FRAC
    return ex, ey + (apex_y - ey) * t


def _arc_path(p0: tuple[float, float], p_control: tuple[float, float], p1: tuple[float, float]) -> str:
    """A single quadratic-Bezier 'd' string through 3 sky points (e.g.
    sunrise -> solar-noon -> sunset) -- a smooth graceful arc rather than a
    densely-sampled polyline, matching this diagram's explicit priority of
    elegance over astronomical precision (the arc doesn't pass exactly
    through p_control, just bulges toward it, which reads perfectly well
    for a decorative sky track)."""
    return f"M {p0[0]:.1f} {p0[1]:.1f} Q {p_control[0]:.1f} {p_control[1]:.1f} {p1[0]:.1f} {p1[1]:.1f}"


_ISO_COS30 = math.cos(math.radians(30))
_ISO_SIN30 = math.sin(math.radians(30))


def _iso_rotate_z(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a local house-space point around the vertical axis -- this is
    what `[house] rotation_deg` means: physically turning the house on the
    ground, not tilting the isometric camera."""
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return x * ca - y * sa, x * sa + y * ca


def _iso_project(x: float, y: float, z: float, rotation_deg: float,
                  cx: float, cy: float) -> tuple[float, float]:
    """Local house-space (x=local east, y=local north, z=up at
    rotation_deg=0) -> 2D screen (SVG y-down), via a true isometric
    projection (the camera looks along (-1,-1,-1), the direction implied
    by this exact formula) after rotating around z by rotation_deg."""
    rx, ry = _iso_rotate_z(x, y, rotation_deg)
    return cx + (rx - ry) * _ISO_COS30, cy + (rx + ry) * _ISO_SIN30 - z


def _iso_face_visible(nx: float, ny: float, nz: float, rotation_deg: float) -> bool:
    """Whether a face (outward normal nx,ny,nz in local house-space) faces
    the isometric camera. (1,1,1) is the view direction implied by
    _iso_project's formula (the one world direction that projects to zero
    screen displacement, i.e. straight down the camera's line of sight) --
    verified against a real cross-product/Newell's-method normal
    computation, not assumed. Rotating both the house and re-testing this
    per render is what lets ANY `rotation_deg` still show a correctly
    "solid" house (the right 2-3 faces), not just the default orientation."""
    rnx, rny = _iso_rotate_z(nx, ny, rotation_deg)
    return (rnx + rny + nz) > 1e-6


def _true_compass_label(target_az: float) -> str:
    """Nearest of the 4 true compass directions (北/東/南/西) to
    `target_az` -- for reporting which real-world direction light is
    actually coming from (the exposure/illumination hint text),
    independent of the house's own rotation_deg.

    (An earlier version of this matched against the house's *rotated*
    wall azimuths and returned the wall's pre-rotation label -- correct
    only by coincidence while rotation_deg was always 0 in testing, and
    actively misleading with a real non-zero rotation_deg: it could
    report e.g. "west" for a wall that, in reality, was facing east. See
    _closest_wall_face for the (correctly rotation-aware) internal
    wall-name lookup this was never meant to duplicate -- that one drives
    the actual highlight/tint on the house geometry, not this text.)"""
    faces = [("北", 0.0), ("東", 90.0), ("南", 180.0), ("西", 270.0)]
    best_label, best_diff = "北", 361.0
    for label, base_az in faces:
        diff = _angular_diff(target_az, base_az)
        if diff < best_diff:
            best_label, best_diff = label, diff
    return best_label


# Real-world azimuth each `faces` list entry (in _house_diagram_svg) points
# at when rotation_deg=0 -- back(-y)=north, right(+x)=east, front(+y)=south,
# left(-x)=west (front faces south, matching _house_diagram_svg's own
# docstring), so the illuminated-facade highlight below can match it
# directly against the `faces` list.
_FACE_BASE_AZ = {"back": 0.0, "right": 90.0, "front": 180.0, "left": 270.0}


def _closest_wall_face(target_az: float, rotation_deg: float) -> str:
    """Which of the house's 4 (possibly rotated) wall faces -- by `faces`
    list name -- is angularly closest to `target_az`."""
    best_name, best_diff = "front", 361.0
    for name, base_az in _FACE_BASE_AZ.items():
        diff = _angular_diff(target_az, (base_az + rotation_deg) % 360.0)
        if diff < best_diff:
            best_name, best_diff = name, diff
    return best_name


def _house_diagram_svg(cfg: Config, layout: _Layout, palette: Palette, geometry: SolarGeometry,
                         cx: float, diagram_cy: float, radius: float,
                         current_az: float, current_alt: float, is_night: bool,
                         moon: MoonPhase | None = None,
                         moon_track: tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None = None) -> list[str]:
    """A stylized celestial-sphere diagram: the outer dotted circle is the
    visible sky/dome boundary, and a flattened ellipse inside it is the
    horizon/ground (house-level) plane seen in perspective -- 北/南/東/西
    sit at the ellipse's far/near/right/left extremes (see _ellipse_xy). A
    house (rectangle + hip-roof ridge lines + a front door mark, so the
    shape is directional rather than mirror-symmetric -- rotation via
    `[house] rotation_deg` actually means something) sits centered on that
    ellipse, sized as the diagram's spatial reference, not a minor detail.
    The sun's (day) or moon's (night, `moon` required) track is a single
    graceful arc reaching up from the ellipse into the dome (see
    _sky_point/_arc_path); the interior is otherwise left empty on
    purpose, so the composition reads as "a small dome containing a
    house," not a technical gauge.

    This is a decorative/legible composition, not a scientific instrument:
    the sky placement is a stylized lift off the ellipse (see _sky_point's
    docstring), not a rigorous sky projection. Always the house, never
    swapped out entirely, so whichever body is actually lighting the place
    shows both where it is and which wall it's currently hitting.

    Convention: at rotation_deg=0 the door faces due south (the box's local
    +y edge) -- i.e. this depicts someone standing at their own front door,
    facing outward. `rotation_deg` should hold the real, precise
    calibration (e.g. a desk's actual compass heading); every real-azimuth
    fact (illuminated_face, hint text, this convention) is derived from it
    unmodified. Only the CAMERA angle gets a cosmetic nudge (see
    display_rotation below) so the door's wall is always one of the 2
    this fixed isometric camera shows -- the house's real orientation
    never changes to make that happen, just which angle it's drawn from.
    """
    s = layout.scale
    text_family = _font_family_css(cfg.fonts.text)
    rotation = cfg.house.rotation_deg
    parts = []

    # display_rotation: the actual angle fed into every _iso_* geometry
    # call below -- rotation, nudged by whichever multiple of 90 degrees
    # makes the front (door) wall one of the 2 walls this fixed isometric
    # camera shows. The camera only ever displays 2 adjacent walls out of
    # 4, so which pair that is cycles in 90-degree steps as rotation_deg
    # changes; picking the nudge this way guarantees the door is always
    # visible, for any rotation_deg, without ever touching the *real*
    # azimuth math (illuminated_face, hint text, etc. below all still use
    # `rotation` unmodified -- only the drawing/camera angle shifts).
    display_rotation = rotation
    for _ in range(4):
        if _iso_face_visible(0, 1, 0, display_rotation):
            break
        display_rotation = (display_rotation + 90) % 360

    # The wall (if any) the current sun/moon position is actually hitting --
    # only walls (not roof slopes) get a facade match, and only when the
    # light source is genuinely above the horizon.
    illuminated_face = _closest_wall_face(current_az, rotation) if current_alt > 0 else None
    highlight_fill = _blend_hex(palette.accent, mix_with=palette.tertiary, mix_t=0.25)

    # Roof/ceiling tint: near the equator the sun (and often the moon)
    # passes close to zenith very frequently, lighting the roof far more
    # than any single wall -- and at least one roof slope is visible from
    # this fixed camera for almost every rotation, unlike the walls (which
    # can easily both face away from the light this camera never shows).
    # Ramps in linearly from 0 at altitude 45 degrees to full strength at
    # 90 (straight overhead).
    roof_tint_t = max(0.0, min(1.0, (current_alt - 45.0) / 45.0)) if current_alt > 0 else 0.0

    # palette.tertiary at any reasonable alpha was measured near-invisible
    # against this dark a background (~5/255 actual contrast) -- secondary
    # is the same token the day bar's own hour-tick text uses, already
    # proven legible at this size against this background.
    ring_color = _blend_hex(palette.secondary)
    # Outer sky-sphere boundary -- dotted/faint, the dome's edge. Interior
    # stays otherwise empty on purpose (no fill) so the composition reads
    # as spacious, not a technical gauge.
    parts.append(
        f'<circle cx="{cx:.1f}" cy="{diagram_cy:.1f}" r="{radius:.1f}" fill="none" '
        f'stroke="{ring_color}" stroke-opacity="0.45" stroke-width="2" stroke-dasharray="3,5"/>'
    )
    # Horizon / ground-plane ellipse -- flattened vertically so it reads as
    # a plane seen in perspective rather than a flat top-down compass ring;
    # the house sits centered on it, and every sun/moon marker's alt=0
    # baseline sits exactly on its rim (see _ellipse_xy/_sky_point).
    ellipse_rx, ellipse_ry = radius * 0.86, radius * 0.34
    parts.append(
        f'<ellipse cx="{cx:.1f}" cy="{diagram_cy:.1f}" rx="{ellipse_rx:.1f}" ry="{ellipse_ry:.1f}" '
        f'fill="none" stroke="{ring_color}" stroke-opacity="0.55" stroke-width="1.5"/>'
    )

    # House: a proper 3D box + gable roof, isometrically projected --
    # not a flat top-down plan symbol. `rotation_deg` rotates the actual
    # 3D geometry around the vertical axis (see _iso_rotate_z), and each
    # face's visibility is recomputed per render from its rotated outward
    # normal against the isometric camera (see _iso_face_visible) -- so
    # the house always reads as a solid volume with the correct 2-4 faces
    # showing, for any orientation, not just the default. Local axes:
    # x=local east, y=local north (the +y "front" wall, at rotation_deg=0,
    # faces due south and carries the door -- same convention the facade-
    # exposure hint below uses).
    # Sized as the diagram's spatial reference -- centered on the horizon
    # ellipse, big enough to anchor the composition, not a minor detail
    # lost inside the dome.
    hw, hd = radius * 0.29, radius * 0.20
    wall_h, roof_h = radius * 0.22, radius * 0.14
    door_w, door_h = hw * 0.36, wall_h * 0.62

    def PT(x: float, y: float, z: float) -> str:
        px, py = _iso_project(x, y, z, display_rotation, cx, diagram_cy)
        return f"{px:.1f},{py:.1f}"

    # (name, [3D corners], outward normal, fill token/alpha)
    wall_fill = _blend_hex(palette.tertiary)
    roof_fill = _blend_hex(palette.tertiary)
    faces = [
        ("right", [(hw, -hd, 0), (hw, hd, 0), (hw, hd, wall_h), (hw, -hd, wall_h)],
         (1, 0, 0), wall_fill, 0.14, palette.secondary),
        ("left", [(-hw, -hd, 0), (-hw, hd, 0), (-hw, hd, wall_h), (-hw, -hd, wall_h)],
         (-1, 0, 0), wall_fill, 0.14, palette.secondary),
        ("front", [(-hw, hd, 0), (hw, hd, 0), (hw, hd, wall_h), (0, hd, wall_h + roof_h), (-hw, hd, wall_h)],
         (0, 1, 0), wall_fill, 0.14, palette.secondary),
        ("back", [(-hw, -hd, 0), (hw, -hd, 0), (hw, -hd, wall_h), (0, -hd, wall_h + roof_h), (-hw, -hd, wall_h)],
         (0, -1, 0), wall_fill, 0.14, palette.secondary),
        ("roof_right", [(hw, -hd, wall_h), (hw, hd, wall_h), (0, hd, wall_h + roof_h), (0, -hd, wall_h + roof_h)],
         (roof_h, 0, hw), roof_fill, 0.24, palette.secondary),
        ("roof_left", [(-hw, -hd, wall_h), (-hw, hd, wall_h), (0, hd, wall_h + roof_h), (0, -hd, wall_h + roof_h)],
         (-roof_h, 0, hw), roof_fill, 0.24, palette.secondary),
    ]

    house_lines = []
    front_visible = False
    for name, corners, normal, fill, fill_alpha, stroke in faces:
        if not _iso_face_visible(*normal, display_rotation):
            continue
        if name == "front":
            front_visible = True
        is_roof = name.startswith("roof_")
        tint_t = roof_tint_t if is_roof else (1.0 if name == illuminated_face else 0.0)
        if tint_t <= 0.0:
            face_fill, face_alpha, face_stroke, face_stroke_w = fill, fill_alpha, stroke, 2
        else:
            face_fill = highlight_fill
            face_alpha = fill_alpha + (0.45 - fill_alpha) * tint_t
            face_stroke = palette.accent
            face_stroke_w = 2 + tint_t
        pts = " ".join(PT(*c) for c in corners)
        house_lines.append(
            f'<polygon points="{pts}" fill="{face_fill}" fill-opacity="{face_alpha:.2f}" '
            f'stroke="{face_stroke}" stroke-width="{face_stroke_w:.2f}"/>'
        )

    # Ridge edge -- always drawn if either roof slope is (it's the shared
    # edge between them, on the silhouette either way).
    if _iso_face_visible(roof_h, 0, hw, display_rotation) or _iso_face_visible(-roof_h, 0, hw, display_rotation):
        ridge_back_x, ridge_back_y = _iso_project(0, -hd, wall_h + roof_h, display_rotation, cx, diagram_cy)
        ridge_front_x, ridge_front_y = _iso_project(0, hd, wall_h + roof_h, display_rotation, cx, diagram_cy)
        house_lines.append(
            f'<line x1="{ridge_back_x:.1f}" y1="{ridge_back_y:.1f}" '
            f'x2="{ridge_front_x:.1f}" y2="{ridge_front_y:.1f}" stroke="{palette.secondary}" stroke-width="2"/>'
        )

    # Door -- only on the front (+y) wall, only drawn if that wall is
    # actually facing the camera (a real door isn't visible from behind
    # the house either).
    if front_visible:
        house_lines.append(
            f'<polygon points="{PT(-door_w/2,hd,0)} {PT(door_w/2,hd,0)} '
            f'{PT(door_w/2,hd,door_h)} {PT(-door_w/2,hd,door_h)}" '
            f'fill="{_blend_hex(palette.accent)}" fill-opacity="0.30" stroke="{palette.accent}" stroke-width="2"/>'
        )

    parts.append("".join(house_lines))

    def _offset_from_center(px: float, py: float, margin: float) -> tuple[float, float]:
        dx, dy = px - cx, py - diagram_cy
        dist = math.hypot(dx, dy) or 1.0
        return px + dx / dist * margin, py + dy / dist * margin

    # Compass labels -- placed at the horizon ellipse's own far/near/side
    # extremes (north=far/top, south=near/bottom, east=right, west=left),
    # fixed in screen space regardless of house rotation.
    for label, az in [("北", 0.0), ("東", 90.0), ("南", 180.0), ("西", 270.0)]:
        lx, ly = _offset_from_center(*_ellipse_xy(cx, diagram_cy, ellipse_rx, ellipse_ry, az), 32 * s)
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" font-family="{text_family}" '
            f'font-size="{26 * s:.1f}" fill="{palette.secondary}" '
            f'text-anchor="middle" dominant-baseline="central">{label}</text>'
        )

    # Sky placement from here on (see _sky_point): alt=0 sits on the
    # horizon ellipse at its true azimuth, and every marker lifts straight
    # up toward the dome's apex as its own altitude rises.
    if not is_night:
        # Sunrise/solar-noon/sunset -- a single graceful arc through the 3
        # anchor points (see _arc_path). Fixed date-based facts, shown
        # regardless of the live moment, unlike the current-position
        # marker added below. Deliberately unlabeled (日出/南中/日没 text
        # was tried and removed -- the module's own event lines above
        # already name these times/azimuths, and the arc's shape reads on
        # its own without repeating them here).
        noon_az = 0.0 if geometry.solar_noon_direction == "north" else 180.0
        sr_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, geometry.sunrise_azimuth_deg, 0.0)
        noon_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, noon_az, geometry.solar_noon_altitude_deg)
        ss_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, geometry.sunset_azimuth_deg, 0.0)

        path_color = _blend_hex(palette.accent)
        parts.append(
            f'<path d="{_arc_path(sr_xy, noon_xy, ss_xy)}" '
            f'fill="none" stroke="{path_color}" stroke-opacity="0.55" stroke-width="2.5"/>'
        )

        # Current sun position -- a small starburst rather than a plain
        # dot, so it reads immediately as "the sun, right now" against the
        # otherwise-unmarked arc (it can land anywhere along the arc
        # depending on the time of day, so shape -- not just an outline
        # ring -- is what makes it stand out).
        cur_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, current_az, current_alt)
        core_r = 7 * s
        parts.append(f'<circle cx="{cur_xy[0]:.1f}" cy="{cur_xy[1]:.1f}" r="{core_r:.1f}" fill="{palette.accent}" filter="url(#nowGlow)"/>')
        for i in range(8):
            ang = math.radians(i * 45.0)
            x1 = cur_xy[0] + math.cos(ang) * core_r * 1.4
            y1 = cur_xy[1] + math.sin(ang) * core_r * 1.4
            x2 = cur_xy[0] + math.cos(ang) * core_r * 2.3
            y2 = cur_xy[1] + math.sin(ang) * core_r * 2.3
            parts.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{palette.accent}" stroke-width="{max(1.5, core_r*0.22):.2f}"/>'
            )
    elif current_alt > 0:
        # Night: a second, subtler arc for tonight's moon track (thinner,
        # dashed, dimmer than the sun's -- see moon_track's 3 anchor
        # points, built by the caller from moon_rise_set_around plus a
        # transit-time sample). Either edge can be missing if the moon was
        # already up at the start of the search window or still up past
        # its end, in which case the arc is simply omitted rather than
        # guessed at. The moon's live position is its own marker, drawn as
        # the actual moon-phase icon (reusing _moon_diagram_svg at marker
        # size) rather than a plain dot, so the phase itself reads
        # directly off the diagram.
        if moon_track:
            rise_pt, peak_pt, set_pt = moon_track
            rise_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, *rise_pt)
            peak_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, *peak_pt)
            set_xy = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, *set_pt)
            # secondary (not tertiary) + a touch more opacity than a first
            # pass used -- tertiary at low opacity measured almost
            # invisible against the night segment's near-black background;
            # still clearly subtler than the sun's solid accent-colored
            # arc (dashed, thinner, dimmer), just not invisible.
            path_color = _blend_hex(palette.secondary)
            parts.append(
                f'<path d="{_arc_path(rise_xy, peak_xy, set_xy)}" '
                f'fill="none" stroke="{path_color}" stroke-opacity="0.6" stroke-width="2" stroke-dasharray="4,4"/>'
            )

        assert moon is not None
        moon_r = 15 * s
        mx, my = _sky_point(cx, diagram_cy, ellipse_rx, ellipse_ry, radius, current_az, current_alt)
        parts.extend(_moon_diagram_svg(palette, mx, my, moon_r, moon))
        parts.append(
            f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="{moon_r + 3*s:.1f}" fill="none" '
            f'stroke="{palette.accent}" stroke-width="1.5"/>'
        )

    # Illumination hint: whichever (rotated) facade is closest to the
    # relevant azimuth -- day uses the fixed sunrise/sunset facts (always
    # meaningful, independent of the live moment); night uses the moon's
    # actual current azimuth (only meaningful right now, so it's omitted
    # once the moon sets below the horizon rather than showing stale info).
    hint_y = diagram_cy + radius + 54 * s
    if not is_night:
        morning_face = _true_compass_label(geometry.sunrise_azimuth_deg)
        evening_face = _true_compass_label(geometry.sunset_azimuth_deg)
        hint_text = f'{morning_face}面 朝陽　{evening_face}面 夕陽'
    elif current_alt > 0:
        moon_face = _true_compass_label(current_az)
        hint_text = f'{moon_face}面 月光'
    else:
        hint_text = "月　地平線下"
    parts.append(
        f'<text x="{cx:.1f}" y="{hint_y:.1f}" font-family="{text_family}" '
        f'font-size="{20 * s:.1f}" fill="{palette.tertiary}" text-anchor="middle">{hint_text}</text>'
    )
    return parts


def _moon_diagram_svg(palette: Palette, cx: float, diagram_cy: float, radius: float,
                        moon: MoonPhase) -> list[str]:
    """The current moon phase, drawn as a lit/dark disk via a half-circle +
    a clipped ellipse (the standard moon-phase-icon technique) rather than
    an arc path with ambiguous sweep flags. Generic over cx/cy/radius --
    used both at a small size as the house diagram's live moon-position
    marker (see _house_diagram_svg) and, historically, at the diagram's
    full radius; kept general rather than hardcoded to the marker's size.
    Northern-hemisphere convention (waxing lit on the right) -- a fully
    latitude-correct terminator orientation (flipped for southern-
    hemisphere observers) is well past what a small decorative icon
    needs."""
    r = radius * 0.55
    parts = []

    dark_fill = _blend_hex(palette.tertiary)
    parts.append(
        f'<circle cx="{cx:.1f}" cy="{diagram_cy:.1f}" r="{r:.1f}" fill="{dark_fill}" fill-opacity="0.18" '
        f'stroke="{palette.tertiary}" stroke-width="1"/>'
    )

    primary_right = moon.waxing
    sweep = 1 if primary_right else 0
    parts.append(
        f'<path d="M {cx:.1f} {diagram_cy-r:.1f} A {r:.1f} {r:.1f} 0 0 {sweep} {cx:.1f} {diagram_cy+r:.1f} Z" '
        f'fill="{palette.primary}"/>'
    )

    m = math.cos(2 * math.pi * moon.phase_fraction)
    rx = r * abs(m)
    if rx > 0.5:
        if m > 0:
            # crescent (< half lit): carve a dark ellipse out of the
            # primary (already-lit) half, leaving a thin sliver near the rim.
            ellipse_fill, ellipse_right = dark_fill, primary_right
        else:
            # gibbous (> half lit): extend a lit ellipse into the opposite
            # half, growing it toward a full disk as phase approaches 0.5.
            ellipse_fill, ellipse_right = palette.primary, not primary_right
        clip_x = cx if ellipse_right else cx - r
        clip_id = "moonClipR" if ellipse_right else "moonClipL"
        parts.append(
            f'<clipPath id="{clip_id}"><rect x="{clip_x:.1f}" y="{diagram_cy-r:.1f}" '
            f'width="{r:.1f}" height="{2*r:.1f}"/></clipPath>'
        )
        parts.append(
            f'<ellipse cx="{cx:.1f}" cy="{diagram_cy:.1f}" rx="{rx:.1f}" ry="{r:.1f}" '
            f'fill="{ellipse_fill}" clip-path="url(#{clip_id})"/>'
        )
    return parts


def _is_night(target_date: date, now: datetime | None, sr_hour: float, ss_hour: float) -> bool:
    """Sun below the horizon right now -- the real astronomical condition,
    not the artificial "night" theming segment (which is bounded by the
    work schedule, not sunrise/sunset). Non-live renders (--date without a
    matching `now`) default to False, same fallback philosophy as
    resolve_palette's "day" default."""
    if now is None or now.date() != target_date:
        return False
    hour = now.hour + now.minute / 60
    return not (sr_hour <= hour < ss_hour)


def _solar_geometry_svg(cfg: Config, layout: _Layout, palette: Palette, target_date: date,
                          now: datetime | None, geometry: SolarGeometry,
                          sr_hour: float, ss_hour: float) -> str:
    """日 module: sunrise/solar-noon/sunset times + azimuth/altitude (always
    shown -- date-based facts, still meaningful at night), plus the house/
    compass diagram -- always the house, day or night; night doesn't swap
    it out for a standalone moon icon, it just shows the moon's live
    position and which wall it's lighting instead of the sun's (see
    `_house_diagram_svg`). Solar noon genuinely flips north/south of zenith
    over the year near the equator (see solar_times.solar_geometry's
    docstring). Always computable (pure local astronomy, no external API),
    so no None/omission case."""
    text_family = _font_family_css(cfg.fonts.text)
    display_family = _font_family_css(cfg.fonts.display)
    s = layout.scale
    x0, x1 = layout.env_x0, layout.env_x1
    width = x1 - x0
    # Shifted left of the column's true center (rather than 0.5) so the
    # compass ring's east-side label clears the day bar's live "now" label
    # with margin -- see _Layout.env_x0/env_x1's comment.
    cx = x0 + width * 0.45

    header_size = 62 * s
    label_size = 20 * s
    value_size = 30 * s
    y0 = layout.solar_y0

    noon_dir_ja = "北" if geometry.solar_noon_direction == "north" else "南"

    # Each event gets its own label-then-value pair (not one cramped
    # inline row) -- more legible, and uses the vertical room this column
    # has plenty of, rather than fighting to fit everything on one line.
    events = [
        ("日出", f'{geometry.sunrise_time.strftime("%H:%M")}　{geometry.sunrise_azimuth_deg:.0f}°'),
        ("南中", f'{geometry.solar_noon_time.strftime("%H:%M")}　高度{geometry.solar_noon_altitude_deg:.0f}°　{noon_dir_ja}'),
        ("日没", f'{geometry.sunset_time.strftime("%H:%M")}　{geometry.sunset_azimuth_deg:.0f}°'),
    ]
    parts = [
        f'<text x="{x0:.1f}" y="{y0:.1f}" font-family="{display_family}" '
        f'font-size="{header_size:.1f}" fill="{palette.accent}">日</text>',
    ]
    y = y0 + 80 * s
    for label, value in events:
        parts.append(
            f'<text x="{x0:.1f}" y="{y:.1f}" font-family="{text_family}" '
            f'font-size="{label_size:.1f}" fill="{palette.tertiary}">{label}</text>'
        )
        y += 34 * s
        parts.append(
            f'<text x="{x0:.1f}" y="{y:.1f}" font-family="{text_family}" '
            f'font-size="{value_size:.1f}" fill="{palette.primary}">{value}</text>'
        )
        y += 50 * s
    last_value_y = y - 50 * s

    # Much larger than the module text above -- this diagram is the part
    # that actually needs to be read at a glance, not squinted at.
    radius = min(width, 320 * s) / 2 * 0.82
    diagram_cy = last_value_y + 70 * s + radius

    is_night = _is_night(target_date, now, sr_hour, ss_hour)
    # Non-live renders (--date without a matching `now`) have no real "this
    # moment" to sample -- fall back to solar noon, same fallback
    # philosophy as _is_night defaulting to day. is_night is always False
    # in that case, so moon_position() is never reached without a real
    # `now` for it to use.
    sample_time = now or geometry.solar_noon_time
    moon = None
    moon_track = None
    lat, lon = cfg.location.latitude, cfg.location.longitude
    if is_night:
        moon = moon_phase(target_date)
        current_az, current_alt = moon_position(sample_time, lat, lon)
        if current_alt > 0:
            # Only meaningful (and only searched for) when the moon is
            # actually up right now; missing rise/set (moon already up at
            # the start of our +/-15h search window, or still up past its
            # end) just means the arc is omitted, not an error -- see
            # moon_rise_set_around. The "peak" anchor is the moon's
            # position at the window's midpoint time -- a stand-in for
            # lunar transit (this diagram doesn't need the exact transit
            # instant, just a plausible highest point for the arc to bulge
            # toward -- see _house_diagram_svg's docstring on priorities).
            rise_t, set_t = moon_rise_set_around(sample_time, lat, lon)
            if rise_t is not None and set_t is not None:
                peak_t = rise_t + (set_t - rise_t) / 2
                moon_track = (
                    moon_position(rise_t, lat, lon),
                    moon_position(peak_t, lat, lon),
                    moon_position(set_t, lat, lon),
                )
    else:
        current_az, current_alt = solar_position(sample_time, lat, lon)

    parts.extend(_house_diagram_svg(cfg, layout, palette, geometry, cx, diagram_cy, radius,
                                      current_az, current_alt, is_night, moon=moon,
                                      moon_track=moon_track))
    if is_night:
        # Below _house_diagram_svg's own illumination-hint line.
        moon_label_y = diagram_cy + radius + 54 * s + 30 * s
        parts.append(
            f'<text x="{cx:.1f}" y="{moon_label_y:.1f}" font-family="{text_family}" '
            f'font-size="{20 * s:.1f}" fill="{palette.secondary}" text-anchor="middle">'
            f'月　{moon.name_ja}　{moon.illumination_pct:.0f}%</text>'
        )

    return "\n".join(parts)


def _hour_to_y(hour_decimal: float, layout: _Layout) -> float:
    frac = hour_decimal / 24.0
    return layout.bar_y0 + frac * (layout.bar_y1 - layout.bar_y0)


_LOCK_KANJI = ["施", "錠", "中"]  # shijouchuu -- "currently locked," real door-sign vocabulary


def _lock_indicator_svg(cfg: Config, layout: _Layout, palette: Palette) -> str:
    """i3lock background variant: replaces the 24h day bar (which isn't
    meaningful while the screen is locked) with a vertical (tategaki-style,
    top-to-bottom) kanji column reading 施錠中 (locked), plus a reading and
    the literal word LOCKED underneath -- see main.py for how this gets
    wired into i3lock. Reuses the day bar's own column position
    (layout.bar_x/bar_y0/bar_y1) so it sits in exactly the slot the day bar
    would have used."""
    display_path = _resolve_font_path(cfg.fonts.display[0])
    display_family = _font_family_css(cfg.fonts.display)
    text_family = _font_family_css(cfg.fonts.text)
    s = layout.scale
    x = layout.bar_x

    char_h = 100 * s
    char_size = _font_size_for_height(display_path, _LOCK_KANJI[0], char_h)
    pitch = char_h * 1.25
    block_h = pitch * (len(_LOCK_KANJI) - 1)
    y_first = (layout.bar_y0 + layout.bar_y1) / 2 - block_h / 2

    parts = []
    for i, ch in enumerate(_LOCK_KANJI):
        y = y_first + i * pitch
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{display_family}" '
            f'font-size="{char_size:.1f}" fill="{palette.accent}" '
            f'text-anchor="middle" dominant-baseline="central">{ch}</text>'
        )

    y_reading = y_first + block_h + 70 * s
    y_english = y_reading + 34 * s
    parts.append(
        f'<text x="{x:.1f}" y="{y_reading:.1f}" font-family="{text_family}" '
        f'font-size="{22 * s:.1f}" fill="{palette.secondary}" text-anchor="middle">'
        f'しじょうちゅう</text>'
    )
    parts.append(
        f'<text x="{x:.1f}" y="{y_english:.1f}" font-family="{text_family}" '
        f'font-size="{26 * s:.1f}" fill="{palette.secondary}" text-anchor="middle" '
        f'letter-spacing="{3 * s:.1f}">LOCKED</text>'
    )
    return "\n".join(parts)


def _day_bar_svg(target_date: date, cfg: Config, layout: _Layout, now: datetime | None,
                  palette: Palette, active_segment_idx: int | None,
                  sr_hour: float, ss_hour: float) -> str:
    text_family = _font_family_css(cfg.fonts.text)
    line_x = layout.bar_x
    y0, y1 = layout.bar_y0, layout.bar_y1
    s = layout.scale

    # Spine: 5 segments, always visible as the "day wheel" legend. Each
    # segment shows its OWN static accent -- EXCEPT the currently active
    # one, which is boosted to its "timeline_active" token (brighter/more
    # saturated) and drawn thicker, so the active state reads as clearly
    # emphasized against the other 4 legend segments, not just "one more
    # color in a row."
    work_start_hour = _parse_hhmm(cfg.schedule.work_start)
    work_end_hour = _parse_hhmm(cfg.schedule.work_end)
    seg_bounds = [0.0, sr_hour, work_start_hour, ss_hour, work_end_hour, 24.0]
    parts = []
    for i in range(5):
        y_seg0 = _hour_to_y(seg_bounds[i], layout)
        y_seg1 = _hour_to_y(seg_bounds[i + 1], layout)
        seg_palette = SEGMENT_PALETTES[SEGMENT_ORDER[i]]
        if i == active_segment_idx:
            seg_color, width, opacity = seg_palette.timeline_active, 5.5 * s, 1.0
        else:
            seg_color, width, opacity = seg_palette.accent, 3.0 * s, 0.75
        parts.append(
            f'<line x1="{line_x:.1f}" y1="{y_seg0:.1f}" x2="{line_x:.1f}" y2="{y_seg1:.1f}" '
            f'stroke="{seg_color}" stroke-width="{width:.2f}" opacity="{opacity}"/>'
        )

    hour_font_size = 18 * s
    tick_w = 10 * s
    for h in range(25):
        y = _hour_to_y(h, layout)
        parts.append(
            f'<line x1="{line_x-tick_w/2:.1f}" y1="{y:.1f}" x2="{line_x+tick_w/2:.1f}" y2="{y:.1f}" '
            f'stroke="{palette.tertiary}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{line_x-20*s:.1f}" y="{y:.1f}" font-family="{text_family}" '
            f'font-size="{hour_font_size:.1f}" fill="{palette.tertiary}" '
            f'text-anchor="end" dominant-baseline="central">{h:02d}</text>'
        )

    events = [
        (_parse_hhmm(cfg.schedule.sleep), "眠"),
        (sr_hour, "日出"),
        (_parse_hhmm(cfg.schedule.wake), "起"),
        (_parse_hhmm(cfg.schedule.work_start), "始"),
        (_parse_hhmm(cfg.schedule.work_end), "終"),
        (ss_hour, "日没"),
        (24.0, "眠"),
    ]
    label_font_size = 24 * s
    dot_r = 4 * s
    for hour, label in events:
        y = _hour_to_y(hour, layout)
        parts.append(f'<circle cx="{line_x:.1f}" cy="{y:.1f}" r="{dot_r:.1f}" fill="{palette.secondary}"/>')
        parts.append(
            f'<text x="{line_x+18*s:.1f}" y="{y:.1f}" font-family="{text_family}" '
            f'font-size="{label_font_size:.1f}" fill="{palette.secondary}" '
            f'dominant-baseline="central">{label}</text>'
        )

    # Lunch as a duration span, not a single point
    lunch_start = _parse_hhmm(cfg.schedule.lunch)
    lunch_end = lunch_start + cfg.schedule.lunch_duration_minutes / 60
    y_ls, y_le = _hour_to_y(lunch_start, layout), _hour_to_y(lunch_end, layout)
    parts.append(
        f'<line x1="{line_x:.1f}" y1="{y_ls:.1f}" x2="{line_x:.1f}" y2="{y_le:.1f}" '
        f'stroke="{palette.secondary}" stroke-width="{4*s:.1f}"/>'
    )
    y_lm = (y_ls + y_le) / 2
    parts.append(
        f'<text x="{line_x+18*s:.1f}" y="{y_lm:.1f}" font-family="{text_family}" '
        f'font-size="{label_font_size:.1f}" fill="{palette.secondary}" '
        f'dominant-baseline="central">昼</text>'
    )

    # Live "now" marker -- strongly themed (palette.glow + blur), only
    # meaningful when rendering for the actual current date.
    if now is not None and now.date() == target_date:
        now_hour = now.hour + now.minute / 60
        y_now = _hour_to_y(now_hour, layout)
        parts.append(
            f'<circle cx="{line_x:.1f}" cy="{y_now:.1f}" r="{7*s:.1f}" fill="none" '
            f'stroke="{palette.glow}" stroke-width="{2*s:.1f}" filter="url(#nowGlow)"/>'
        )
        parts.append(
            f'<text x="{line_x-55*s:.1f}" y="{y_now:.1f}" font-family="{text_family}" '
            f'font-size="{label_font_size:.1f}" fill="{palette.glow}" '
            f'text-anchor="end" dominant-baseline="central">{now.strftime("%H:%M")}</text>'
        )

    return "\n".join(parts)


_FILTER_DEFS = '''<defs>
  <filter id="nowGlow" x="-150%" y="-150%" width="400%" height="400%" color-interpolation-filters="sRGB">
    <feGaussianBlur in="SourceGraphic" stdDeviation="5" result="blur"/>
    <feMerge>
      <feMergeNode in="blur"/>
      <feMergeNode in="blur"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>
  </filter>
</defs>'''


def resolve_palette(target_date: date, now: datetime | None, cfg: Config) -> tuple[Palette, int | None, float, float]:
    """The live, smoothly-interpolated Palette for `now` if it's actually
    `target_date`, else a fixed neutral fallback (keeps --date/--output
    testing reproducible). Also returns the active segment index (for the
    bar's timeline emphasis, None if not live) and sunrise/sunset hours."""
    lat, lon = cfg.location.latitude, cfg.location.longitude
    sr = sunrise(target_date, lat, lon)
    ss = sunset(target_date, lat, lon)
    sr_hour, ss_hour = sr.hour + sr.minute / 60, ss.hour + ss.minute / 60
    work_start_hour = _parse_hhmm(cfg.schedule.work_start)
    work_end_hour = _parse_hhmm(cfg.schedule.work_end)

    if now is None or now.date() != target_date:
        return SEGMENT_PALETTES[_FALLBACK_SEGMENT], None, sr_hour, ss_hour

    hour = now.hour + now.minute / 60
    palette = current_palette(hour, sr_hour, ss_hour, work_start_hour, work_end_hour)
    seg_name = current_segment_name(hour, sr_hour, ss_hour, work_start_hour, work_end_hour)
    return palette, SEGMENT_ORDER.index(seg_name), sr_hour, ss_hour


def compose(target_date: date, seed_str: str, cfg: Config, now: datetime | None = None,
            weather: WeatherInfo | None = None, climatology: RainfallClimatology | None = None,
            lock_mode: bool = False, currency: "CurrencyInfo | None" = None) -> str:
    variation = build_variation(seed_str)
    layout = _Layout(cfg)

    palette, active_segment_idx, sr_hour, ss_hour = resolve_palette(target_date, now, cfg)

    layers = [
        _FILTER_DEFS,
        f'<rect x="0" y="0" width="{layout.w}" height="{layout.h}" fill="{palette.background}"/>',
    ]

    if cfg.modules.year_progress:
        layers.append(_year_progress_svg(target_date, variation, layout, palette))
    if cfg.modules.kanji:
        layers.append(_kanji_svg(target_date, cfg, variation, layout, palette))
    if cfg.modules.solar_term:
        layers.append(_term_block_svg(target_date, cfg, variation, layout, palette))
    if cfg.modules.microseason:
        layers.append(_microseason_svg(target_date, cfg, layout, palette))
    if cfg.modules.calendar:
        layers.append(_calendar_svg(target_date, cfg, variation, layout, palette))
    if cfg.modules.weather:
        layers.append(_weather_svg(cfg, layout, palette, weather))
    if cfg.modules.rainfall:
        layers.append(_rainfall_svg(cfg, layout, palette, target_date, climatology))
    if cfg.modules.currency:
        layers.append(_currency_svg(cfg, layout, palette, currency))
    if cfg.modules.solar_geometry:
        geometry = solar_geometry(target_date, cfg.location.latitude, cfg.location.longitude)
        layers.append(_solar_geometry_svg(cfg, layout, palette, target_date, now, geometry, sr_hour, ss_hour))
    if lock_mode:
        layers.append(_lock_indicator_svg(cfg, layout, palette))
    elif cfg.modules.day_bar:
        layers.append(_day_bar_svg(target_date, cfg, layout, now, palette, active_segment_idx, sr_hour, ss_hour))

    body = "\n".join(layers)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {layout.w} {layout.h}" '
        f'width="{layout.w}" height="{layout.h}">\n{body}\n</svg>'
    )


def rasterize(svg: str, output_path: str, width: int, height: int) -> None:
    """Rasterize via inkscape CLI, not cairosvg.

    cairosvg silently DROPS characters missing from the first font in a
    comma-separated font-family list instead of falling back to the next
    one (confirmed empirically: a 3-char microseason name lost its 3rd
    character, 雊, which exists in Noto Sans JP but not Zen Kaku Gothic
    New). inkscape uses the real Pango/fontconfig text stack and resolves
    per-glyph fallback correctly. The cost is subprocess latency
    (~1-3s), irrelevant for a once-a-day generator.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".svg", encoding="utf-8", delete=False) as f:
        f.write(svg)
        svg_path = f.name
    try:
        subprocess.run(
            ["inkscape", svg_path, "--export-type=png",
             f"--export-width={width}", f"--export-height={height}",
             f"--export-filename={output_path}"],
            check=True, capture_output=True,
        )
    finally:
        os.unlink(svg_path)


# Small per-segment multiplier on noise strength -- night/evening slightly
# quieter ("subdued/lowered energy"), day slightly crisper ("clearest"),
# dusk a touch stronger. Kept subtle: the texture PATTERN never changes,
# only its intensity, per the explicit "keep the noise look" requirement.
_NOISE_INTENSITY_MULTIPLIER = {
    "night": 0.80, "dawn": 0.95, "day": 1.15, "dusk": 1.05, "evening": 0.85,
}


def apply_blocky_noise(image_path: str, seed_str: str, block_size: int = 12, intensity: int = 4,
                        segment_name: str | None = None) -> None:
    """Post-process pass: mosaic/blocky luminance noise over the WHOLE
    rendered image (background and kanji alike), applied after rasterize()
    rather than as a per-element SVG filter.

    feTurbulence (used previously) only produces smooth Perlin-style noise --
    there's no "blocky" turbulence type in SVG. Generating a low-resolution
    noise grid and upscaling it with nearest-neighbor (no interpolation) is
    what actually produces visible square blocks, and doing it here (in
    raster space, over the finished composite) is far simpler than trying
    to coordinate two separate filters across element boundaries.

    Seeded the same way as the rest of the generative geometry (stable for
    a given day, changes only when the date or --seed changes) rather than
    reseeding every render -- this runs every 15 minutes for the day bar's
    live marker, and unseeded noise would flicker each time.
    """
    if segment_name is not None:
        intensity = round(intensity * _NOISE_INTENSITY_MULTIPLIER.get(segment_name, 1.0))

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    arr = np.array(img).astype(np.int16)

    rng = make_rng(seed_str, "blocky_noise")
    bw, bh = (w + block_size - 1) // block_size, (h + block_size - 1) // block_size
    block_noise = np.array(
        [[rng.randint(-intensity, intensity) for _ in range(bw)] for _ in range(bh)],
        dtype=np.int16,
    )
    noise_full = np.repeat(np.repeat(block_noise, block_size, axis=0), block_size, axis=1)[:h, :w]

    arr += noise_full[:, :, None]
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    Image.fromarray(arr, "RGB").save(image_path)
