"""Apply a generated PNG as the desktop wallpaper."""
import re
import subprocess


def detect_output_geometries() -> list[tuple[int, int, int, int]]:
    """Connected output (width, height, x, y), in xrandr's own order
    (primary first). feh's xinerama-aware --bg-fill maps positional image
    arguments to screens in that same order; the (x, y) offsets are for
    compositing a single combined image for tools (i3lock) that only take
    one image and have no per-output awareness at all."""
    result = subprocess.run(["xrandr", "--query"], check=True, capture_output=True, text=True)
    geometries = []
    for line in result.stdout.splitlines():
        if " connected " not in line:
            continue
        m = re.search(r"(\d+)x(\d+)\+(\d+)\+(\d+)", line)
        if m:
            geometries.append(tuple(int(g) for g in m.groups()))
    return geometries


def set_wallpaper(paths: list[str], setter: str = "feh") -> None:
    """One correctly-sized image per connected output, in xrandr's own
    order (see main.py, which renders one variant per distinct detected
    resolution via the SVG renderer's own width/height scaling).

    Deliberately NOT the same 2560x1440 render repeated and left to feh's
    --bg-fill cover-crop on a differently-shaped screen -- confirmed live
    that this cuts straight through the giant kanji and calendar on a
    2560x1080 second monitor (cover-fill scales to the larger of the two
    fit ratios then center-crops the rest, which for an image already at
    the target's width just center-crops the height with no scaling at
    all)."""
    if setter != "feh":
        raise ValueError(f"unsupported wallpaper setter: {setter!r}")
    subprocess.run(["feh", "--bg-fill", *paths], check=True)
