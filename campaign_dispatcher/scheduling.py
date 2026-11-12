"""Pure scheduling rules for campaign windows and lead priority."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# India has no daylight-saving transition; this fixed offset also works on Windows
# installations that do not ship the IANA timezone database.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def in_window(instant: datetime, start: str, end: str, days: list[str] | None) -> bool:
    """Return whether an instant is inside a local-time [start, end) window."""
    local = instant.astimezone(IST)
    if days is not None and DAY_NAMES[local.weekday()] not in days:
        return False
    return _clock(start) <= local.timetz().replace(tzinfo=None) < _clock(end)


def is_calling_time(now: datetime, campaign: dict, regulatory: dict) -> bool:
    return in_window(now, regulatory["start"], regulatory["end"], None) and in_window(
        now, campaign["window"]["start"], campaign["window"]["end"], campaign["window"]["days"]
    )


def next_window_open(instant: datetime, start: str, end: str, days: list[str]) -> datetime:
    """Find the first instant at or after ``instant`` inside a weekly window."""
    local = instant.astimezone(IST)
    opening = _clock(start)
    closing = _clock(end)
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        if DAY_NAMES[day.weekday()] not in days:
            continue
        candidate = datetime.combine(day, opening, IST)
        close_at = datetime.combine(day, closing, IST)
        if offset == 0:
            if local < candidate:
                return candidate
            if local < close_at:
                return local
            continue
        return candidate
    raise ValueError("campaign window has no eligible day")


def next_eligible_time(instant: datetime, campaign: dict, regulatory: dict) -> datetime:
    """Find the next time both campaign and regulatory windows are open."""
    candidate = instant.astimezone(IST)
    for _ in range(16):
        campaign_open = next_window_open(
            candidate, campaign["window"]["start"], campaign["window"]["end"], campaign["window"]["days"]
        )
        regulatory_open = next_window_open(campaign_open, regulatory["start"], regulatory["end"], list(DAY_NAMES))
        if is_calling_time(regulatory_open, campaign, regulatory):
            return regulatory_open
        candidate = regulatory_open + timedelta(seconds=1)
    raise ValueError("no overlapping campaign and regulatory window found")


def priority(lead: dict, now: datetime) -> tuple[int, datetime, str, str]:
    """Sort due callbacks first, due retries second, and fresh leads last."""
    callback = datetime.fromisoformat(lead["callback_at"]) if lead.get("callback_at") else None
    retry = datetime.fromisoformat(lead["retry_at"]) if lead.get("retry_at") else None
    if callback and callback <= now:
        return (0, callback, lead["campaign_id"], lead["lead_id"])
    if retry and retry <= now:
        return (1, retry, lead["campaign_id"], lead["lead_id"])
    return (2, now, lead["campaign_id"], lead["lead_id"])
