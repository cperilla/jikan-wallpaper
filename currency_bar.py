#!/usr/bin/env python3
"""Compact i3bar indicator: the live USD/COP spot (現在) with its trend
arrow, plus the official TRM (公式) in parentheses -- read from the jikan
currency caches, the same values the wallpaper's 為替 module last showed,
condensed to one block for i3status-rs.

Read-only by design: this NEVER fetches. It reads
`currency_cache.json` (official TRM) and `currency_spot_cache.json` (live
Coinbase spot), both refreshed by main.py on the normal regen cycle (each
TTL-guarded against hitting its API too often -- see currency.py). Running
this from the bar as often as the bar likes therefore costs nothing and can
never hammer either upstream.

Output is Pango markup (like segment_indicator.py) so i3status-rs's `custom`
block renders the colored trend arrow: green when the spot trades over the
official TRM, red under, gray equal.

    both live  : "為替 3,251 ▲ (公 3,209)"
    spot stale : "為替 3,251 古 (公 3,209)"   (muted)
    spot gone  : "為替 (公 3,209)"            (official only)
    all gone   : "為替 —"                      (never blank)

Prints a single line and exits 0 even when data is missing, so the bar
block never errors out.
"""
import sys

from currency import (
    DEFAULT_CACHE_PATH,
    DEFAULT_SPOT_CACHE_PATH,
    combine_spot,
    load_cached_currency,
    load_cached_spot,
)

# Semantic trend tints, matching renderer._TREND_COLORS (green up / red
# down / gray flat). Kept in sync by intent; both are the one deliberate
# non-palette exception for near-universal financial up/down color meaning.
_TREND = {
    "up": ("▲", "#5FAF7A"),
    "down": ("▼", "#C25B5B"),
    "flat": ("—", "#8A8A8A"),
}
_STALE_COLOR = "#8A8A8A"
_LABEL = "為替"


def render(currency) -> str:
    """Pure formatting -- returns the Pango-markup line for a CurrencyInfo
    (or None) carrying both the official TRM and the merged spot. Separated
    from I/O so it's unit-testable."""
    trm_ok = currency is not None and currency.current_rate is not None and currency.status != "unavailable"
    spot_rate = getattr(currency, "spot_rate", None) if currency is not None else None
    spot_status = getattr(currency, "spot_status", "unavailable") if currency is not None else "unavailable"

    # Nothing at all -> visible dash, never blank.
    if not trm_ok and spot_rate is None:
        return f'{_LABEL} <span foreground="{_TREND["flat"][1]}">\u2014</span>'

    official = f"(公 {currency.current_rate:,.0f})" if trm_ok else "(公 —)"

    if spot_rate is not None:
        glyph, color = _TREND.get(getattr(currency, "spot_trend", "flat"), _TREND["flat"])
        if spot_status == "stale":
            # Muted, with a 古 marker so a frozen spot reads as frozen.
            return (f'<span foreground="{_STALE_COLOR}">{_LABEL} {spot_rate:,.0f} '
                    f'古 {official}</span>')
        return (f'{_LABEL} {spot_rate:,.0f} '
                f'<span foreground="{color}">{glyph}</span> {official}')

    # Spot unavailable but TRM present -> official only.
    return f'{_LABEL} {official}'


def main(argv=None) -> int:
    currency = load_cached_currency(DEFAULT_CACHE_PATH)
    spot = load_cached_spot(DEFAULT_SPOT_CACHE_PATH)
    if currency is not None:
        # Merge the cached spot in (status "live" -- this is a read of what's
        # on disk; freshness is the wallpaper regen's concern, not the bar's).
        spot_tuple = (spot[0], spot[1], "live") if spot else None
        currency = combine_spot(currency, spot_tuple)
    print(render(currency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
