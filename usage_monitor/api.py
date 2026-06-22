"""Anthropic usage API client.

Targets the organization usage report endpoint. The exact schema varies and
may be unavailable on some keys, so the client is best-effort and the app
falls back to ccusage when this raises. Includes exponential-backoff retries.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from .models import UsageRecord

log = logging.getLogger("usage_monitor.api")

ANTHROPIC_VERSION = "2023-06-01"
USAGE_PATH = "/v1/organizations/usage_report/messages"


class ApiError(RuntimeError):
    """Raised when the usage API cannot be reached or returns an error."""


def _parse_bucket(bucket: dict) -> list[UsageRecord]:
    """Parse one time-bucket of the usage report into records."""
    start_raw = bucket.get("starting_at") or bucket.get("start_time")
    if start_raw:
        ts = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
    else:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    records: list[UsageRecord] = []
    for result in bucket.get("results", []):
        model = result.get("model") or "claude-unknown"
        input_tokens = int(
            result.get("input_tokens", result.get("uncached_input_tokens", 0)) or 0
        )
        input_tokens += int(result.get("cache_read_input_tokens", 0) or 0)
        input_tokens += int(result.get("cache_creation_input_tokens", 0) or 0)
        output_tokens = int(result.get("output_tokens", 0) or 0)
        records.append(
            UsageRecord(
                timestamp=ts,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
    return records


def parse_usage_report(payload: dict) -> list[UsageRecord]:
    """Parse the full usage report payload into records."""
    records: list[UsageRecord] = []
    for bucket in payload.get("data", []):
        if isinstance(bucket, dict):
            records.extend(_parse_bucket(bucket))
    return records


class AnthropicUsageClient:
    """Async client for the Anthropic usage report endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        *,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "AnthropicUsageClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def fetch_usage(self, *, days: int = 7) -> list[UsageRecord]:
        """Fetch hourly usage for the last ``days`` with retry/backoff."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

        starting_at = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).replace(minute=0, second=0, microsecond=0)
        params = {
            "starting_at": starting_at.isoformat(),
            "bucket_width": "1h",
            "group_by[]": "model",
        }
        url = f"{self.base_url}{USAGE_PATH}"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.get(url, headers=self._headers(), params=params)
                resp.raise_for_status()
                return parse_usage_report(resp.json())
            except httpx.HTTPStatusError as exc:
                # Client errors (auth, bad request) won't fix on retry.
                if 400 <= exc.response.status_code < 500:
                    raise ApiError(
                        f"usage API returned {exc.response.status_code}"
                    ) from exc
                last_exc = exc
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc

            backoff = 2**attempt
            log.warning("usage API attempt %d failed: %s; retry in %ss",
                        attempt + 1, last_exc, backoff)
            await asyncio.sleep(backoff)

        raise ApiError(f"usage API failed after {self.max_retries} retries: {last_exc}")
