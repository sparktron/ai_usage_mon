"""Rich rendering for the TUI. All functions are pure: state in, renderable
out, so they can be unit-tested without a live terminal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config
from .models import Bucket, Provider, UsageSummary
from .oauth_usage import LimitWindow

VIEWS = ("dashboard", "weekly", "hourly", "raw")

_PROVIDER_LABEL = {
    Provider.CLAUDE: "Claude",
    Provider.CODEX: "Codex",
    Provider.OTHER: "Other",
}


@dataclass
class UsageState:
    """Everything the UI needs to render a frame."""

    weekly: dict[Provider, UsageSummary] = field(default_factory=dict)
    last_hour: dict[Provider, UsageSummary] = field(default_factory=dict)
    daily_buckets: list[Bucket] = field(default_factory=list)
    hourly_buckets: list[Bucket] = field(default_factory=list)
    recent: list = field(default_factory=list)
    last_updated: datetime | None = None
    last_error: str | None = None
    source: str = "—"
    next_refresh_in: int = 0
    # Official Anthropic plan usage windows (Path A-lite).
    windows: list[LimitWindow] = field(default_factory=list)
    plan: str | None = None
    windows_error: str | None = None


def color_for_pct(pct: float) -> str:
    """Green <50%, yellow 50-80%, red >80%."""
    if pct < 50:
        return "green"
    if pct <= 80:
        return "yellow"
    return "red"


def fmt_reset(window: LimitWindow, now: datetime | None = None) -> str:
    """Format a window's reset as a countdown (<24h) or a local day/time."""
    secs = window.resets_in_seconds(now)
    if secs is None:
        return "reset time unknown"
    if secs < 24 * 3600:
        h, m = divmod(secs // 60, 60)
        return f"Resets in {h}h {m:02d}m" if h else f"Resets in {m}m"
    # Show the local weekday + time, like the official panel ("Wed 5:59 PM").
    local = window.resets_at.astimezone()
    return f"Resets {local:%a} {local:%-I:%M %p}"


def _bar(pct: float, width: int = 24) -> Text:
    """A colored progress bar like the official usage panel."""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    color = color_for_pct(pct)
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="grey37")
    return bar


def render_windows(state: UsageState) -> RenderableType:
    """The headline panel: official 5-hour + weekly plan usage windows."""
    plan = f" · {state.plan.title()}" if state.plan else ""
    if state.windows_error:
        body: RenderableType = Text(state.windows_error, style="yellow")
    elif not state.windows:
        body = Text("Waiting for usage data…", style="dim")
    else:
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="left", no_wrap=True)   # label
        table.add_column(justify="left")                  # bar
        table.add_column(justify="right", no_wrap=True)   # pct
        table.add_column(justify="right", no_wrap=True)   # reset
        for w in state.windows:
            color = color_for_pct(w.utilization)
            table.add_row(
                Text(w.label, style="bold"),
                _bar(w.utilization),
                Text(f"{w.utilization:.0f}% used", style=color),
                Text(fmt_reset(w), style="cyan"),
            )
        body = table
    return Panel(body, title=f"Plan usage limits{plan}", border_style="bright_blue")


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _pct_text(used: float, cap: float, body: str) -> Text:
    pct = (used / cap * 100) if cap else 0.0
    color = color_for_pct(pct)
    return Text(f"{body} ({pct:.0f}%)", style=color)


def _quota_for(provider: Provider, config: Config) -> int:
    if provider is Provider.CODEX:
        return config.weekly_token_cap_codex
    return config.weekly_token_cap_claude


def _summary_line(
    provider: Provider, summary: UsageSummary, token_cap: float
) -> Text:
    label = _PROVIDER_LABEL.get(provider, provider.value.title())
    used = _fmt_tokens(summary.total_tokens)
    cap = _fmt_tokens(int(token_cap))
    return _pct_text(summary.total_tokens, token_cap, f"{label:<8} {used} / {cap}")


def _usage_panel(
    title: str,
    summaries: dict[Provider, UsageSummary],
    config: Config,
    cap_scale: float,
    cost_cap: float,
) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column()
    total_cost = 0.0
    for provider in (Provider.CLAUDE, Provider.CODEX):
        summary = summaries.get(provider, UsageSummary(provider=provider))
        total_cost += summary.cost
        token_cap = _quota_for(provider, config) * cap_scale
        table.add_row(_summary_line(provider, summary, token_cap))
    table.add_row(Text(""))
    table.add_row(
        _pct_text(total_cost, cost_cap, f"Cost     ${total_cost:.2f} / ${cost_cap:.2f}")
    )
    return Panel(table, title=title, border_style="cyan")


def render_dashboard(state: UsageState, config: Config) -> RenderableType:
    week = _usage_panel(
        "THIS WEEK", state.weekly, config, 1.0, config.weekly_cost_cap
    )
    hour = _usage_panel(
        "LAST HOUR",
        state.last_hour,
        config,
        1.0 / 168.0,
        config.effective_hourly_cost_cap,
    )
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(week, hour)
    # Lead with the official plan-limit windows; the token panels are
    # supplementary context below them.
    return Group(
        render_windows(state),
        Text("Token throughput (from ccusage):", style="dim"),
        grid,
    )


def _bucket_table(title: str, buckets: list[Bucket], cost_cap_each: float) -> Table:
    table = Table(title=title, expand=True, border_style="cyan")
    table.add_column("Period", style="bold")
    table.add_column("Claude", justify="right")
    table.add_column("Codex", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Cumulative", justify="right")

    cumulative = 0.0
    for bucket in buckets:
        claude = bucket.summaries.get(Provider.CLAUDE, UsageSummary(provider=Provider.CLAUDE))
        codex = bucket.summaries.get(Provider.CODEX, UsageSummary(provider=Provider.CODEX))
        cumulative += bucket.total_cost
        cost_color = color_for_pct(
            (bucket.total_cost / cost_cap_each * 100) if cost_cap_each else 0
        )
        table.add_row(
            bucket.label,
            _fmt_tokens(claude.total_tokens),
            _fmt_tokens(codex.total_tokens),
            _fmt_tokens(bucket.total_tokens),
            Text(f"${bucket.total_cost:.2f}", style=cost_color),
            f"${cumulative:.2f}",
        )
    return table


def render_weekly(state: UsageState, config: Config) -> RenderableType:
    daily_cap = config.weekly_cost_cap / 7.0
    return _bucket_table("WEEKLY BREAKDOWN (last 7 days, UTC)", state.daily_buckets, daily_cap)


def render_hourly(state: UsageState, config: Config) -> RenderableType:
    return _bucket_table(
        "HOURLY BREAKDOWN (last 24 hours, UTC)",
        state.hourly_buckets,
        config.effective_hourly_cost_cap,
    )


def render_raw(state: UsageState, config: Config) -> RenderableType:
    table = Table(title="RAW DATA (most recent records)", expand=True, border_style="cyan")
    table.add_column("Timestamp (UTC)")
    table.add_column("Model")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")
    for record in state.recent[:50]:
        table.add_row(
            record.timestamp.strftime("%Y-%m-%d %H:%M"),
            record.model,
            _fmt_tokens(record.input_tokens),
            _fmt_tokens(record.output_tokens),
            f"${record.cost:.4f}",
        )
    if not state.recent:
        table.add_row("—", "no data yet", "0", "0", "$0.00")
    return table


def render_header(state: UsageState) -> RenderableType:
    updated = (
        state.last_updated.strftime("%Y-%m-%d %H:%M:%S UTC")
        if state.last_updated
        else "never"
    )
    return Panel(
        Text(f"Last updated: {updated}   |   Source: {state.source}", style="bold"),
        title="USAGE MONITOR",
        border_style="bright_blue",
    )


def render_footer(state: UsageState, view: str) -> RenderableType:
    nav = Text()
    for name in VIEWS:
        key = name[0].upper()
        style = "reverse bold" if name == view else "dim"
        nav.append(f" [{key}]{name[1:].title()} ", style=style)
    nav.append("  [R]efresh  [Q]uit", style="dim")

    if state.last_error:
        status = Text(f"⚠ {state.last_error} (showing last-known data)", style="red")
    else:
        status = Text(
            f"Last refresh OK. Next refresh in {state.next_refresh_in}s.", style="green"
        )
    return Panel(Group(nav, status), border_style="bright_blue")


def render(state: UsageState, view: str, config: Config) -> RenderableType:
    """Compose the full frame for the given view."""
    body_fns = {
        "dashboard": render_dashboard,
        "weekly": render_weekly,
        "hourly": render_hourly,
        "raw": render_raw,
    }
    body = body_fns.get(view, render_dashboard)(state, config)
    return Group(render_header(state), body, render_footer(state, view))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
