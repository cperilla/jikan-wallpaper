"""Per-day-segment color palettes, interpolated in OKLab (a perceptual color
space) rather than raw sRGB -- so blending two segments' colors together
produces perceptually even transitions instead of the muddy/uneven
midpoints plain RGB lerp gives you.

Palettes are AUTHORED in OKLCH (lightness, chroma, hue-angle) because hue
maps directly to "warm/cool" intuition, which is how the segment moods were
designed (night=cool blue hue, dusk=warm amber hue, etc). They're converted
to OKLab (Cartesian L,a,b) for storage/interpolation, since Cartesian lerp
has no circular-hue wraparound edge case the way interpolating hue-angle
directly would.
"""
import math
from dataclasses import dataclass, fields

# --- sRGB <-> OKLab (Bjorn Ottosson's published formulas) -----------------

def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def hex_to_oklab(hexcolor: str) -> tuple[float, float, float]:
    hexcolor = hexcolor.lstrip("#")
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    r, g, b = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)

    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (max(0.0, x) ** (1 / 3) for x in (l, m, s))

    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b_ = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return L, a, b_


def oklab_to_hex(L: float, a: float, b: float) -> str:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3

    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bl = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    r, g, bl = (_linear_to_srgb(x) for x in (r, g, bl))
    return "#" + "".join(f"{max(0, min(255, round(x * 255))):02X}" for x in (r, g, bl))


def oklch_to_oklab(L: float, C: float, H_deg: float) -> tuple[float, float, float]:
    h = math.radians(H_deg)
    return L, C * math.cos(h), C * math.sin(h)


def oklch_to_hex(L: float, C: float, H_deg: float) -> str:
    return oklab_to_hex(*oklch_to_oklab(L, C, H_deg))


def lerp_oklab(c1: str, c2: str, t: float) -> str:
    L1, a1, b1 = hex_to_oklab(c1)
    L2, a2, b2 = hex_to_oklab(c2)
    return oklab_to_hex(L1 + (L2 - L1) * t, a1 + (a2 - a1) * t, b1 + (b2 - b1) * t)


# --- Palette ---------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    background: str
    ghost: str
    tertiary: str
    secondary: str
    primary: str
    accent: str
    glow: str
    timeline_active: str


def lerp_palette(p1: Palette, p2: Palette, t: float) -> Palette:
    return Palette(**{f.name: lerp_oklab(getattr(p1, f.name), getattr(p2, f.name), t) for f in fields(Palette)})


# --- Per-segment definitions, authored in OKLCH (L 0-1, C ~0-0.4, H degrees) ----
# Design intent per segment (see conversation): night = darkest/coolest/lowest
# contrast; dawn = cool-clear, contrast rising; day = most neutral (lowest
# chroma) and highest contrast (widest primary-tertiary lightness gap) --
# "clearest, most legible"; dusk = warmest, highest accent chroma; evening =
# muted-warm, contrast compressing back down toward night.

_SEGMENT_OKLCH = {
    "night": {  # 深夜
        "background": (0.155, 0.015, 250), "ghost": (0.185, 0.020, 250),
        "tertiary": (0.320, 0.020, 245), "secondary": (0.400, 0.025, 245),
        "primary": (0.620, 0.020, 240), "accent": (0.550, 0.060, 250),
        "glow": (0.720, 0.050, 250), "timeline_active": (0.620, 0.090, 250),
    },
    "dawn": {  # 朝
        "background": (0.175, 0.015, 350), "ghost": (0.205, 0.020, 350),
        "tertiary": (0.370, 0.020, 340), "secondary": (0.460, 0.025, 340),
        "primary": (0.700, 0.020, 340), "accent": (0.620, 0.080, 350),
        "glow": (0.800, 0.060, 350), "timeline_active": (0.660, 0.110, 350),
    },
    "day": {  # 日中
        "background": (0.195, 0.008, 80), "ghost": (0.225, 0.010, 80),
        "tertiary": (0.400, 0.012, 80), "secondary": (0.500, 0.015, 80),
        "primary": (0.780, 0.015, 80), "accent": (0.650, 0.070, 80),
        "glow": (0.840, 0.050, 80), "timeline_active": (0.680, 0.100, 80),
    },
    "dusk": {  # 夕
        "background": (0.175, 0.020, 45), "ghost": (0.205, 0.025, 45),
        "tertiary": (0.360, 0.030, 45), "secondary": (0.440, 0.035, 45),
        "primary": (0.660, 0.030, 45), "accent": (0.600, 0.100, 45),
        "glow": (0.780, 0.080, 45), "timeline_active": (0.640, 0.130, 45),
    },
    "evening": {  # 夜
        "background": (0.165, 0.015, 55), "ghost": (0.195, 0.018, 55),
        "tertiary": (0.340, 0.018, 55), "secondary": (0.420, 0.020, 55),
        "primary": (0.640, 0.018, 55), "accent": (0.560, 0.055, 55),
        "glow": (0.730, 0.045, 55), "timeline_active": (0.600, 0.080, 55),
    },
}

# Order matters: this IS the cyclic segment order (night->dawn->day->dusk->evening->night)
SEGMENT_ORDER = ["night", "dawn", "day", "dusk", "evening"]
SEGMENT_LABEL_JA = {"night": "深夜", "dawn": "朝", "day": "日中", "dusk": "夕", "evening": "夜"}


def _build_palette(name: str) -> Palette:
    tokens = _SEGMENT_OKLCH[name]
    return Palette(**{k: oklch_to_hex(*v) for k, v in tokens.items()})


SEGMENT_PALETTES: dict[str, Palette] = {name: _build_palette(name) for name in SEGMENT_ORDER}

# Same transition shape as the old single-accent model: a logistic/sigmoid
# curve (not a plain logarithm -- see renderer.py's earlier note), stable
# through most of a segment, rapid change within a window centered on each
# boundary.
TRANSITION_HALF_WIDTH = 1.0
TRANSITION_STEEPNESS = 6.0


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def segment_bounds(sunrise_hour: float, work_start_hour: float, sunset_hour: float,
                    work_end_hour: float) -> list[float]:
    return [0.0, sunrise_hour, work_start_hour, sunset_hour, work_end_hour, 24.0]


def segment_index(hour: float, bounds: list[float]) -> int:
    for i in range(5):
        if bounds[i] <= hour < bounds[i + 1]:
            return i
    return 4


def current_segment_name(hour: float, sunrise_hour: float, sunset_hour: float,
                          work_start_hour: float, work_end_hour: float) -> str:
    bounds = segment_bounds(sunrise_hour, work_start_hour, sunset_hour, work_end_hour)
    return SEGMENT_ORDER[segment_index(hour, bounds)]


def current_palette(hour: float, sunrise_hour: float, sunset_hour: float,
                     work_start_hour: float, work_end_hour: float) -> Palette:
    """The live, smoothly-interpolated palette for a given decimal hour."""
    bounds = segment_bounds(sunrise_hour, work_start_hour, sunset_hour, work_end_hour)
    idx = segment_index(hour, bounds)
    seg_start, seg_end = bounds[idx], bounds[idx + 1]
    w = TRANSITION_HALF_WIDTH
    dist_to_end = seg_end - hour
    dist_to_start = hour - seg_start

    this_palette = SEGMENT_PALETTES[SEGMENT_ORDER[idx]]

    def _ease(dist: float) -> float:
        progress = max(0.0, min(1.0, (w - dist) / w))
        return 0.5 * _sigmoid(TRANSITION_STEEPNESS * (progress - 0.5))

    if dist_to_end < w:
        next_palette = SEGMENT_PALETTES[SEGMENT_ORDER[(idx + 1) % 5]]
        return lerp_palette(this_palette, next_palette, _ease(dist_to_end))
    if dist_to_start < w:
        prev_palette = SEGMENT_PALETTES[SEGMENT_ORDER[(idx - 1) % 5]]
        return lerp_palette(this_palette, prev_palette, _ease(dist_to_start))
    return this_palette
