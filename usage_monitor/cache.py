"""SQLite cache layer for usage records.

Records are keyed by (timestamp, model) so re-fetching the same window updates
in place rather than double-counting. All times are stored as UTC ISO strings.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Bucket, Provider, UsageRecord, UsageSummary, provider_for_model

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    timestamp     TEXT NOT NULL,
    model         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost          REAL NOT NULL,
    PRIMARY KEY (timestamp, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage (timestamp);
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class Cache:
    """Thin wrapper around a SQLite connection holding usage records."""

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes ---------------------------------------------------------------

    def insert_records(self, records: Iterable[UsageRecord]) -> int:
        """Upsert records. Returns the number of rows written."""
        rows = []
        for r in records:
            r = r.with_computed_cost()
            rows.append(
                (
                    _iso(r.timestamp),
                    r.model,
                    r.provider.value,
                    r.input_tokens,
                    r.output_tokens,
                    r.cost,
                )
            )
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO usage (timestamp, model, provider, input_tokens, "
            "output_tokens, cost) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(timestamp, model) DO UPDATE SET "
            "input_tokens=excluded.input_tokens, "
            "output_tokens=excluded.output_tokens, "
            "cost=excluded.cost, provider=excluded.provider",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def prune_older_than(self, days: int = 30, now: datetime | None = None) -> int:
        """Delete records older than ``days``. Returns rows deleted."""
        now = now or datetime.now(timezone.utc)
        cutoff = _iso(now - timedelta(days=days))
        cur = self.conn.execute("DELETE FROM usage WHERE timestamp < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    # -- reads ----------------------------------------------------------------

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0

    def summaries_between(
        self, start: datetime, end: datetime
    ) -> dict[Provider, UsageSummary]:
        """Per-provider aggregate over the half-open interval [start, end)."""
        cur = self.conn.execute(
            "SELECT provider, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
            "SUM(cost) AS c FROM usage WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY provider",
            (_iso(start), _iso(end)),
        )
        result: dict[Provider, UsageSummary] = {}
        for row in cur.fetchall():
            provider = Provider(row["provider"])
            result[provider] = UsageSummary(
                provider=provider,
                input_tokens=row["i"] or 0,
                output_tokens=row["o"] or 0,
                cost=row["c"] or 0.0,
            )
        return result

    def daily_buckets(self, days: int = 7, now: datetime | None = None) -> list[Bucket]:
        """Return ``days`` calendar-day buckets ending with today (UTC)."""
        now = now or datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        buckets: list[Bucket] = []
        for i in range(days - 1, -1, -1):
            start = today - timedelta(days=i)
            end = start + timedelta(days=1)
            buckets.append(
                Bucket(
                    label=start.strftime("%Y-%m-%d"),
                    start=start,
                    summaries=self.summaries_between(start, end),
                )
            )
        return buckets

    def hourly_buckets(self, hours: int = 24, now: datetime | None = None) -> list[Bucket]:
        """Return ``hours`` hour buckets ending with the current hour (UTC)."""
        now = now or datetime.now(timezone.utc)
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        buckets: list[Bucket] = []
        for i in range(hours - 1, -1, -1):
            start = this_hour - timedelta(hours=i)
            end = start + timedelta(hours=1)
            buckets.append(
                Bucket(
                    label=start.strftime("%H:00"),
                    start=start,
                    summaries=self.summaries_between(start, end),
                )
            )
        return buckets

    def recent_records(self, limit: int = 200) -> list[UsageRecord]:
        cur = self.conn.execute(
            "SELECT timestamp, model, input_tokens, output_tokens, cost "
            "FROM usage ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return [
            UsageRecord(
                timestamp=datetime.fromisoformat(row["timestamp"]),
                model=row["model"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cost=row["cost"],
            )
            for row in cur.fetchall()
        ]


def merge_summaries(
    summaries: Sequence[dict[Provider, UsageSummary]]
) -> dict[Provider, UsageSummary]:
    """Combine several per-provider summary maps into one."""
    merged: dict[Provider, UsageSummary] = {}
    for mapping in summaries:
        for provider, summary in mapping.items():
            target = merged.setdefault(provider, UsageSummary(provider=provider))
            target.input_tokens += summary.input_tokens
            target.output_tokens += summary.output_tokens
            target.cost += summary.cost
    return merged


__all__ = ["Cache", "merge_summaries", "provider_for_model"]
