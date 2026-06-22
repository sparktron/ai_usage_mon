# usage-monitor

A standalone Python CLI that continuously monitors **Claude** and **Codex** API
usage in real time, rendering weekly and hourly breakdowns in a color-coded TUI.

```
╭──────────────────────────── USAGE MONITOR ─────────────────────────────╮
│ Last updated: 2026-06-21 14:32:05 UTC   |   Source: ccusage            │
╰─────────────────────────────────────────────────────────────────────────╯
╭──────────── THIS WEEK ────────────╮  ╭──────────── LAST HOUR ────────────╮
│ Claude   125.4K / 500.0K (25%)    │  │ Claude   8.2K / 3.0K (16%)        │
│ Codex     42.1K / 200.0K (21%)    │  │ Codex    1.3K / 1.2K (6%)         │
│                                   │  │                                   │
│ Cost     $2.14 / $100.00 (2%)     │  │ Cost     $0.08 / $0.60 (13%)      │
╰───────────────────────────────────╯  ╰───────────────────────────────────╯
╭───────────────────────────────────────────────────────────────────────────╮
│  [D]ashboard  [W]eekly  [H]ourly  [R]aw   [R]efresh  [Q]uit                │
│ Last refresh OK. Next refresh in 45s.                                      │
╰───────────────────────────────────────────────────────────────────────────╯
```

## Features

- **Dashboard, Weekly, Hourly, and Raw** views, switchable live.
- Per-provider tracking of input/output/total tokens, USD cost, and % of quota.
- Color coding: **green** <50%, **yellow** 50–80%, **red** >80%.
- Async, non-blocking refresh every 30–60s (configurable).
- Anthropic usage API as the primary source, automatic **ccusage** fallback.
- Local SQLite cache with 30-day retention; seeds from ccusage on first run.
- Graceful error handling — failures show in the footer, never crash the UI.

## Install

```bash
pip install -e .          # from a checkout
pip install -e ".[dev]"   # with test dependencies
```

Requires Python 3.10+.

## Usage

```bash
usage-monitor                 # launch the TUI
usage-monitor --refresh 30    # refresh every 30 seconds
usage-monitor --config ./my-config.json
usage-monitor --debug         # log every refresh cycle to the log file
```

### Keys

| Key | Action |
| --- | --- |
| `d` | Dashboard |
| `w` | Weekly breakdown |
| `h` | Hourly breakdown |
| `r` | Raw data view |
| `j` / `k` / `l` / `←` `→` | Cycle views |
| `R` | Force refresh now |
| `q` | Quit |

## Configuration

Settings load from (highest precedence first): command-line flags, environment
variables, the JSON config file, then built-in defaults.

**Config file:** `~/.config/usage-monitor/config.json`

```json
{
  "refresh_interval": 60,
  "weekly_cost_cap": 100.0,
  "hourly_cost_cap": null,
  "weekly_token_cap_claude": 500000,
  "weekly_token_cap_codex": 200000,
  "use_ccusage_fallback": true,
  "api_base_url": "https://api.anthropic.com"
}
```

`hourly_cost_cap` defaults to `weekly_cost_cap / 168` (hours per week) when null.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | API auth. **Preferred** over the config file; never hardcode keys. |
| `USAGE_MONITOR_REFRESH_INTERVAL` | Override refresh interval. |
| `USAGE_MONITOR_WEEKLY_COST_CAP` | Override the weekly USD cap. |

Any `Config` field can be set via `USAGE_MONITOR_<FIELD>`.

## Data sources & storage

1. **Anthropic API** (`/v1/organizations/usage_report/messages`) when
   `ANTHROPIC_API_KEY` is set. Hourly buckets, grouped by model.
2. **ccusage** CLI fallback (`ccusage daily --json`) when the API is missing or
   unreachable. Install separately: `npm install -g ccusage`.

Records are cached in SQLite at `~/.cache/usage-monitor/usage.db`, keyed by
`(timestamp, model)` so re-fetching updates in place. Data older than 30 days
is pruned on startup. Errors are logged to
`~/.cache/usage-monitor/usage-monitor.log` (file only — never to the console).

## Cost calculation

Costs come from a built-in pricing table (USD per 1M tokens, see
[`models.py`](usage_monitor/models.py)). The longest matching model-name prefix
wins. Unknown models fall back to a Sonnet-class default. When a source already
reports cost (ccusage does), that value is used as-is.

## Development

```bash
pytest                 # run tests with coverage
pytest -q tests/test_cache.py
```

The test suite covers config loading, cache queries/bucketing, cost/model
logic, API and ccusage parsing, and UI rendering (>90% coverage on library
modules).

## Troubleshooting

- **"ccusage not found on PATH"** in the footer: install ccusage
  (`npm install -g ccusage`) or set `ANTHROPIC_API_KEY` to use the API.
- **"usage API returned 401"**: the usage report endpoint needs an
  organization/admin-scoped key. Without one, rely on the ccusage fallback.
- **Empty views**: the cache may be empty and ccusage unavailable. Check the log
  file for details.
- **Garbled display**: ensure your terminal is at least ~80 columns wide; the
  layout adapts but very narrow terminals truncate panels.

## License

MIT — see [LICENSE](LICENSE).
