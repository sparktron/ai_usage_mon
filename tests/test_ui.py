from datetime import datetime, timezone

from rich.console import Console

from usage_monitor.config import Config
from usage_monitor.models import Bucket, Provider, UsageRecord, UsageSummary
from usage_monitor.ui import (
    UsageState,
    color_for_pct,
    render,
    render_dashboard,
    render_footer,
    _fmt_tokens,
    _spaced,
)


def _render_to_text(renderable) -> str:
    console = Console(width=100, record=True, file=open("/dev/null", "w"))
    console.print(renderable)
    return console.export_text()


def test_color_thresholds():
    assert color_for_pct(10) == "green"
    assert color_for_pct(49.9) == "green"
    assert color_for_pct(50) == "orange3"
    assert color_for_pct(80) == "orange3"
    assert color_for_pct(80.1) == "red"
    assert color_for_pct(150) == "red"


def test_fmt_tokens():
    assert _fmt_tokens(500) == "500"
    assert _fmt_tokens(1500) == "1.5K"
    assert _fmt_tokens(2_500_000) == "2.5M"


def _state():
    now = datetime(2026, 6, 21, 14, 0, tzinfo=timezone.utc)
    weekly = {
        Provider.CLAUDE: UsageSummary(Provider.CLAUDE, 100_000, 25_000, 2.0),
        Provider.CODEX: UsageSummary(Provider.CODEX, 30_000, 12_000, 0.5),
    }
    bucket = Bucket(label="2026-06-21", start=now, summaries=weekly)
    return UsageState(
        weekly=weekly,
        last_hour=weekly,
        daily_buckets=[bucket],
        hourly_buckets=[bucket],
        recent=[UsageRecord(timestamp=now, model="claude-opus", input_tokens=10, output_tokens=5, cost=0.01)],
        last_updated=now,
        source="ccusage",
        next_refresh_in=42,
    )


def test_render_dashboard_contains_providers():
    text = _render_to_text(render_dashboard(_state(), Config()))
    assert "Claude" in text
    assert "Codex" in text
    assert _spaced("THIS WEEK") in text


def test_render_all_views_do_not_crash():
    state = _state()
    cfg = Config()
    for view in ("dashboard", "weekly", "hourly", "raw"):
        text = _render_to_text(render(state, view, cfg))
        assert _spaced("USAGE MONITOR") in text


def test_footer_shows_error_when_present():
    state = _state()
    state.last_error = "boom"
    text = _render_to_text(render_footer(state, "dashboard"))
    assert "boom" in text


def test_footer_shows_countdown_without_error():
    text = _render_to_text(render_footer(_state(), "dashboard"))
    assert "42s" in text


def test_raw_view_with_no_data():
    text = _render_to_text(render(UsageState(), "raw", Config()))
    assert "no data yet" in text
