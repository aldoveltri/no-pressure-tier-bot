"""GEXP requirement maths (no Discord code here, so it can be tested on its own).

Rules of the guild:
  - 55,000 GEXP per week.
  - The week ends (purge) every Tuesday at 12:00 AM Eastern Time (America/New_York).
  - A member's requirement is proportional to the exact time they have left before the next purge,
    down to the second: 55,000 * (time left) / (length of the week).
  - Extra GEXP "debt" is tracked separately: the normal requirement must be met first, only the GEXP
    earned beyond it pays the debt, and what is left carries over to the next weeks.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Denver")
UTC = timezone.utc

WEEKLY_REQ = 55000
PURGE_WEEKDAY = 1        # Monday = 0, Tuesday = 1


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def secs(a: datetime, b: datetime) -> int:
    """Real seconds between two aware datetimes (computed in UTC, so daylight saving never skews it)."""
    return int((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds())


def week_start(dt: datetime) -> datetime:
    """The most recent Tuesday 12:00 AM ET at or before dt (aware datetime in ET)."""
    local = dt.astimezone(ET)
    back = (local.weekday() - PURGE_WEEKDAY) % 7
    return local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=back)


def week_end(dt: datetime) -> datetime:
    """The next purge: Tuesday 12:00 AM ET after dt."""
    return week_start(dt) + timedelta(days=7)


def week_len(dt: datetime) -> int:
    """Seconds in the week that contains dt (168 hours, or 167/169 in a week with a clock change)."""
    return secs(week_start(dt), week_end(dt))


def required_from(join: datetime, req: int = WEEKLY_REQ) -> int:
    """Normal requirement for someone who joins (or rejoins) at 'join': proportional to the time left
    until the next purge, rounded up to a whole GEXP."""
    remaining = max(0, secs(join, week_end(join)))
    return min(req, ceil_div(req * remaining, week_len(join)))


def required_elapsed(start: datetime, end: datetime, req: int = WEEKLY_REQ) -> int:
    """GEXP that should have been earned between start and end (inside one week): used for the
    GEXP a player owes when they are put on the breaklist."""
    elapsed = max(0, secs(start, end))
    return min(req, ceil_div(req * elapsed, week_len(start)))


def settle_debt(normal: int, debt: int, progress: int) -> dict:
    """progress = GEXP earned since joining. Returns the full picture of the week."""
    progress = max(0, progress)
    still_normal = max(0, normal - progress)           # normal requirement still to earn
    extra = max(0, progress - normal)                  # GEXP beyond the normal requirement
    paid = min(debt, extra)
    remaining_debt = debt - paid
    return {
        "normal": normal, "debt": debt, "target": normal + debt, "progress": progress,
        "still_normal": still_normal, "paid": paid, "remaining_debt": remaining_debt,
        "still_total": max(0, normal + debt - progress),
        "normal_met": progress >= normal,
        "all_clear": progress >= normal + debt,
    }


def weekly_sum(history: dict, ws: datetime) -> int:
    """Sum of the GEXP of the days from the start of the week (history = {'YYYY-MM-DD': gexp}, days in ET)."""
    first = ws.astimezone(ET).date().isoformat()
    return sum(int(v) for day, v in history.items() if day >= first)


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m"


def fmt_days(days: float) -> str:
    return fmt_duration(days * 86400)


def fmt_date(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%m/%d/%Y")


def fmt_et(dt: datetime) -> str:
    local = dt.astimezone(ET)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%m/%d/%Y')} {hour}:{local.strftime('%M %p')} ET"


_TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I %p")


def parse_et(text) -> "datetime | None":
    """Eastern Time. Date first, optional time after it:
       '2026/10/21 6:00 PM', '2026/10/21 18:00', '2026/10/21' (= midnight),
       '10/21 6:00 PM' (no year: the current year), also the old '10/21/2026 6:00 PM' and '2026-10-21'.
       Returns None if unreadable."""
    text = re.sub(r"(\d)\s*(am|pm)\b", r"\1 \2", (text or "").strip(), flags=re.I).upper()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    date_txt, _, time_txt = text.partition(" ")
    date = None
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", date_txt)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_txt)
        if m:
            mo, d, y = map(int, m.groups())
        else:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", date_txt)
            if not m:
                return None
            mo, d = map(int, m.groups())
            y = datetime.now(ET).year
    hh = mm = 0
    if time_txt:
        t = None
        for fmt in _TIME_FORMATS:
            try:
                t = datetime.strptime(time_txt.strip(), fmt)
                break
            except ValueError:
                continue
        if t is None:
            return None
        hh, mm = t.hour, t.minute
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=ET)
    except ValueError:
        return None
