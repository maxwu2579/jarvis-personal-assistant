"""Phase 11A Today: IANA timezone day boundaries.

The local day is [first instant of the local date, first instant of the next local date).
Boundaries come from calendar-date construction in the requested zone, never from adding
24 hours to an instant. Expectations are computed against the installed tzdata.
"""

import zoneinfo
from datetime import date, datetime, time, timedelta, timezone, tzinfo

import pytest

from app.services import today_service
from app.services.today_service import (
    TodayBoundaryError,
    compute_day_window,
    first_instant_of_local_date,
    local_midnight_exists,
    resolve_timezone,
)
from tests.test_phase11a_today_projection import add_reminder, add_task, db, get_today, iso, set_clock, today  # noqa: F401

UTC = timezone.utc
US = timedelta(microseconds=1)
VALID_ZONES = ['UTC', 'Etc/UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/London', 'Asia/Kathmandu',
               'Australia/Lord_Howe', 'Pacific/Kiritimati', 'Pacific/Pago_Pago', 'Africa/Cairo', 'America/Havana']


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def window(zone_name, now):
    return compute_day_window(now, zoneinfo.ZoneInfo(zone_name))


# ------------------------------------------------------------------ zone resolution

@pytest.mark.parametrize('name', VALID_ZONES)
def test_installed_iana_zones_resolve(name):
    assert resolve_timezone(name) == zoneinfo.ZoneInfo(name)


@pytest.mark.parametrize('name', [None, '', ' ', 'Mars/Phobos', '+08:00', 'UTC+8', 'GMT+8', 'asia/shanghai',
                                  ' Asia/Shanghai', 'Asia/Shanghai ', '../../etc/passwd', 'Asia/../UTC',
                                  'Asia/Shanghai\x00', 'localtime', 'A' * 65, 8, ['UTC'], b'UTC'])
def test_everything_else_is_rejected_without_fallback(name):
    assert resolve_timezone(name) is None


# ------------------------------------------------------------------ ordinary and DST days

def test_ordinary_day():
    w = window('Asia/Shanghai', utc(2026, 8, 11, 4, 0))
    assert w.local_date == date(2026, 8, 11)
    assert (w.day_start, w.day_end) == (utc(2026, 8, 10, 16), utc(2026, 8, 11, 16))
    assert w.day_end - w.day_start == timedelta(hours=24)
    assert w.lookback_start == utc(2026, 8, 4, 16)  # 2026-08-05 00:00 +08:00: seven local dates with today


def test_utc_day():
    w = window('UTC', utc(2026, 1, 1, 0, 0))
    assert (w.local_date, w.day_start, w.day_end) == (date(2026, 1, 1), utc(2026, 1, 1), utc(2026, 1, 2))


def test_spring_forward_day_is_23_hours():
    w = window('America/New_York', utc(2026, 3, 8, 15, 0))
    assert w.local_date == date(2026, 3, 8)
    assert (w.day_start, w.day_end) == (utc(2026, 3, 8, 5), utc(2026, 3, 9, 4))
    assert w.day_end - w.day_start == timedelta(hours=23)
    assert w.day_end != w.day_start + timedelta(hours=24)


def test_fall_back_day_is_25_hours():
    w = window('America/New_York', utc(2026, 11, 1, 15, 0))
    assert w.local_date == date(2026, 11, 1)
    assert (w.day_start, w.day_end) == (utc(2026, 11, 1, 4), utc(2026, 11, 2, 5))
    assert w.day_end - w.day_start == timedelta(hours=25)
    assert w.day_end != w.day_start + timedelta(hours=24)


def test_half_hour_dst_days():
    """Australia/Lord_Howe shifts by 30 minutes: 24.5-hour and 23.5-hour days."""
    lengths = set()
    zone = zoneinfo.ZoneInfo('Australia/Lord_Howe')
    for offset in range(366):
        d = date(2026, 1, 1) + timedelta(days=offset)
        lengths.add(first_instant_of_local_date(d + timedelta(days=1), zone) - first_instant_of_local_date(d, zone))
    assert lengths == {timedelta(hours=23, minutes=30), timedelta(hours=24), timedelta(hours=24, minutes=30)}


def test_lookback_is_calendar_dates_across_a_dst_change():
    w = window('America/New_York', utc(2026, 3, 10, 15, 0))  # local 2026-03-10, after the 8 March change
    assert w.lookback_start == utc(2026, 3, 4, 5)            # 2026-03-04 00:00 EST, not day_start minus 144 hours
    assert w.day_start - w.lookback_start == timedelta(hours=143)


# ------------------------------------------------------------------ instants around local midnight

@pytest.mark.parametrize('zone_name, midnight_utc, local', [
    ('Asia/Shanghai', utc(2026, 8, 10, 16), date(2026, 8, 11)),
    ('America/New_York', utc(2026, 11, 2, 5), date(2026, 11, 2)),
    ('Asia/Kathmandu', utc(2026, 5, 31, 18, 15), date(2026, 6, 1)),
    ('Pacific/Kiritimati', utc(2026, 5, 31, 10), date(2026, 6, 1)),
    ('Pacific/Pago_Pago', utc(2026, 6, 1, 11), date(2026, 6, 1)),
    ('Europe/London', utc(2026, 6, 30, 23), date(2026, 7, 1)),
])
def test_local_date_flips_exactly_at_local_midnight(zone_name, midnight_utc, local):
    before = window(zone_name, midnight_utc - US)
    at = window(zone_name, midnight_utc)
    after = window(zone_name, midnight_utc + US)
    assert before.local_date == local - timedelta(days=1)
    assert at.local_date == after.local_date == local
    assert at.day_start == midnight_utc and before.day_end == midnight_utc  # contiguous, no gap, no overlap
    assert before.day_start < midnight_utc - US < before.day_end
    assert at.day_start <= midnight_utc < at.day_end


@pytest.mark.parametrize('zone_name', VALID_ZONES)
def test_consecutive_days_tile_the_timeline_and_contain_now(zone_name):
    zone = zoneinfo.ZoneInfo(zone_name)
    now = utc(2026, 1, 1, 12, 34, 56)
    for _ in range(400):
        w = compute_day_window(now, zone)
        assert w.day_start <= now < w.day_end
        assert w.day_start.astimezone(zone).date() == w.local_date
        assert (w.day_start - US).astimezone(zone).date() < w.local_date
        assert (w.day_end - US).astimezone(zone).date() == w.local_date
        assert first_instant_of_local_date(w.local_date + timedelta(days=1), zone) == w.day_end
        now += timedelta(hours=23, minutes=7)


# ------------------------------------------------------------------ nonexistent local midnight

@pytest.mark.parametrize('zone_name, local', [('Africa/Cairo', date(2026, 4, 24)), ('America/Havana', date(2026, 3, 8))])
def test_nonexistent_midnight_starts_at_the_first_valid_instant(zone_name, local):
    zone = zoneinfo.ZoneInfo(zone_name)
    assert local_midnight_exists(local - timedelta(days=1), zone) is True
    assert local_midnight_exists(local, zone) is False  # wall-clock 00:00 is skipped on this date
    start = first_instant_of_local_date(local, zone)
    first_local = start.astimezone(zone)
    assert first_local.date() == local and first_local.time() == time(1, 0)  # the day begins at 01:00 local
    assert (start - US).astimezone(zone).date() == local - timedelta(days=1)
    assert (start - US).astimezone(zone).time() == time(23, 59, 59, 999999)
    w = compute_day_window(start + timedelta(hours=3), zone)
    assert w.local_date == local and w.day_start == start
    assert w.day_end - w.day_start == timedelta(hours=23)
    previous = compute_day_window(start - US, zone)
    assert previous.local_date == local - timedelta(days=1) and previous.day_end == start


class _GapAcrossMidnight(tzinfo):
    """Synthetic zone whose forward jump spans midnight (23:30 -> 00:30 on 2026-06-01).

    The fold=0 candidate for 2026-06-01 00:00 lands 30 minutes after the true first instant,
    so the round-trip proof must fail and the service must fail closed.
    """

    transition_utc = datetime(2026, 5, 31, 23, 30)
    gap_end_wall = datetime(2026, 6, 1, 0, 30)

    def utcoffset(self, dt):
        return timedelta(hours=1) if dt.replace(tzinfo=None) >= self.gap_end_wall else timedelta(0)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return 'SYN'

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        return (naive + (timedelta(hours=1) if naive >= self.transition_utc else timedelta(0))).replace(tzinfo=self)


class _InconsistentZone(tzinfo):
    """Synthetic zone whose round trip lands on a different date."""

    def utcoffset(self, dt):
        return timedelta(hours=1)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return 'BAD'

    def fromutc(self, dt):
        return dt.replace(tzinfo=self)


def test_boundary_proof_fails_closed_instead_of_guessing():
    with pytest.raises(TodayBoundaryError) as error:
        first_instant_of_local_date(date(2026, 6, 1), _GapAcrossMidnight())
    assert str(error.value) == 'Today day boundary could not be proven'  # fixed text: no zone, no date
    with pytest.raises(TodayBoundaryError):
        first_instant_of_local_date(date(2026, 6, 1), _InconsistentZone())
    with pytest.raises(TodayBoundaryError):
        compute_day_window(datetime(2026, 6, 1, 12, 0), zoneinfo.ZoneInfo('UTC'))  # naive "now" is never assumed
    assert first_instant_of_local_date(date(2026, 5, 20), _GapAcrossMidnight()) == utc(2026, 5, 20)


def test_service_never_adds_a_fixed_day_length():
    import inspect

    source = inspect.getsource(today_service)
    assert 'hours=24' not in source and 'hours = 24' not in source and '86400' not in source
    assert 'datetime.now(' not in source and 'utcnow(' not in source and 'tzlocal' not in source


# ------------------------------------------------------------------ through the API

def test_api_reflects_zone_date_and_boundaries(client):
    set_clock(utc(2026, 11, 1, 15, 0))
    body = today(client, 'America/New_York')
    assert (body['timezone'], body['local_date']) == ('America/New_York', '2026-11-01')
    assert (body['day_start'], body['day_end']) == ('2026-11-01T04:00:00Z', '2026-11-02T05:00:00Z')
    assert body['generated_at'] == '2026-11-01T15:00:00Z'
    # The same instant is a different local day elsewhere.
    assert today(client, 'Pacific/Kiritimati')['local_date'] == '2026-11-02'
    assert today(client, 'Pacific/Pago_Pago')['local_date'] == '2026-11-01'
    assert today(client, 'Asia/Shanghai')['day_start'] == '2026-10-31T16:00:00Z'


def test_fall_back_day_keeps_its_25th_hour(client, db):
    """start + 24h would end this day at 04:00Z on 2 November; the real boundary is 05:00Z."""
    set_clock(utc(2026, 11, 1, 15, 0))
    add_task(db, 'in the 25th hour', due_at=utc(2026, 11, 2, 4, 30))
    add_task(db, 'last instant', due_at=utc(2026, 11, 2, 5) - US)
    add_task(db, 'next day', due_at=utc(2026, 11, 2, 5))
    add_reminder(db, 'PENDING', utc(2026, 11, 2, 4, 30), title='reminder in the 25th hour')
    body = today(client, 'America/New_York')
    assert [t['title'] for t in body['tasks']['due_today']['items']] == ['in the 25th hour', 'last instant']
    assert body['tasks']['other_open']['later'] == 1  # only 'next day' is later than this 25-hour day
    assert [r['task_title'] for r in body['reminders']['scheduled']['items']] == ['reminder in the 25th hour']


def test_spring_forward_day_ends_after_23_hours(client, db):
    """start + 24h would keep 04:00Z-05:00Z on 9 March inside this day; it belongs to the next one."""
    set_clock(utc(2026, 3, 8, 15, 0))
    add_task(db, 'last instant', due_at=utc(2026, 3, 9, 4) - US)
    add_task(db, 'hour after the 23-hour day', due_at=utc(2026, 3, 9, 4, 30))
    add_task(db, 'first instant', due_at=utc(2026, 3, 8, 5))
    add_task(db, 'before the day', due_at=utc(2026, 3, 8, 5) - US)
    body = today(client, 'America/New_York')
    assert [t['title'] for t in body['tasks']['due_today']['items']] == ['first instant', 'last instant']
    assert [t['title'] for t in body['tasks']['overdue']['items']] == ['before the day']
    assert body['tasks']['other_open']['later'] == 1


def test_day_without_midnight_through_the_api(client, db):
    start = utc(2026, 4, 23, 22)  # Africa/Cairo 2026-04-24 begins at 01:00 local (+03:00)
    set_clock(start + timedelta(hours=5))
    add_task(db, 'last instant of the 23rd', due_at=start - US)
    add_task(db, 'first instant of the 24th', due_at=start)
    body = today(client, 'Africa/Cairo')
    assert (body['local_date'], body['day_start'], body['day_end']) == ('2026-04-24', iso(start), '2026-04-24T21:00:00Z')
    assert [t['title'] for t in body['tasks']['due_today']['items']] == ['first instant of the 24th']
    assert [t['title'] for t in body['tasks']['overdue']['items']] == ['last instant of the 23rd']


def test_unprovable_boundary_is_a_500_not_a_guess(client, monkeypatch):
    monkeypatch.setattr(today_service, 'resolve_timezone', lambda _name: _GapAcrossMidnight())
    set_clock(utc(2026, 6, 1, 6, 0))
    response = get_today(client, 'Asia/Shanghai')
    assert response.status_code == 500
    assert response.json() == {'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal server error'}}
    assert response.headers['cache-control'] == 'no-store'
