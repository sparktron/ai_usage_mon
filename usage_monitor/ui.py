"""Rich rendering for the TUI. All functions are pure: state in, renderable
out, so they can be unit-tested without a live terminal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config
from .models import Bucket, Provider, UsageSummary
from .oauth_usage import LimitWindow

VIEWS = ("dashboard", "weekly", "hourly", "raw")

# A muted palette so the chrome recedes and the numbers stand out.
BORDER = "grey37"          # subtle rounded panel/table outlines
TITLE_STYLE = "bold cyan"  # centered panel titles
LABEL = "grey66"           # row labels (Claude, Last updated:, …)
VALUE = "bold white"       # primary figures
DIM = "grey50"             # separators, caps, reset times, trailing words
TRACK = "grey23"           # the empty portion of a progress bar


def _spaced(text: str) -> str:
    """Letter-space an all-caps title (USAGE MONITOR -> U S A G E   M O N I T O R)."""
    return " ".join(text)

# Explicit view -> hotkey letter. "raw" cannot use "r" because that is bound to
# Refresh, so it gets "a" instead. Keep this as the single source of truth for
# both the footer labels and the key handler.
VIEW_KEYS = {"dashboard": "d", "weekly": "w", "hourly": "h", "raw": "a"}

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
    """Green <50%, amber 50-80%, red >80%."""
    if pct < 50:
        return "green"
    if pct <= 80:
        return "orange3"
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
    # Avoid the glibc-only %-I directive so this renders on every platform.
    local = window.resets_at.astimezone()
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"Resets {local:%a} {hour}:{local:%M} {local:%p}"


def _bar(pct: float, width: int = 28, color: str | None = None) -> Text:
    """A solid two-tone progress bar like the official usage panel: a colored
    fill over a dark track. Pass ``color`` to override the severity-based fill.
    """
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    fill = color or color_for_pct(pct)
    bar = Text()
    bar.append("█" * filled, style=fill)
    bar.append("█" * (width - filled), style=TRACK)
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
        table.add_column(justify="left", no_wrap=True)    # pct
        table.add_column(justify="left", no_wrap=True)    # reset
        for w in state.windows:
            color = color_for_pct(w.utilization)
            pct = Text()
            pct.append(f"{w.utilization:.0f}% ", style=f"bold {color}")
            pct.append("used", style=DIM)
            table.add_row(
                Text(w.label, style=f"bold {LABEL}"),
                _bar(w.utilization),
                pct,
                Text(fmt_reset(w), style=DIM),
            )
        body = table
    return Panel(
        body,
        title=Text(f"Plan usage limits{plan}", style=TITLE_STYLE),
        border_style=BORDER,
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _metric_line(label: str, used_str: str, cap_str: str, used_val: float, cap: float) -> Text:
    """A throughput row: dim label, bright value, dim cap, amber percentage.
    The value goes dim when it's zero so live activity reads at a glance."""
    pct = (used_val / cap * 100) if cap else 0.0
    line = Text()
    line.append(f"{label:<8}", style=LABEL)
    line.append(used_str, style=VALUE if used_val else DIM)
    line.append(f" / {cap_str} ", style=DIM)
    line.append(f"({pct:.0f}%)", style=f"bold {color_for_pct(pct)}")
    return line


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
    return _metric_line(label, used, cap, summary.total_tokens, token_cap)


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
        _metric_line(
            "Cost", f"${total_cost:.2f}", f"${cost_cap:.2f}", total_cost, cost_cap
        )
    )
    return Panel(
        table, title=Text(_spaced(title), style=TITLE_STYLE), border_style=BORDER
    )


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
    caption = Text()
    caption.append("Token throughput ", style=LABEL)
    caption.append("(from ccusage):", style=DIM)
    return Group(
        render_windows(state),
        caption,
        grid,
    )


def _bucket_table(title: str, buckets: list[Bucket], cost_cap_each: float) -> Table:
    table = Table(
        title=Text(title, style=TITLE_STYLE),
        expand=True,
        border_style=BORDER,
        box=box.ROUNDED,
    )
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
    table = Table(
        title=Text("RAW DATA (most recent records)", style=TITLE_STYLE),
        expand=True,
        border_style=BORDER,
        box=box.ROUNDED,
    )
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
    line = Text()
    line.append("Last updated: ", style=LABEL)
    line.append(updated, style=VALUE)
    line.append("   |   ", style=DIM)
    line.append("Source: ", style=LABEL)
    line.append(state.source, style="cyan")
    return Panel(
        line,
        title=Text(_spaced("USAGE MONITOR"), style=TITLE_STYLE),
        border_style=BORDER,
    )


def _append_action(nav: Text, title: str, key: str, active: bool = False) -> None:
    """Append a nav entry, bracketing the hotkey letter in place. The active
    view gets a filled teal pill; others show a cyan bracket on dim text."""
    pos = title.lower().find(key)
    if pos < 0:
        title, pos = f"{key.upper()}{title}", 0
    left, mid, right = title[:pos], title[pos].upper(), title[pos + 1:]
    if active:
        nav.append(f" {left}[{mid}]{right} ", style="bold black on cyan")
        return
    nav.append(f" {left}", style=DIM)
    nav.append(f"[{mid}]", style="cyan")
    nav.append(f"{right} ", style=DIM)


def render_footer(state: UsageState, view: str) -> RenderableType:
    nav = Text()
    for name in VIEWS:
        _append_action(nav, name.title(), VIEW_KEYS[name], active=name == view)
    nav.append("  ")
    _append_action(nav, "Refresh", "r")
    _append_action(nav, "Quit", "q")

    if state.last_error:
        status = Text(f"⚠ {state.last_error} (showing last-known data)", style="red")
    else:
        status = Text()
        status.append("● ", style="green")
        status.append("Last refresh OK. ", style=LABEL)
        status.append("Next refresh in ", style=DIM)
        status.append(f"{state.next_refresh_in}s", style=VALUE)
        status.append(".", style=DIM)
    return Panel(Group(nav, status), border_style=BORDER)


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
