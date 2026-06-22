from datetime import datetime, timezone

import pytest

from usage_monitor.models import (
    Provider,
    UsageRecord,
    compute_cost,
    price_for_model,
    provider_for_model,
)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-4-8", Provider.CLAUDE),
        ("claude-3-5-haiku", Provider.CLAUDE),
        ("gpt-4o", Provider.CODEX),
        ("codex-mini", Provider.CODEX),
        ("o3-pro", Provider.CODEX),
        ("some-llama", Provider.OTHER),
    ],
)
def test_provider_for_model(model, expected):
    assert provider_for_model(model) == expected


def test_price_longest_prefix_wins():
    # "claude-3-haiku" (0.25/1.25) is more specific than "claude-haiku".
    assert price_for_model("claude-3-haiku-20240307") == (0.25, 1.25)


def test_price_unknown_model_uses_default():
    assert price_for_model("mystery-model") == (3.0, 15.0)


def test_compute_cost():
    # 1M input + 1M output at sonnet rates (3 / 15).
    cost = compute_cost("claude-sonnet-4", 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.0)


def test_record_normalizes_to_utc():
    naive = UsageRecord(timestamp=datetime(2026, 1, 1, 12), model="claude-opus", input_tokens=1, output_tokens=1)
    assert naive.timestamp.tzinfo == timezone.utc


def test_with_computed_cost_fills_missing():
    rec = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        model="claude-sonnet-4",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert rec.cost == 0.0
    filled = rec.with_computed_cost()
    assert filled.cost == pytest.approx(3.0)


def test_with_computed_cost_preserves_existing():
    rec = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        model="claude-sonnet-4",
        input_tokens=10,
        output_tokens=10,
        cost=99.0,
    )
    assert rec.with_computed_cost().cost == 99.0


def test_total_tokens_and_provider_properties():
    rec = UsageRecord(
        timestamp=datetime.now(timezone.utc),
        model="claude-opus",
        input_tokens=5,
        output_tokens=7,
    )
    assert rec.total_tokens == 12
    assert rec.provider == Provider.CLAUDE


def test_negative_tokens_rejected():
    with pytest.raises(ValueError):
        UsageRecord(
            timestamp=datetime.now(timezone.utc),
            model="x",
            input_tokens=-1,
            output_tokens=0,
        )
