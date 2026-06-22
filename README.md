# crypto-trading — Paper Trading Bot 🤖

A crypto **paper trading** bot: it pulls real market data from Coinbase, runs a
technical strategy, and simulates trades against a virtual portfolio — no real
money, ever. Each trade is explained in plain English by **Claude**, and a
static dashboard shows your equity curve, positions, and the bot's reasoning.

> Status: **v1**. Local or cloud (scheduled) loop · trend-following EMA
> strategy with trend/ADX/RSI filters + ATR risk management · optional Claude
> news-sentiment signals · Claude trade explanations · installable PWA dashboard.

---

## Why this exists

This is a sandbox for agentic-automation patterns: a decision loop that pulls
data, reasons about it, acts, persists state, and explains itself. The trading
strategy is deliberately simple and fully auditable — the interesting part is
the wiring. See the [roadmap](#roadmap) for where it's headed (LLM news
sentiment, natural-language strategy config).

## How it works

Every `poll_interval` seconds, for each market:

1. **Fetch** recent candles from Coinbase.
2. **Signal** — a trend-following EMA crossover, gated by a long-term trend
   filter (trade with the trend), an ADX chop filter (only trade real trends),
   RSI, and optional news sentiment. Emits `BUY` / `SELL` / `HOLD` with reasons.
3. **Size & protect** — entries are sized by volatility (ATR) so each trade
   risks a fixed % of equity; open positions are guarded by an ATR stop-loss, a
   take-profit target, and a Chandelier trailing stop, with a cap on how many
   positions can be open at once.
4. **Execute** the simulated trade against the paper portfolio (with a fee).
5. **Explain** — Claude turns the signal into a human-readable "why".
6. **Persist** the trade + an equity snapshot to SQLite, and export
   `dashboard/state.json` for the UI.

The strategy decides; Claude only *describes*. If Claude is unavailable, the
bot falls back to a templated explanation and keeps trading.

```
bot/
  config.py        config from config.yaml + .env (secrets)
  market_data.py   Coinbase: public API (default) or Advanced SDK (your keys)
  indicators.py    SMA / EMA / RSI / ATR / ADX  (pure, unit-tested)
  sentiment.py     RSS news -> Claude sentiment score (optional)
  strategy.py      EMA crossover + trend/ADX/RSI filters (+ sentiment) -> signals
  strategies.py    strategy registry + RSI-mean-reversion & Donchian-breakout
  portfolio.py     paper portfolio: cash, positions, cost basis, P&L
  storage.py       SQLite (durable) + dashboard JSON export
  explain.py       Claude trade explanations (+ deterministic fallback)
  publish.py       push state.json to GitHub Pages (phone viewing)
  engine.py        one tick for ONE account: data -> signal -> trade -> persist
  runner.py        orchestrates multiple accounts -> one combined dashboard
  main.py          CLI: once / run / status / verify / reset
dashboard/
  index.html       static PWA dashboard (reads state.json)
  manifest.json    sw.js   make_icons.py   icon-*.png   (installable on phone)
tests/             unit tests for indicators, portfolio, strategy
```

## Quick start

```bash
# 1. Install deps (a virtualenv is recommended)
pip install -r requirements.txt

# 2. (Optional) configure
cp config.example.yaml config.yaml      # edit markets, strategy, cadence
cp .env.example .env                    # add your API keys

# 3. Run a single decision cycle
python -m bot.main once

# 4. Or run the loop
python -m bot.main run

# 5. View the dashboard (separate terminal)
cd dashboard && python -m http.server 8000
#   then open http://localhost:8000
```

The bot runs **out of the box with no config and no keys** — it uses Coinbase's
public market-data API and a templated (non-LLM) trade rationale.

### Adding your keys

Put secrets in `.env` (gitignored):

- `ANTHROPIC_API_KEY` — enables Claude-written trade explanations.
- `COINBASE_API_KEY` / `COINBASE_API_SECRET` — your Coinbase Developer Platform
  (Advanced Trade) keys. Only needed if you set `data_source: coinbase_advanced`
  in `config.yaml`. Verify they work with:

  ```bash
  python -m bot.main verify
  ```

> Paper trading only needs **market data**, which the public API provides for
> free — so your keys are optional. They're wired in for when you want to use
> the same authenticated feed you'd trade against live.

## News sentiment (optional)

With `sentiment_enabled: true` and an `ANTHROPIC_API_KEY`, the bot pulls recent
crypto headlines (keyless RSS feeds), asks Claude to score near-term sentiment
for each asset (-1 bearish … +1 bullish), and folds that into the signal:

- A bullish/neutral score **confirms** a price-based BUY (and nudges its
  strength); sentiment never invents a BUY on its own.
- A sufficiently bearish score **vetoes** a BUY (`sentiment_buy_veto`).
- A strongly bearish score **triggers a risk-off SELL** of an open position
  (`sentiment_sell_trigger`).

Scores are cached for `sentiment_cache_ttl` seconds so short poll intervals
don't hammer the feeds or the API, and every failure (no key, no network, no
relevant headlines) degrades to neutral — the bot keeps trading on price alone.
The score and Claude's one-line summary show up in the dashboard and in each
trade's explanation.

## View it on your phone

The dashboard is a PWA and can be hosted free on GitHub Pages — the bot pushes
its `state.json` to a `gh-pages` branch each tick, and you open the page on your
phone (and "Add to Home Screen" to get an app icon).

One-time setup:

1. **Create a token.** GitHub → Settings → Developer settings → Fine-grained
   personal access tokens. Scope it to this repo with **Contents: Read and
   write**. Put it in `.env` as `GITHUB_TOKEN=...`.

2. **Enable publishing** in `config.yaml`:

   ```yaml
   publish_enabled: true
   publish_repo: AndyRBrett/crypto-trading   # your owner/repo
   ```

3. **Prime the Pages branch.** Run the bot once so it pushes the first
   `state.json`, and trigger the dashboard deploy (the
   "Deploy dashboard to GitHub Pages" workflow → *Run workflow*, or just push
   any change under `dashboard/`).

4. **Turn on Pages.** Repo → Settings → Pages → Source: **Deploy from a
   branch** → Branch: **gh-pages** / **/(root)** → Save.

Then open `https://<your-user>.github.io/<repo>/` on your phone and add it to
your home screen. It refreshes every few seconds and works offline (showing the
last fetched state). The data updates whenever the bot is running and pushing.

> The bot must be running somewhere to push fresh data. Run it on your laptop,
> or set up always-on cloud runs (below) so it keeps going without you.

## Always-on (run it in the cloud)

`.github/workflows/run-bot.yml` runs the bot on a schedule via GitHub Actions —
free, no server, and it keeps updating the dashboard even when your laptop is
off. Each run is a fresh machine, so the paper portfolio (`trading.db`) is
restored from and saved to a dedicated `bot-state` branch between ticks.
`config.ci.yaml` holds the (non-secret) settings the cloud run uses.

Setup:

1. **Merge this to `main`** so the workflow exists on the default branch
   (scheduled workflows only run from `main`).
2. **(Optional) Add your Anthropic key** for Claude explanations: repo →
   Settings → Secrets and variables → Actions → New repository secret →
   `ANTHROPIC_API_KEY`. Without it, the cloud bot uses the templated rationale.
   No GitHub token needed — Actions provides one automatically.
3. **Kick off the first run:** Actions tab → "Run trading bot (always-on)" →
   *Run workflow*. After that it runs every 15 minutes on its own (entries use
   hourly candles; the frequent checks keep stop-losses responsive).

Notes:
- Cron timing is approximate (GitHub may delay a run by several minutes).
- Edit `config.ci.yaml` to change what the cloud bot trades or how it behaves.

## Laptop as the fast driver (optional)

GitHub's schedule is best-effort and often only fires every hour or two. When
your laptop is on you can drive faster (every 15 min) and have the cloud
automatically step aside — sharing **one continuous portfolio** so P&L never
jumps. This is the `coordinate_*` settings (see `bot/coordinate.py`):

- The shared portfolio (`trading.db`) and a lease (`driver.json`) live on the
  `bot-state` branch. Both drivers pull the DB at startup and push it after each
  tick, via the GitHub API.
- While your laptop runs it refreshes the lease every tick. The cloud checks the
  lease at the start of each run and **stands down** while a local lease is fresh
  (`lease_ttl_seconds`, default 30 min), then resumes automatically once you've
  been gone longer than that.

To make your laptop the fast driver, set in `config.yaml`:

```yaml
publish_enabled: true
publish_repo: AndyRBrett/crypto-trading
coordinate_enabled: true
driver_role: local
```

(plus `GITHUB_TOKEN` in `.env`), then `python -m bot.main run`. The cloud config
(`config.ci.yaml`) already has `coordinate_enabled: true` / `driver_role: cloud`.

## Monitoring (`overseer-status.json`)

A separate **Project Overseer** agent reviews this repo weekly and needs to see
that the bot is alive and how it's doing — otherwise Trading is a blind spot.
`write_status.py` writes `overseer-status.json` at the repo root from the bot's
own SQLite trade stores (`trading*.db`), summarizing the headline 7-day window
plus 30- and 90-day totals:

```json
{ "generated_at": "...Z", "last_run_at": "...Z", "window_days": 7,
  "trades": 2, "pnl": 14.01, "win_rate": 1.0, "win_rate_low_sample": true,
  "pnl_30d": 14.01, "trades_30d": 2, "pnl_90d": 14.01, "trades_90d": 2,
  "benchmark": { "deployed_notional": 966.0, "strategy_pnl": 14.01,
                 "buy_hold_pnl": 7.7, "strategy_return_pct": 1.45,
                 "buy_hold_return_pct": 0.8, "alpha_pct": 0.65 },
  "equity_curve": [ { "t": "...Z", "equity": 1000.0 }, { "t": "...Z", "equity": 1014.01 } ],
  "risk_metrics": { "window_days": 30, "samples": 29, "max_drawdown_pct": 4.20,
                    "volatility_pct": 31.4, "sharpe": 0.82, "sortino": 1.15 },
  "signals_evaluated": 6, "signals_acted": 2,
  "decisions": [ { "product_id": "BTC-USD", "action": "HOLD", "outcome": "hold",
                   "reject_code": "no_signal", "slippage_bps": null,
                   "thresholds": { "ma_gap_pct": -0.42, "rsi_to_overbought": 21.0,
                                   "price_to_trend_pct": -1.2, "adx_to_min": -5.0 } } ],
  "rejection_reasons": { "no_signal": 3, "size_zero": 1 }, "avg_slippage_bps": 3.1,
  "last_fill_at": null, "errors": [] }
```

`generated_at` is how staleness is judged; `win_rate` (0–1) is included once
there are closed trades in the window, with `win_rate_low_sample: true` when
fewer than ten back it so a 1–2 trade week's perfect score is greyed out rather
than trusted. `pnl_30d` / `pnl_90d` (and their trade counts) keep a quiet week
from hiding longer-term performance. A week with zero fills is reported as
data (`trades: 0`), not an error. `last_run_at` (always written),
`signals_evaluated` (signals the strategy scored this run, counted from
`signal_log`) and `signals_acted` (how many of those became a trade) are a
heartbeat: a healthy-but-idle bot (`signals_evaluated > 0`, `trades: 0`) is
distinguishable from a silently dead one (`signals_evaluated: 0`).

`benchmark` turns raw P&L into alpha-vs-holding: it marks each traded symbol at
the window's start and end, holds the notional the strategy actually deployed,
and reports the strategy's return against that buy-and-hold return plus the
`alpha_pct` between them (omitted when no capital was deployed in the window).
`equity_curve` is a small rolling series for a dashboard chart. The decision log
accounts for every evaluated signal: `decisions` lists each one's `outcome`
(`acted` / `rejected` / `hold`) and `reject_code`, `rejection_reasons` tallies
why signals didn't trade (e.g. `no_signal`, `size_zero`, `max_open_positions`),
and `avg_slippage_bps` is the realized signal-to-fill slippage on acted signals.
Each non-acted decision also carries its `thresholds` — the **signed distance to
each decision threshold** captured at evaluation time (e.g. `ma_gap_pct: -0.42`
means the fast MA was 0.42% below the slow MA, so the crossover entry was just
shy of firing). The full snapshot (indicators + thresholds) is persisted per
signal in the `signal_log.features` column of each `trading*.db`, so HOLDs are
queryable for threshold tuning instead of being an invisible gap — the trade log
only ever records the signals that *did* fire.

`risk_metrics` makes a raw P&L number interpretable by scaling return against the
risk taken to earn it. Computed from the persisted equity curve over a **30-day
lookback** (matching the headline `pnl_30d`): `sharpe` and `sortino` are
annualized with the 365-day, 24/7 crypto convention at a 0% risk-free rate,
`max_drawdown_pct` is the worst peak-to-trough decline in the window, and
`volatility_pct` is the annualized standard deviation of daily returns. Equity is
resampled to one observation per UTC day before the ratios are computed; metrics
that need dispersion (or, for Sortino, a losing day) are omitted when the curve
can't support them. See `bot/metrics.py` for the full convention. The dashboard
renders the same numbers in a "Risk-adjusted metrics" panel, computed client-side
from the equity curve in `state.json`.

The always-on workflow regenerates and commits it once a day (right after a
tick, so the trade stores are present), so the monitor always has a fresh
snapshot. Run it by hand anytime with `python write_status.py`.

## CLI

| Command  | What it does                                        |
| -------- | --------------------------------------------------- |
| `once`   | Run one decision cycle and print a portfolio summary |
| `run`    | Loop forever, sleeping `poll_interval` between ticks |
| `status` | Print the current paper portfolio                    |
| `verify` | Check Coinbase Advanced credentials + public data    |
| `reset`  | Delete the database (wipe paper history)             |

Add `-v` for debug logging, `--config path.yaml` for an alternate config.

## Configuration

All settings live in `config.yaml` (see `config.example.yaml` for the full,
commented list). Highlights:

- `products` — Coinbase product IDs, e.g. `BTC-USD`, `ETH-USD`.
- `starting_cash`, `fee_rate` — the paper account.
- `poll_interval`, `candle_granularity`, `candle_count` — cadence + history.
- `strategy.{fast_period, slow_period, ma_type, trend_period, adx_min, ...}` —
  the EMA crossover + trend/ADX/RSI filters.
- `risk_per_trade_pct`, `max_position_pct`, `max_open_positions`,
  `stop_loss_atr_mult`, `take_profit_atr_mult`, `trailing_stop` — risk controls.
- `data_source` — `public` or `coinbase_advanced`.

### Multiple accounts, multiple strategies

Add an `accounts:` block to run several independent paper accounts side by side,
each with its own strategy, markets, starting cash, and SQLite DB
(`trading.<name>.db`) — all surfaced in one dashboard with per-account tabs and a
portfolio-total summary. Omit `accounts:` to keep the original single-account
behavior. `strategy_type` selects the algorithm from the registry in
`strategies.py`:

- `ema_crossover` — trend-following EMA crossover (the original/default).
- `rsi_mean_reversion` — counter-trend: buy oversold RSI, sell back toward the mean.
- `donchian_breakout` — breakout: buy new N-bar highs, exit on M-bar lows.

```yaml
accounts:
  - name: trend
    strategy_type: ema_crossover
    products: [BTC-USD, ETH-USD]
    starting_cash: 10000
  - name: mean_reversion
    strategy_type: rsi_mean_reversion
    products: [BTC-USD, SOL-USD]
    starting_cash: 10000
  - name: breakout
    strategy_type: donchian_breakout
    products: [ETH-USD, SOL-USD]
    starting_cash: 10000
```

Per-account `strategy:` overrides merge over the top-level strategy defaults, and
risk controls are inherited unless overridden per account. See
`config.example.yaml` for the fully commented version.

## Backtesting

Before shipping a strategy or parameter change to live paper trading, measure it
on historical candles. The backtester replays each configured account/strategy
through the *same* risk layer (`bot/risk.py`) and paper portfolio the live engine
uses, so results — return, max drawdown, win-rate, profit factor — are net of fees
and reflect what the bot would actually have done:

```bash
python -m scripts.backtest                       # uses config.yaml
python -m scripts.backtest --count 1000          # more history
python -m scripts.backtest --granularity ONE_DAY # different timeframe
```

Each row reports one strategy on one product. Use it to compare changes head-to-head
instead of waiting weeks for live signal to accrue.

## Testing

```bash
python -m pytest
```

Covers the indicators (SMA/EMA/RSI/ATR/ADX), the paper portfolio (fills, fees,
cost basis, P&L, restart-replay), the strategy's signal logic (crossover, trend
and ADX filters, sentiment gating), and the engine's risk layer (volatility
sizing, stop-loss / take-profit / trailing-stop exits).

## Notes & caveats

- **Paper only.** Nothing here places real orders. The portfolio is virtual.
- The strategy is a sensible trend-following template with real risk management
  — not guaranteed alpha. Tune the knobs in `config.yaml` or swap in your own
  logic in `strategy.py`.
- Restart-safe: portfolio state is reconstructed by replaying the trade log
  from SQLite, so you can stop and resume without losing history.

## Roadmap

- [x] **LLM news sentiment** — pull crypto headlines, have Claude score
      sentiment, feed it into the signal.
- [ ] **Natural-language strategy config** — "be aggressive when BTC dominance
      is rising" compiled into rules.
- [x] Backtesting harness over historical candles (`scripts/backtest.py`, `scripts/sweep.py`).
- [ ] Close (or beat) the buy-and-hold gap — see [TODO.md](TODO.md).
- [ ] More indicators / strategies (MACD, Bollinger, multi-timeframe).
- [ ] Always-on hosting option.
