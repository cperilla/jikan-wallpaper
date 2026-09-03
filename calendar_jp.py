"""Japanese calendar notation: weekday kanji, month notation, month grid,
and deterministic kanji-of-day selection."""
import calendar
from dataclasses import dataclass
from datetime import date

# Monday=0 .. Sunday=6, matching Python's date.weekday()
WEEKDAY_KANJI = ["月", "火", "水", "木", "金", "土", "日"]

# Kanji numerals for month notation (一月.."十二月")
MONTH_KANJI_DIGITS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"]


def weekday_kanji_header() -> list[str]:
    return list(WEEKDAY_KANJI)


def month_notation(d: date) -> str:
    """e.g. 九月 for September."""
    return f"{MONTH_KANJI_DIGITS[d.month - 1]}月"


@dataclass(frozen=True)
class MonthGrid:
    weeks: list[list[int | None]]  # each week: 7 entries (Mon..Sun), None = no day
    today_day: int | None          # day-of-month if `d`'s month/year matches today, else None


def month_grid(d: date) -> MonthGrid:
    """Week rows (Mon-start), matching WEEKDAY_KANJI order. `calendar.monthcalendar`
    already returns Mon-start weeks with 0 for out-of-month days."""
    raw = calendar.monthcalendar(d.year, d.month)
    weeks = [[(day if day != 0 else None) for day in week] for week in raw]
    return MonthGrid(weeks=weeks, today_day=d.day)


def pick_kanji_of_day(d: date, kanji_list: list[str]) -> str:
    """Deterministic selection: date ordinal modulo list length. No hashing --
    stable, human-debuggable (you can compute by hand which index a date maps to)."""
    if not kanji_list:
        raise ValueError("kanji_list must not be empty")
    return kanji_list[d.toordinal() % len(kanji_list)]
