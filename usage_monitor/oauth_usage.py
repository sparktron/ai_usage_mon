"""Path A-lite: read the Claude Code OAuth token (read-only) and fetch the
official plan usage limits from Anthropic's ``/api/oauth/usage`` endpoint.

This NEVER writes to the credential store and never refreshes the token. It
uses whatever token Claude Code has already persisted; when that token is
expired it reports ``TokenExpired`` so the UI can prompt the user to refresh
by using Claude Code. This keeps us out of token-rotation / re-login hazards.

The exact response schema is verified against a live call before this module's
parser is finalized; until then ``parse_usage`` is intentionally defensive
about nesting and the 0-1 vs 0-100 scale of ``utilization``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


class OAuthUsageError(RuntimeError):
    """Base error for the OAuth usage source."""


class CredentialsUnavailable(OAuthUsageError):
    """The credentials file is missing or has no Claude OAuth token."""


class TokenExpired(OAuthUsageError):
    """The on-disk access token has expired; Claude Code must refresh it."""


@dataclass
class LimitWindow:
    """One usage window (e.g. the 5-hour session or the 7-day week)."""

    key: str
    label: str
    utilization: float  # percent, 0-100
    resets_at: datetime | None

    def resets_in_seconds(self, now: datetime | None = None) -> int | None:
        if self.resets_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, int((self.resets_at - now).total_seconds()))


def read_token(path: Path = DEFAULT_CREDENTIALS) -> tuple[str, float]:
    """Return (access_token, expires_at_epoch_seconds), read-only.

    Raises CredentialsUnavailable / TokenExpired as appropriate.
    """
    if not path.exists():
        raise CredentialsUnavailable(f"no credentials file at {path}")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise CredentialsUnavailable(f"cannot read credentials: {exc}") from exc

    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise CredentialsUnavailable("no claudeAiOauth.accessToken in credentials")

    expires_at = float(oauth.get("expiresAt", 0) or 0) / 1000.0
    if expires_at and expires_at < time.time():
        raise TokenExpired("access token expired; use Claude Code to refresh it")
    return token, expires_at


def read_plan(path: Path = DEFAULT_CREDENTIALS) -> str | None:
    """Best-effort read of the subscription type (e.g. 'pro'), read-only."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return (data.get("claudeAiOauth") or {}).get("subscriptionType")


def _coerce_pct(value: float | int | None) -> float:
    """Normalize a utilization value to a 0-100 percentage."""
    if value is None:
        return 0.0
    v = float(value)
    # Some payloads use a 0-1 fraction, others 0-100.
    return v * 100.0 if v <= 1.0 else v


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # epoch seconds or millis
        secs = value / 1000.0 if value > 1e12 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Human labels for the windows we care about; others are passed through.
_LABELS = {
    "five_hour": "Current session (5h)",
    "seven_day": "Weekly · all models",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
}


def parse_usage(payload: dict) -> list[LimitWindow]:
    """Parse the /api/oauth/usage payload into ordered LimitWindows.

    Defensive about nesting: each window may be a dict carrying ``utilization``
    and ``resets_at`` (the expected shape) — anything else is skipped.
    """
    windows: list[LimitWindow] = []
    preferred = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
    seen = set()

    def add(key: str, obj) -> None:
        if key in seen or not isinstance(obj, dict):
            return
        if "utilization" not in obj and "resets_at" not in obj and "resetsAt" not in obj:
            return
        seen.add(key)
        windows.append(
            LimitWindow(
                key=key,
                label=_LABELS.get(key, key.replace("_", " ")),
                utilization=_coerce_pct(obj.get("utilization")),
                resets_at=_parse_dt(obj.get("resets_at") or obj.get("resetsAt")),
            )
        )

    for key in preferred:
        if key in payload:
            add(key, payload[key])
    for key, obj in payload.items():
        add(key, obj)
    return windows


def fetch_usage(
    credentials_path: Path = DEFAULT_CREDENTIALS,
    *,
    client: httpx.Client | None = None,
) -> list[LimitWindow]:
    """Fetch and parse the official plan usage limits (read-only token)."""
    token, _ = read_token(credentials_path)
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": "usage-monitor/0.1",
    }
    owns = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        resp = client.get(USAGE_URL, headers=headers)
        if resp.status_code == 401:
            raise TokenExpired("usage endpoint rejected the token (401)")
        resp.raise_for_status()
        return parse_usage(resp.json())
    except httpx.HTTPError as exc:
        raise OAuthUsageError(f"usage request failed: {exc}") from exc
    finally:
        if owns:
            client.close()
