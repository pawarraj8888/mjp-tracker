"""Timezone helpers. Everything the product shows a person is in IST
(Asia/Kolkata, UTC+05:30, no DST), stored alongside a timezone-aware ISO value
so displays are consistent regardless of where the crawler runs.

Leaf module (no project imports) so tracker.py, the pipeline and app.py share
one implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def now_ist() -> datetime:
    return datetime.now(IST)


def iso_ist(dt: datetime | None = None) -> str:
    """Timezone-aware ISO 8601 string in IST, e.g. 2026-09-12T01:30:00+05:30."""
    return (dt or now_ist()).astimezone(IST).isoformat(timespec="seconds")


def to_ist(iso: str | None) -> datetime | None:
    """Parse a stored ISO timestamp (any offset, or naive=assumed IST) to an
    IST-aware datetime. Returns None on failure."""
    if not iso:
        return None
    s = str(iso).strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                    "%d-%b-%Y %I:%M %p", "%d-%b-%Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def fmt_ist(iso_or_dt) -> str:
    """Human IST label, e.g. '12 Sep 2026, 01:30 IST'."""
    dt = iso_or_dt if isinstance(iso_or_dt, datetime) else to_ist(iso_or_dt)
    if dt is None:
        return ""
    return dt.strftime("%d %b %Y, %H:%M IST")


def relative_age(iso_or_dt, ref: datetime | None = None) -> str:
    """'12 minutes ago' style relative age from an IST-aware reference."""
    dt = iso_or_dt if isinstance(iso_or_dt, datetime) else to_ist(iso_or_dt)
    if dt is None:
        return ""
    ref = ref or now_ist()
    secs = int((ref - dt).total_seconds())
    if secs < 0:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return "%d %s%s ago" % (n, unit, "s" if n != 1 else "")
    return "just now"
