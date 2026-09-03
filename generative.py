"""Deterministic supporting-geometry variation.

Two separate determinism mechanisms exist in this project, deliberately kept
apart:
  1. Kanji-of-day selection (calendar_jp.pick_kanji_of_day) -- a plain
     date-ordinal modulo, not touched by randomness at all.
  2. This module -- small jittered variations in the *supporting* geometry
     (year-progress field rotation, hairline position, small anchor shifts),
     seeded per-concern from a hash of the seed string so that adding a new
     random draw later never reshuffles values for dates that already
     "shipped".

A local `random.Random` is always used, never the global `random.seed()`.
"""
import hashlib
import random
from dataclasses import dataclass


def make_rng(seed_str: str, namespace: str) -> random.Random:
    digest = hashlib.sha256(f"{seed_str}|{namespace}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


@dataclass(frozen=True)
class LayoutVariation:
    year_progress_rotation_deg: float
    year_progress_row_phase: float       # 0..1, per-row jitter phase seed
    guideline_y_offset_px: float
    calendar_shift_x_px: float
    calendar_shift_y_px: float
    kanji_scale_pct: float                # e.g. 0.97..1.03
    kanji_baseline_shift_px: float


def build_variation(seed_str: str) -> LayoutVariation:
    r_yp = make_rng(seed_str, "year_progress")
    r_guide = make_rng(seed_str, "guideline")
    r_cal = make_rng(seed_str, "calendar_shift")
    r_kanji = make_rng(seed_str, "kanji_shift")

    return LayoutVariation(
        year_progress_rotation_deg=r_yp.uniform(-2.0, 2.0),
        year_progress_row_phase=r_yp.random(),
        guideline_y_offset_px=r_guide.uniform(-12.0, 12.0),
        calendar_shift_x_px=r_cal.uniform(-30.0, 30.0),
        calendar_shift_y_px=r_cal.uniform(-20.0, 20.0),
        kanji_scale_pct=r_kanji.uniform(0.97, 1.03),
        kanji_baseline_shift_px=r_kanji.uniform(-15.0, 15.0),
    )
