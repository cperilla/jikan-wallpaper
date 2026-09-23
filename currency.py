"""USD/COP exchange rate for the 為替 module (official TRM, no API key).

Shows today's official representative market rate (Tasa Representativa del
Mercado) with its min/max and an up/down trend arrow, plus a GitHub-style
weekly strip -- one cell per previous day, tinted by that day's rate and
labelled with the weekday kanji. Ambient financial context, refreshed at
the normal 15-minute wallpaper regeneration, never on a separate timer.

Note on granularity: TRM is a single value per effective date (the source
has no intraday ticks), so "today's min/max" is derived from the trailing
daily series that the weekly strip also shows, and "trend" compares the
latest day to the one before it -- not intraday movement.

Source: Colombia's open-data Socrata dataset `32sa-8pi3` on datos.gov.co
(Superintendencia Financiera's daily TRM). Keyless JSON with SoQL, so a
single request returns the trailing-week window from which the daily
series / min / max / trend are all derived -- no per-day loop. The endpoint
lives in ONE place (`_API_BASE`/`_SOQL_*`) so a different source (a market
spot API, the wilkinson page, frankfurter.dev, ...) can be swapped in
without touching anything else.

Politeness / abuse guard: `get_currency()` loads the on-disk cache first
and, if the cached response is younger than `min_refresh_seconds` (default
just under the 15-minute regen cycle), returns it with NO network call at
all. Since the wallpaper also regenerates on service start, resume, and
manual runs, this TTL is what keeps several regens in a short window from
each hitting the API -- the endpoint sees at most one request per TTL.

Failure handling differs *deliberately* from every other environmental
module (天/雨/日 omit themselves when data is missing). A silently vanished
exchange rate is exactly the kind of thing you want to notice, so this
module ALWAYS renders and surfaces one of three states via `status`:

    "live"        fresh data from this run's fetch
    "stale"       fetch failed but a prior cached rate exists (shown, marked old)
    "unavailable" fetch failed and no cache exists (visible error stub)

`get_currency()` therefore never returns None and never raises -- it always
returns a CurrencyInfo, `unavailable` in the worst case. `explain.py` reads
the cache only (never fetches), so the popup reflects what the wallpaper
last showed.
"""
import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_CACHE_PATH = Path.home() / ".local" / "state" / "jikan" / "currency_cache.json"

# Just under the 15-minute (900 s) regen cycle: the normal timer refetches
# each cycle, but startup/resume/manual runs landing in the same window
# reuse the cache instead of hitting the API again.
DEFAULT_MIN_REFRESH_SECONDS = 840.0

# The ONE place the data source is defined. datos.gov.co Socrata TRM
# dataset. SoQL params are URL-encoded at request time (see fetch_currency);
# {since} is an ISO date 7 days back. Rows come back newest-effective first.
_API_BASE = "https://www.datos.gov.co/resource/32sa-8pi3.json"
_SOQL_WHERE = "vigenciahasta >= '{since}'"
_SOQL_ORDER = "vigenciadesde DESC"
_SOQL_LIMIT = 50

# Human label for the pair this module tracks.
PAIR_LABEL = "USD/COP"


@dataclass(frozen=True)
class CurrencyInfo:
    current_rate: float | None       # latest daily TRM value (None only when unavailable)
    week_min: float | None           # min over the trailing window (across the shown days)
    week_max: float | None           # max over the trailing window
    as_of_date: str                  # ISO date the current rate is effective from ("" if unavailable)
    fetched_at: str                  # ISO timestamp of the fetch that produced this
    trend: str = "flat"              # "up" | "down" | "flat" | "" (unavailable) -- today vs prior day
    days: list = field(default_factory=list)  # trailing daily series oldest..newest: [[iso_date, rate], ...]
    pair: str = PAIR_LABEL
    is_cached: bool = False          # served from disk cache rather than this run's fetch
    status: str = "live"             # "live" | "stale" | "unavailable"


# TRM is a single value per effective date; the source has no intraday
# ticks, so "today's min/max" is derived from the trailing daily series
# (the same days the weekly squares show), and "trend" compares the latest
# day to the one before it. How many trailing days the weekly strip shows.
WEEK_DAYS = 7


def _expand_daily_series(payload: list[dict], now: datetime) -> list[tuple[str, float]]:
    """TRM rows carry an effective *range* (vigenciadesde..vigenciahasta):
    a Friday row covers Sat/Sun too. Expand to one (iso_date, rate) entry
    per calendar day in the trailing WEEK_DAYS window, oldest first, so the
    GitHub-style weekly strip has a cell per day with the right value."""
    # date -> rate, filling every day a row's range covers.
    by_day: dict[str, float] = {}
    for row in payload:
        rate = float(row["valor"])
        d0 = datetime.fromisoformat(str(row["vigenciadesde"])).date()
        d1 = datetime.fromisoformat(str(row["vigenciahasta"])).date()
        day = d0
        while day <= d1:
            by_day[day.isoformat()] = rate
            day += timedelta(days=1)
    # Keep only the trailing WEEK_DAYS up to today, oldest..newest.
    today = now.date()
    window = []
    for i in range(WEEK_DAYS - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        if d in by_day:
            window.append((d, by_day[d]))
    # If the API window didn't reach today (e.g. weekend/holiday), fall back
    # to the most recent WEEK_DAYS entries we do have.
    if not window:
        items = sorted(by_day.items())
        window = items[-WEEK_DAYS:]
    return window


def _parse_rows(payload: list[dict], now: datetime | None = None):
    """Turn raw SoQL rows into (current, week_min, week_max, trend, days,
    as_of_date).

    `days` is the trailing daily series oldest..newest ([iso_date, rate]).
    `current` is the newest day's rate; `trend` compares it to the prior
    day ("up"/"down"/"flat"). min/max are over the shown days. Raises on
    empty/garbage input so the caller treats it as a fetch failure."""
    now = now or datetime.now()
    series = _expand_daily_series(payload, now)
    if not series:
        raise ValueError("no TRM rows in response")
    rates = [r for _, r in series]
    current = rates[-1]
    week_min, week_max = min(rates), max(rates)
    if len(rates) >= 2:
        prev = rates[-2]
        trend = "up" if current > prev else "down" if current < prev else "flat"
    else:
        trend = "flat"
    as_of_date = series[-1][0]
    days = [[d, round(r, 2)] for d, r in series]
    return current, week_min, week_max, trend, days, as_of_date


def fetch_currency(timeout: float = 4.0, now: datetime | None = None) -> CurrencyInfo:
    """One live TRM request covering the trailing 7 days. Raises on any
    failure -- the caller (get_currency) decides the fallback."""
    now = now or datetime.now()
    since = (now - timedelta(days=7)).date().isoformat()
    query = urllib.parse.urlencode({
        "$where": _SOQL_WHERE.format(since=since),
        "$order": _SOQL_ORDER,
        "$limit": _SOQL_LIMIT,
    })
    url = f"{_API_BASE}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    current, week_min, week_max, trend, days, as_of_date = _parse_rows(payload, now)
    return CurrencyInfo(
        current_rate=round(current, 2),
        week_min=round(week_min, 2),
        week_max=round(week_max, 2),
        trend=trend,
        days=days,
        as_of_date=as_of_date,
        fetched_at=now.isoformat(timespec="seconds"),
        is_cached=False,
        status="live",
    )


def _save_cache(info: CurrencyInfo, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(asdict(info), ensure_ascii=False), encoding="utf-8")


def load_cached_currency(cache_path: Path = DEFAULT_CACHE_PATH) -> "CurrencyInfo | None":
    """Pure read, no network -- used by explain.py so the popup reflects
    exactly what the wallpaper's last render showed. Returns None only when
    there is no usable cache at all."""
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        raw["is_cached"] = True
        # Tolerate an older cache shape written before trend/days existed:
        # fill sane defaults so a stale-but-real rate still loads and shows.
        raw.setdefault("trend", "flat")
        raw.setdefault("days", [])
        # A cache is by definition not a fresh "live" read; callers that
        # want to display it mark it stale themselves. Keep whatever status
        # was stored so explain.py can tell an old-but-real rate from a
        # never-succeeded one.
        return CurrencyInfo(**raw)
    except Exception:
        return None


def _unavailable(now: datetime) -> CurrencyInfo:
    return CurrencyInfo(
        current_rate=None,
        week_min=None,
        week_max=None,
        trend="",
        days=[],
        as_of_date="",
        fetched_at=now.isoformat(timespec="seconds"),
        is_cached=False,
        status="unavailable",
    )


def get_currency(cache_path: Path = DEFAULT_CACHE_PATH,
                 timeout: float = 4.0,
                 min_refresh_seconds: float = DEFAULT_MIN_REFRESH_SECONDS,
                 now: datetime | None = None) -> CurrencyInfo:
    """Fetch-with-TTL-fallback. The only function main.py should call.

    Order of operations, chosen to be polite to the API:
      1. Load the cache. If it is younger than min_refresh_seconds, return
         it verbatim (status stays "live") with NO network call.
      2. Otherwise attempt exactly one live fetch; on success cache + return.
      3. On fetch failure, return the cache marked "stale" if one exists,
         else an "unavailable" sentinel.

    Never raises; never returns None.
    """
    now = now or datetime.now()
    cached = load_cached_currency(cache_path)

    if cached is not None and cached.fetched_at:
        try:
            age = (now - datetime.fromisoformat(cached.fetched_at)).total_seconds()
        except Exception:
            age = None
        if age is not None and 0 <= age < min_refresh_seconds:
            # Fresh enough -- serve the cache, no request. is_cached is True
            # (set by load_cached_currency) but the data is current, so the
            # status it carries is preserved rather than downgraded.
            return cached

    try:
        info = fetch_currency(timeout=timeout, now=now)
        _save_cache(info, cache_path)
        return info
    except Exception:
        if cached is not None:
            # Show the last good rate, clearly marked as no longer fresh.
            from dataclasses import replace
            return replace(cached, status="stale", is_cached=True)
        return _unavailable(now)
