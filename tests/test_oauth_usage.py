import json
import time
from datetime import datetime, timezone, timedelta

import pytest

from usage_monitor import oauth_usage as ou
from usage_monitor.oauth_usage import (
    CredentialsUnavailable,
    LimitWindow,
    TokenExpired,
    parse_usage,
    read_plan,
    read_token,
)
from usage_monitor.ui import UsageState, fmt_reset, render_windows


def _write_creds(path, *, token="tok", expires_in=3600, plan="pro"):
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "expiresAt": int((time.time() + expires_in) * 1000),
                    "subscriptionType": plan,
                }
            }
        )
    )


def test_parse_usage_pct_scales_and_order():
    now = datetime.now(timezone.utc)
    payload = {
        "seven_day": {"utilization": 68, "resets_at": now.isoformat()},
        "five_hour": {"utilization": 0.34, "resets_at": now.isoformat()},  # 0-1 scale
    }
    windows = parse_usage(payload)
    # five_hour is ordered before seven_day regardless of payload order
    assert [w.key for w in windows] == ["five_hour", "seven_day"]
    assert windows[0].utilization == pytest.approx(34.0)  # 0.34 -> 34%
    assert windows[1].utilization == pytest.approx(68.0)
    assert windows[0].label == "Current session (5h)"


def test_parse_usage_skips_non_window_keys():
    windows = parse_usage({"account_uuid": "x", "five_hour": {"utilization": 10}})
    assert len(windows) == 1
    assert windows[0].key == "five_hour"


def test_parse_usage_resets_at_epoch_millis():
    ms = int(datetime(2026, 6, 24, tzinfo=timezone.utc).timestamp() * 1000)
    windows = parse_usage({"five_hour": {"utilization": 5, "resets_at": ms}})
    assert windows[0].resets_at == datetime(2026, 6, 24, tzinfo=timezone.utc)


def test_read_token_valid(tmp_path):
    p = tmp_path / "creds.json"
    _write_creds(p, token="abc", expires_in=3600)
    token, exp = read_token(p)
    assert token == "abc"
    assert exp > time.time()


def test_read_token_expired(tmp_path):
    p = tmp_path / "creds.json"
    _write_creds(p, expires_in=-100)
    with pytest.raises(TokenExpired):
        read_token(p)


def test_read_token_missing_file(tmp_path):
    with pytest.raises(CredentialsUnavailable):
        read_token(tmp_path / "nope.json")


def test_read_token_no_oauth(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"somethingElse": {}}))
    with pytest.raises(CredentialsUnavailable):
        read_token(p)


def test_read_plan(tmp_path):
    p = tmp_path / "creds.json"
    _write_creds(p, plan="pro")
    assert read_plan(p) == "pro"
    assert read_plan(tmp_path / "nope.json") is None


def test_fmt_reset_countdown():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    w = LimitWindow("five_hour", "x", 10, now + timedelta(hours=4, minutes=39))
    assert fmt_reset(w, now) == "Resets in 4h 39m"


def test_fmt_reset_minutes_only():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    w = LimitWindow("five_hour", "x", 10, now + timedelta(minutes=12))
    assert fmt_reset(w, now) == "Resets in 12m"


def test_fmt_reset_far_future_uses_weekday():
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)  # Monday
    w = LimitWindow("seven_day", "x", 10, now + timedelta(days=2, hours=6))
    out = fmt_reset(w, now)
    assert out.startswith("Resets ")
    assert "in " not in out  # not a countdown


def test_fmt_reset_unknown():
    w = LimitWindow("five_hour", "x", 10, None)
    assert fmt_reset(w) == "reset time unknown"


def test_render_windows_shows_pct_and_plan():
    from rich.console import Console

    now = datetime.now(timezone.utc)
    windows = parse_usage(
        {
            "five_hour": {"utilization": 34, "resets_at": (now + timedelta(hours=2)).isoformat()},
            "seven_day": {"utilization": 68, "resets_at": (now + timedelta(days=2)).isoformat()},
        }
    )
    state = UsageState(windows=windows, plan="pro")
    con = Console(width=100, record=True, file=open("/dev/null", "w"))
    con.print(render_windows(state))
    text = con.export_text()
    assert "Plan usage limits" in text
    assert "Pro" in text
    assert "34% used" in text
    assert "68% used" in text


def test_render_windows_expired_message():
    from rich.console import Console

    state = UsageState(windows=[], windows_error="token expired")
    con = Console(width=100, record=True, file=open("/dev/null", "w"))
    con.print(render_windows(state))
    assert "token expired" in con.export_text()


def test_fetch_usage_token_expired(tmp_path):
    p = tmp_path / "creds.json"
    _write_creds(p, expires_in=-1)
    with pytest.raises(TokenExpired):
        ou.fetch_usage(p)
