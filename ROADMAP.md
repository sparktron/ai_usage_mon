# Feature Roadmap

A forward-looking plan for `usage-monitor` — the real-time TUI for Claude and
Codex usage. Items are grouped by theme and tagged with a rough horizon:

- **Now** — small, high-value, low-risk; fits the current architecture cleanly.
- **Next** — meaningful features that need design but no rearchitecting.
- **Later** — larger bets that change the shape of the app.

Each item notes the primary files it touches so scope is visible up front.

---

## Theme 1 — Alerting & thresholds

The tool currently *shows* usage but never *warns*. The most-requested behavior
for a quota monitor is "tell me before I hit the wall."

- **[Now] Threshold alerts in the footer.** When any window (5h session, weekly,
  or a token cap) crosses a configurable percentage, surface a sticky warning in
  `render_footer`. Reuse `color_for_pct` thresholds (50/80%) as defaults.
  *Touches:* `ui.py`, `config.py`.
- **[Next] Desktop / terminal-bell notifications.** Fire an OS notification
  (`notify-send` on Linux, `osascript` on macOS) once per crossing, debounced so
  it doesn't re-fire every 30s tick. Opt-in via config.
  *Touches:* new `notify.py`, `app.py` (`_refresh_loop`), `config.py`.
- **[Next] Projected-exhaustion estimate.** Using the trailing burn rate from
  `hourly_buckets`, estimate when the weekly window will hit 100% and show
  "exhausts in ~3d" next to the bar. *Touches:* `cache.py`, `ui.py`.

## Theme 2 — More data sources / providers

Today there are two sources (Anthropic usage API, ccusage) and three providers
(`Claude`, `Codex`, `Other` in `models.py`).

- **[Now] First-class Gemini/OpenAI pricing.** `provider_for_model` already
  buckets `gpt`/`o1`/`o3` as Codex; add explicit Gemini classification and
  pricing rows so `Other` shrinks. *Touches:* `models.py` (`PRICING`,
  `provider_for_model`).
- **[Next] Pluggable source interface.** `_fetch_records` in `app.py` hardcodes
  "API first, then ccusage." Extract a small `UsageSource` protocol so new
  sources (OpenAI usage API, a Codex CLI exporter) register without editing the
  fetch ladder. *Touches:* `app.py`, new `sources/` package.
- **[Later] Multi-account / multi-org.** Track more than one credential set and
  aggregate or tab between them. *Touches:* `config.py`, `oauth_usage.py`,
  `app.py`, `ui.py`.

## Theme 3 — Pricing accuracy

`PRICING` in `models.py` is a hardcoded table; rates drift as Anthropic/OpenAI
change them. Note: the primary user is on a **subscription, not API billing**, so
cost figures are API-equivalent value — token caps are the real quota. Pricing
work should stay proportionate to that.

- **[Now] Externalize the pricing table.** Move `PRICING` into a shipped JSON
  file that config can override, so updates don't require a code change.
  *Touches:* `models.py`, `config.py`.
- **[Later] Auto-refresh pricing from a remote manifest.** Optional, cached,
  with the built-in table as the offline fallback. *Touches:* `models.py`, new
  fetch path.

## Theme 4 — History & trends

The SQLite cache (`cache.py`) holds 30 days but the UI only shows the last
7 days / 24 hours. There's latent value in the stored history.

- **[Next] Sparkline / trend view.** A new view (extend `VIEWS` in `ui.py`)
  drawing a 30-day daily-token sparkline from `daily_buckets`.
  *Touches:* `ui.py`, `app.py` (view handling).
- **[Next] CSV / JSON export.** A `usage-monitor export --since 7d` subcommand
  that dumps `recent_records` for external analysis. *Touches:* `__main__.py`,
  `cache.py`.
- **[Later] Configurable retention.** `prune_older_than` is fixed at 30 days;
  make it a config field with a longer ceiling for trend analysis.
  *Touches:* `cache.py`, `config.py`, `app.py`.

## Theme 5 — UX polish

- **[Now] Responsive layout for narrow terminals.** The README already flags
  truncation below ~80 cols; have `render` stack panels vertically under a
  width threshold. *Touches:* `ui.py`.
- **[Now] Configurable color thresholds.** `color_for_pct` hardcodes 50/80%;
  expose them in config for users who want earlier warnings.
  *Touches:* `ui.py`, `config.py`.
- **[Next] In-app help overlay.** A `?` key toggling a keybinding/legend panel,
  rather than relying solely on the footer. *Touches:* `ui.py`, `app.py`
  (`handle_key`).

## Theme 6 — Robustness & operations

- **[Now] Surface token-refresh state.** `oauth_usage.py` raises `TokenExpired`
  when Claude Code's token is stale; show a clear, actionable footer message
  instead of a generic error. *Touches:* `oauth_usage.py`, `app.py`, `ui.py`.
- **[Next] Retry/backoff telemetry.** `AnthropicUsageClient.fetch_usage` already
  retries; expose last-attempt count / latency in the Raw view for debugging.
  *Touches:* `api.py`, `ui.py`.
- **[Next] Health self-check command.** `usage-monitor doctor` that reports which
  sources are reachable (API key valid? ccusage on PATH? credentials readable?)
  and exits — useful for first-run setup. *Touches:* `__main__.py`, reuse
  `ccusage_fallback.is_available`, `oauth_usage.read_token`.

---

## Suggested near-term sequence

A pragmatic first slice that delivers visible value with low risk:

1. Threshold alerts in the footer (Theme 1) — biggest behavior gain.
2. Configurable color thresholds + responsive layout (Theme 5) — quick wins.
3. Externalize the pricing table (Theme 3) — removes a recurring maintenance tax.
4. `usage-monitor doctor` (Theme 6) — smooths onboarding.

## Non-goals (for now)

- A web/GUI frontend — the TUI is the product.
- Writing or mutating any usage/billing data — the tool stays strictly
  read-only (the OAuth token is read-only by design).
- Acting as a general-purpose API client; scope is usage observability.
