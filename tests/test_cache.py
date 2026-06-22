from datetime import datetime, timedelta, timezone

import pytest

from usage_monitor.cache import Cache, merge_summaries
from usage_monitor.models import Provider, UsageRecord, UsageSummary


@pytest.fixture
def cache():
    c = Cache(":memory:")
    yield c
    c.close()


def _rec(ts, model="claude-sonnet-4", i=100, o=50, cost=0.0):
    return UsageRecord(timestamp=ts, model=model, input_tokens=i, output_tokens=o, cost=cost)


def test_empty(cache):
    assert cache.is_empty()
    cache.insert_records([_rec(datetime.now(timezone.utc))])
    assert not cache.is_empty()


def test_insert_computes_cost(cache):
    ts = datetime.now(timezone.utc)
    cache.insert_records([_rec(ts, i=1_000_000, o=0)])
    rows = cache.recent_records()
    assert rows[0].cost == pytest.approx(3.0)


def test_upsert_dedups_on_timestamp_model(cache):
    ts = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
    cache.insert_records([_rec(ts, i=100, o=10)])
    cache.insert_records([_rec(ts, i=200, o=20)])  # same key -> replace
    records = cache.recent_records()
    assert len(records) == 1
    assert records[0].input_tokens == 200


def test_summaries_between_groups_by_provider(cache):
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    cache.insert_records(
        [
            _rec(now, model="claude-sonnet-4", i=100, o=50),
            _rec(now, model="gpt-4o", i=10, o=5),
        ]
    )
    summaries = cache.summaries_between(now - timedelta(hours=1), now + timedelta(hours=1))
    assert summaries[Provider.CLAUDE].total_tokens == 150
    assert summaries[Provider.CODEX].total_tokens == 15


def test_summaries_respects_window(cache):
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    cache.insert_records([_rec(now - timedelta(days=10))])
    summaries = cache.summaries_between(now - timedelta(days=1), now)
    assert summaries == {}


def test_daily_buckets(cache):
    now = datetime(2026, 6, 7, 12, tzinfo=timezone.utc)
    cache.insert_records([_rec(now, i=100, o=100)])
    cache.insert_records([_rec(now - timedelta(days=2), model="gpt-4o", i=10, o=10)])
    buckets = cache.daily_buckets(7, now=now)
    assert len(buckets) == 7
    assert buckets[-1].label == "2026-06-07"
    assert buckets[-1].total_tokens == 200
    assert buckets[-3].total_tokens == 20


def test_hourly_buckets(cache):
    now = datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)
    cache.insert_records([_rec(now.replace(minute=5), i=100, o=0)])
    buckets = cache.hourly_buckets(24, now=now)
    assert len(buckets) == 24
    assert buckets[-1].label == "12:00"
    assert buckets[-1].total_tokens == 100


def test_prune(cache):
    now = datetime.now(timezone.utc)
    cache.insert_records([_rec(now - timedelta(days=40))])
    cache.insert_records([_rec(now, model="gpt-4o")])
    deleted = cache.prune_older_than(30, now=now)
    assert deleted == 1
    assert len(cache.recent_records()) == 1


def test_merge_summaries():
    a = {Provider.CLAUDE: UsageSummary(Provider.CLAUDE, 100, 50, 1.0)}
    b = {Provider.CLAUDE: UsageSummary(Provider.CLAUDE, 10, 5, 0.5)}
    merged = merge_summaries([a, b])
    assert merged[Provider.CLAUDE].input_tokens == 110
    assert merged[Provider.CLAUDE].cost == pytest.approx(1.5)


def test_context_manager(tmp_path):
    db = tmp_path / "u.db"
    with Cache(db) as c:
        c.insert_records([_rec(datetime.now(timezone.utc))])
    assert db.exists()
