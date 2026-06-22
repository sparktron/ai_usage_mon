import json

import pytest

from usage_monitor import ccusage_fallback as cc
from usage_monitor.models import Provider


def test_parse_real_ccusage_schema():
    """Mirrors actual `ccusage daily --json` output (v20): the date field is
    `period`, and input tokens are reported exclusive of cache tokens."""
    raw = json.dumps(
        {
            "daily": [
                {
                    "period": "2026-06-22",
                    "inputTokens": 1544,
                    "cacheReadTokens": 7194923,
                    "cacheCreationTokens": 389564,
                    "outputTokens": 157507,
                    "totalCost": 5.78,
                    "modelBreakdowns": [
                        {
                            "modelName": "claude-sonnet-4-6",
                            "inputTokens": 109,
                            "cacheReadTokens": 5499109,
                            "cacheCreationTokens": 248791,
                            "outputTokens": 150702,
                            "cost": 5.40,
                        },
                        {
                            "modelName": "gpt-5.3-codex",
                            "inputTokens": 68063,
                            "cacheReadTokens": 462592,
                            "cacheCreationTokens": 0,
                            "outputTokens": 26934,
                            "cost": 0.577,
                        },
                    ],
                }
            ]
        }
    )
    records = cc.parse_ccusage_json(raw)
    assert len(records) == 2
    sonnet = records[0]
    assert sonnet.timestamp.date().isoformat() == "2026-06-22"
    # input must include cache: 109 + 5,499,109 + 248,791
    assert sonnet.input_tokens == 5_748_009
    assert sonnet.provider == Provider.CLAUDE
    assert records[1].provider == Provider.CODEX
    assert records[1].input_tokens == 530_655  # 68,063 + 462,592


def test_input_total_helper():
    assert cc._input_total(
        {"inputTokens": 10, "cacheReadTokens": 5, "cacheCreationTokens": 2}
    ) == 17
    assert cc._input_total({"inputTokens": 3}) == 3


def test_parse_with_model_breakdowns():
    raw = json.dumps(
        {
            "daily": [
                {
                    "date": "2026-06-20",
                    "modelBreakdowns": [
                        {
                            "modelName": "claude-opus-4",
                            "inputTokens": 1000,
                            "outputTokens": 500,
                            "cost": 0.05,
                        },
                        {
                            "modelName": "claude-3-5-haiku",
                            "inputTokens": 200,
                            "outputTokens": 100,
                        },
                    ],
                }
            ]
        }
    )
    records = cc.parse_ccusage_json(raw)
    assert len(records) == 2
    assert records[0].model == "claude-opus-4"
    assert records[0].cost == 0.05
    assert records[0].provider == Provider.CLAUDE


def test_parse_day_aggregate_fallback():
    raw = json.dumps(
        {
            "daily": [
                {
                    "date": "2026-06-19",
                    "inputTokens": 300,
                    "outputTokens": 150,
                    "totalCost": 0.02,
                }
            ]
        }
    )
    records = cc.parse_ccusage_json(raw)
    assert len(records) == 1
    assert records[0].input_tokens == 300
    assert records[0].cost == 0.02


def test_parse_list_top_level():
    raw = json.dumps([{"date": "2026-06-18", "modelBreakdowns": [
        {"modelName": "gpt-4o", "inputTokens": 10, "outputTokens": 5, "cost": 0.001}
    ]}])
    records = cc.parse_ccusage_json(raw)
    assert records[0].provider == Provider.CODEX


def test_parse_invalid_json_raises():
    with pytest.raises(cc.CcusageError):
        cc.parse_ccusage_json("not json")


def test_parse_empty():
    assert cc.parse_ccusage_json(json.dumps({"daily": []})) == []


def test_run_ccusage_missing_binary():
    with pytest.raises(cc.CcusageError):
        cc.run_ccusage("definitely-not-a-real-binary-xyz")


def test_is_available_false():
    assert cc.is_available("definitely-not-a-real-binary-xyz") is False
