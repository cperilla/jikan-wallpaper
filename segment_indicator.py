#!/usr/bin/env python3
"""Compact i3bar indicator: current day segment (深夜/朝/日中/夕/夜), colored
with the live theme accent -- the same "work/life cycle" concept as the
wallpaper's 24h day bar, condensed to one glyph next to the clock.

Label is the hard current segment (no blending -- it's discrete); color is
the smoothly-blended live accent from the same resolve_palette() the
wallpaper and i3 theme use, so a transition between segments shows as a
color shift even though the glyph itself only changes at the boundary.
"""
import sys
from datetime import date, datetime

from config import DEFAULT_CONFIG_PATH, load_config
from palette import SEGMENT_LABEL_JA, current_segment_name
from renderer import _parse_hhmm, resolve_palette


def main() -> int:
    cfg = load_config(DEFAULT_CONFIG_PATH)
    today = date.today()
    now = datetime.now()

    palette, _, sr_hour, ss_hour = resolve_palette(today, now, cfg)
    hour = now.hour + now.minute / 60
    work_start_hour = _parse_hhmm(cfg.schedule.work_start)
    work_end_hour = _parse_hhmm(cfg.schedule.work_end)
    seg = current_segment_name(hour, sr_hour, ss_hour, work_start_hour, work_end_hour)
    label = SEGMENT_LABEL_JA[seg]

    print(f'<span foreground="{palette.accent}">{label}</span>')
    return 0


if __name__ == "__main__":
    sys.exit(main())
