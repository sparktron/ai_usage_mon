"""Pydantic models and pricing logic for usage-monitor.

A single ``UsageRecord`` is the atomic unit stored in the cache. Aggregations
(``UsageSummary`` / ``Bucket``) are derived on demand for the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Provider(str, Enum):
    """Which vendor a model belongs to."""

    CLAUDE = "claude"
    CODEX = "codex"
    OTHER = "other"


# Price per 1M tokens, (input, output), in USD. Substring matched against the
# model name, longest match wins. Update as vendor pricing changes.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-opus": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-haiku": (0.80, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-sonnet": (3.0, 15.0),
    "claude-3-haiku": (0.25, 1.25),
    "gpt-5": (1.25, 10.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1": (2.0, 8.0),
    "o3": (2.0, 8.0),
    "o1": (15.0, 60.0),
    "codex": (1.5, 6.0),
}

# Used when a model name matches no known prefix.
DEFAULT_PRICE: tuple[float, float] = (3.0, 15.0)


def provider_for_model(model: str) -> Provider:
    """Classify a model name into a :class:`Provider`."""
    name = model.lower()
    if "claude" in name:
        return Provider.CLAUDE
    if any(tok in name for tok in ("codex", "gpt", "o1", "o3", "davinci")):
        return Provider.CODEX
    return Provider.OTHER


def price_for_model(model: str) -> tuple[float, float]:
    """Return (input, output) USD-per-1M-token rates for a model name."""
    name = model.lower()
    best: tuple[int, tuple[float, float]] | None = None
    for prefix, price in PRICING.items():
        if prefix in name and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    return best[1] if best else DEFAULT_PRICE


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute the USD cost of a usage record from current pricing."""
    in_rate, out_rate = price_for_model(model)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


class UsageRecord(BaseModel):
    """A single usage data point for one model at one timestamp."""

    timestamp: datetime
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float = Field(default=0.0, ge=0.0)

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        """Normalize all timestamps to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def provider(self) -> Provider:
        return provider_for_model(self.model)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def with_computed_cost(self) -> "UsageRecord":
        """Return a copy whose cost is filled in from pricing if missing."""
        if self.cost:
            return self
        return self.model_copy(
            update={"cost": compute_cost(self.model, self.input_tokens, self.output_tokens)}
        )


@dataclass
class UsageSummary:
    """Aggregated usage for a single provider over some window."""

    provider: Provider
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, record: UsageRecord) -> None:
        self.input_tokens += record.input_tokens
        self.output_tokens += record.output_tokens
        self.cost += record.cost


@dataclass
class Bucket:
    """A labeled time bucket (a day or an hour) with per-provider summaries."""

    label: str
    start: datetime
    summaries: dict[Provider, UsageSummary] = field(default_factory=dict)

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.summaries.values())

    @property
    def total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.summaries.values())
