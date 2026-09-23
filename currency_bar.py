#!/usr/bin/env python3
"""Compact i3bar indicator: today's USD/COP rate + trend arrow, read from
the jikan currency cache -- the same value the wallpaper's 為替 module last
showed, condensed to one clickable block for i3status-rs.

Read-only by design: this NEVER fetches. It reads
`~/.local/state/jikan/currency_cache.json`, which main.py refreshes on the
normal 15-minute regen (itself TTL-guarded against hitting the API too
often -- see currency.py). Running this from the bar as often as the bar
likes therefore costs nothing and can never hammer the upstream API.

Output is Pango markup (like segment_indicator.py) so i3status-rs's `custom`
block with `format = "$text.pango-str()"` renders the colored trend arrow:
green up / red down / gray flat -- the same semantic tints the wallpaper
uses. Three states mirror the wallpaper exactly:

    live/stale : "為替 3,192 ▼"  (stale adds a muted 古 marker)
    unavailable: "為替 —"        (never blank, so a broken feed is visible)

Prints a single line and exits 0 even when data is missing, so the bar
block never errors out.
"""
import sys

from currency import DEFAULT_CACHE_PATH, load_cached_currency

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


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(currency) -> str:
    """Pure formatting -- returns the Pango-markup line for a CurrencyInfo
    (or None). Separated from I/O so it's unit-testable."""
    if currency is None or currency.status == "unavailable" or currency.current_rate is None:
        return f'{_LABEL} <span foreground="{_TREND["flat"][1]}">\u2014</span>'

    rate = f"{currency.current_rate:,.0f}"
    glyph, color = _TREND.get(currency.trend, _TREND["flat"])
    is_stale = currency.status == "stale"
    if is_stale:
        # Muted everything and add a 古 (old) marker so a frozen feed reads
        # as frozen in the bar, not as a live quote.
        return (f'<span foreground="{_STALE_COLOR}">{_LABEL} {rate} '
                f'{glyph} 古</span>')
    return f'{_LABEL} {rate} <span foreground="{color}">{glyph}</span>'


def main(argv=None) -> int:
    currency = load_cached_currency(DEFAULT_CACHE_PATH)
    print(render(currency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
