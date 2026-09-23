#!/usr/bin/env python3
"""jikan -- generate and (optionally) set today's Japanese almanac wallpaper.

Usage:
    python main.py                                  generate + set today's wallpaper
    python main.py --output preview.png              render only, don't set
    python main.py --date 2026-09-01                 override the date shown
    python main.py --seed 2026-09-01                 override the generative seed
    python main.py --no-set                          render only, don't set
    python main.py --config /path/to/config.toml      use a specific config file
    python main.py --no-lock                         skip regenerating the i3lock background
"""
import argparse
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from PIL import Image

from config import DEFAULT_CONFIG_PATH, load_config
from palette import SEGMENT_ORDER
from rainfall import load_rainfall_climatology
from renderer import apply_blocky_noise, compose, rasterize, resolve_palette
from wallpaper import detect_output_geometries, set_wallpaper
from weather import DEFAULT_CACHE_PATH as WEATHER_CACHE_PATH
from weather import get_weather
from currency import DEFAULT_CACHE_PATH as CURRENCY_CACHE_PATH
from currency import DEFAULT_SPOT_CACHE_PATH as CURRENCY_SPOT_CACHE_PATH
from currency import combine_spot, get_currency, get_spot

DEFAULT_OUTPUT = Path.home() / ".local" / "state" / "jikan-wallpaper.png"
DEFAULT_LOCK_OUTPUT = Path.home() / ".local" / "state" / "jikan-lock.png"


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _render_per_resolution(resolutions, base_path, target_date, seed_str, cfg, now, weather,
                            climatology, segment_name, lock_mode, preseed=None, currency=None):
    """One compose()+rasterize()+noise pass per *distinct* resolution --
    each gets its own correctly-scaled composition (the renderer already
    scales every element to whatever cfg.display it's given), never a
    crop/stretch of another resolution's render. `preseed` lets a caller
    that already rendered one resolution itself (the desktop wallpaper's
    primary render) reuse that file instead of re-rendering it here."""
    rendered = dict(preseed) if preseed else {}
    for w, h in resolutions:
        if (w, h) in rendered:
            continue
        variant_path = base_path.with_stem(f"{base_path.stem}-{w}x{h}")
        variant_cfg = replace(cfg, display=replace(cfg.display, width=w, height=h))
        variant_svg = compose(target_date, seed_str, variant_cfg, now=now, weather=weather,
                               climatology=climatology, lock_mode=lock_mode, currency=currency)
        rasterize(variant_svg, str(variant_path), w, h)
        apply_blocky_noise(str(variant_path), seed_str, block_size=12, intensity=4,
                            segment_name=segment_name)
        rendered[(w, h)] = variant_path
        print(f"wrote {variant_path}")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate today's Japanese almanac wallpaper.")
    parser.add_argument("--output", type=Path, default=None,
                         help=f"output PNG path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--date", type=_parse_date, default=None,
                         help="override the date shown (YYYY-MM-DD), default: today")
    parser.add_argument("--seed", type=str, default=None,
                         help="override the generative-geometry seed, default: same as --date")
    parser.add_argument("--no-set", action="store_true",
                         help="render only, do not set as wallpaper")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                         help=f"config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--no-lock", action="store_true",
                         help="skip regenerating the i3lock background")
    parser.add_argument("--lock-output", type=Path, default=None,
                         help=f"i3lock background output path (default: {DEFAULT_LOCK_OUTPUT})")
    args = parser.parse_args(argv)

    target_date = args.date or date.today()
    seed_str = args.seed or target_date.isoformat()
    output_path = args.output or DEFAULT_OUTPUT
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = load_config(args.config)

    now = datetime.now()
    weather = get_weather(cfg.location.latitude, cfg.location.longitude,
                           WEATHER_CACHE_PATH, cfg.weather.timeout_seconds)
    climatology = load_rainfall_climatology(Path(__file__).parent / cfg.rainfall.data_path)
    # One currency fetch per run, reused everywhere below (like weather).
    # Its own TTL guard (currency.py) suppresses the network hit entirely
    # when a recent cache exists.
    currency = get_currency(CURRENCY_CACHE_PATH, cfg.currency.timeout_seconds,
                            cfg.currency.min_refresh_seconds)
    # Live spot (現在): a second, independent source (Coinbase), fetched once
    # and merged. Toggleable, TTL-guarded, fails on its own.
    if cfg.currency.spot:
        spot = get_spot(CURRENCY_SPOT_CACHE_PATH, cfg.currency.timeout_seconds,
                        cfg.currency.spot_min_refresh_seconds)
        currency = combine_spot(currency, spot)
    svg = compose(target_date, seed_str, cfg, now=now, weather=weather, climatology=climatology,
                  currency=currency)
    rasterize(svg, str(output_path), cfg.display.width, cfg.display.height)

    _, active_segment_idx, _, _ = resolve_palette(target_date, now, cfg)
    segment_name = SEGMENT_ORDER[active_segment_idx] if active_segment_idx is not None else None
    apply_blocky_noise(str(output_path), seed_str, block_size=12, intensity=4, segment_name=segment_name)
    print(f"wrote {output_path}")

    try:
        geometries = detect_output_geometries() or [(cfg.display.width, cfg.display.height, 0, 0)]
    except Exception:
        geometries = [(cfg.display.width, cfg.display.height, 0, 0)]
    resolutions = [(w, h) for w, h, x, y in geometries]

    if not args.no_set:
        # One correctly-composed render per distinct output resolution
        # (not the primary's image repeated) -- see wallpaper.py for why;
        # feh gets each output's own correctly-scaled image, not a single
        # render it has to cover-crop into a differently-shaped screen.
        rendered = _render_per_resolution(
            resolutions, output_path, target_date, seed_str, cfg, now, weather, climatology,
            segment_name, lock_mode=False,
            preseed={(cfg.display.width, cfg.display.height): output_path},
            currency=currency,
        )
        paths = [str(rendered[(w, h)]) for w, h in resolutions]
        set_wallpaper(paths, cfg.wallpaper.setter)
        print(f"wallpaper set via {cfg.wallpaper.setter} ({len(resolutions)} output(s))")

    if not args.no_lock:
        # Reuses the same now/weather/climatology already computed above --
        # no second weather fetch. Generated here (every ~15 min alongside
        # the desktop wallpaper) rather than at lock-time itself, so
        # ~/bin/lock's `i3lock -i` is instant instead of waiting on an
        # inkscape render (and possibly a weather fetch) before the screen
        # actually locks.
        #
        # i3lock takes exactly ONE image with no xinerama/per-output
        # awareness of its own (unlike feh) -- the only way to get each
        # monitor correctly composed is to render each resolution
        # separately (same as the desktop wallpaper above) and composite
        # them into one image sized to the full virtual screen, pasted at
        # each output's real (x, y) offset. i3lock draws that one image
        # directly onto the same contiguous root-window coordinate space
        # X11 already uses for multi-monitor, so a correctly-sized
        # composite lands correctly without needing -t/--tiling.
        lock_output_path = args.lock_output or DEFAULT_LOCK_OUTPUT
        lock_output_path.parent.mkdir(parents=True, exist_ok=True)
        lock_rendered = _render_per_resolution(
            resolutions, lock_output_path, target_date, seed_str, cfg, now, weather, climatology,
            segment_name, lock_mode=True,
            currency=currency,
        )
        canvas_w = max(x + w for w, h, x, y in geometries)
        canvas_h = max(y + h for w, h, x, y in geometries)
        canvas = Image.new("RGB", (canvas_w, canvas_h))
        for w, h, x, y in geometries:
            canvas.paste(Image.open(lock_rendered[(w, h)]), (x, y))
        canvas.save(lock_output_path)
        print(f"wrote {lock_output_path} (composited, {len(geometries)} output(s))")

    return 0


if __name__ == "__main__":
    sys.exit(main())
