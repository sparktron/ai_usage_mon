import httpx
import pytest
import respx

from usage_monitor.api import (
    AnthropicUsageClient,
    ApiError,
    USAGE_PATH,
    parse_usage_report,
)
from usage_monitor.models import Provider


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr("usage_monitor.api.asyncio.sleep", _instant)


def test_parse_usage_report_sums_cache_tokens():
    payload = {
        "data": [
            {
                "starting_at": "2026-06-20T10:00:00Z",
                "results": [
                    {
                        "model": "claude-opus-4",
                        "input_tokens": 100,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                        "output_tokens": 50,
                    }
                ],
            }
        ]
    }
    records = parse_usage_report(payload)
    assert len(records) == 1
    assert records[0].input_tokens == 115
    assert records[0].output_tokens == 50
    assert records[0].provider == Provider.CLAUDE


def test_parse_empty_payload():
    assert parse_usage_report({}) == []


@respx.mock
async def test_fetch_usage_success():
    route = respx.get(f"https://api.anthropic.com{USAGE_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": "2026-06-20T10:00:00Z",
                        "results": [
                            {"model": "claude-sonnet-4", "input_tokens": 10, "output_tokens": 5}
                        ],
                    }
                ]
            },
        )
    )
    async with AnthropicUsageClient("sk-test") as client:
        records = await client.fetch_usage(days=1)
    assert route.called
    assert records[0].model == "claude-sonnet-4"


@respx.mock
async def test_fetch_usage_4xx_raises_immediately():
    route = respx.get(f"https://api.anthropic.com{USAGE_PATH}").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    async with AnthropicUsageClient("bad-key") as client:
        with pytest.raises(ApiError):
            await client.fetch_usage()
    assert route.call_count == 1  # no retries on 4xx


@respx.mock
async def test_fetch_usage_retries_then_fails():
    route = respx.get(f"https://api.anthropic.com{USAGE_PATH}").mock(
        return_value=httpx.Response(500)
    )
    async with AnthropicUsageClient("sk-test", max_retries=3) as client:
        with pytest.raises(ApiError):
            await client.fetch_usage()
    assert route.call_count == 3


@respx.mock
async def test_fetch_usage_recovers_after_transient_error():
    route = respx.get(f"https://api.anthropic.com{USAGE_PATH}")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(
            200,
            json={"data": [{"starting_at": "2026-06-20T10:00:00Z",
                            "results": [{"model": "claude-opus", "input_tokens": 1, "output_tokens": 1}]}]},
        ),
    ]
    async with AnthropicUsageClient("sk-test") as client:
        records = await client.fetch_usage()
    assert len(records) == 1
    assert route.call_count == 2
