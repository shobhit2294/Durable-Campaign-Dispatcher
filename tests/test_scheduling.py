from datetime import datetime

from campaign_dispatcher.scheduling import IST, in_window, is_calling_time, next_eligible_time, priority


def test_window_is_start_inclusive_and_end_exclusive():
    assert in_window(datetime(2026, 11, 12, 10, 0, tzinfo=IST), "10:00", "19:00", ["thu"])
    assert not in_window(datetime(2026, 11, 12, 19, 0, tzinfo=IST), "10:00", "19:00", ["thu"])


def test_window_respects_allowed_days():
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=IST)
    assert not in_window(saturday, "09:30", "18:00", ["mon", "tue", "wed", "thu", "fri"])


def test_next_eligible_respects_campaign_and_regulatory_windows():
    campaign = {"window": {"start": "10:00", "end": "19:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat"]}}
    regulatory = {"start": "09:00", "end": "21:00"}
    now = datetime(2026, 11, 12, 20, 0, tzinfo=IST)
    assert next_eligible_time(now, campaign, regulatory) == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_calling_time_requires_both_windows():
    campaign = {"window": {"start": "10:00", "end": "20:30", "days": ["thu"]}}
    regulatory = {"start": "09:00", "end": "21:00"}
    assert is_calling_time(datetime(2026, 11, 12, 20, 0, tzinfo=IST), campaign, regulatory)
    assert not is_calling_time(datetime(2026, 11, 12, 20, 45, tzinfo=IST), campaign, regulatory)


def test_due_callbacks_precede_due_retries_and_fresh_leads():
    now = datetime(2026, 11, 12, 12, 0, tzinfo=IST)
    callback = {"campaign_id": "c", "lead_id": "a", "callback_at": now.isoformat()}
    retry = {"campaign_id": "c", "lead_id": "b", "retry_at": now.isoformat()}
    fresh = {"campaign_id": "c", "lead_id": "c"}
    assert sorted((fresh, retry, callback), key=lambda lead: priority(lead, now)) == [callback, retry, fresh]
