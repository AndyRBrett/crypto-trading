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
  notifier.py      Web Push alerts to the dashboard PWA (trades, highs, heartbeat)
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

## Push notifications

The dashboard PWA doubles as the notification channel — no extra app. The bot
alerts you on:

- **every closed round trip**, win or loss (`notify_on_loss: false` to get wins
  only);
- **a new portfolio all-time high** (needs to clear the previous peak by 0.5%);
- **a heartbeat** when `heartbeat_days` (default 7) pass with no alert at all.

The heartbeat exists because the first two only fire when something happens. A
long-only bot parked in cash through a downtrend can legitimately go weeks
without trading, and from the phone that is indistinguishable from a crashed
workflow or an expired subscription. The heartbeat makes healthy-and-idle say so.

### Setup

```bash
python -m scripts.vapid_keygen     # generate a keypair
```

1. Add the printed private key as the `VAPID_PRIVATE_KEY` secret (repo →
   Settings → Secrets and variables → Actions), or to `.env` locally.
2. Let the bot tick once. It derives the matching **public** key and publishes it
   as `vapid_public_key` in `state.json`.
3. Open the dashboard on your phone (iOS: add to Home Screen first), tap 🔔, and
   allow notifications. The page subscribes with the key the bot published, so
   the two halves cannot drift apart.
4. Copy the subscription JSON into the `PUSH_SUBSCRIPTION` secret.
5. Run the **Test push notification** workflow to confirm delivery.

> ⚠️ **Rotating `VAPID_PRIVATE_KEY` kills every existing subscription.** A push
> subscription is permanently bound to the key it was created with; afterwards
> the push service rejects every message with `VapidPkHashMismatch`. Always
> re-subscribe (steps 3–4) after a rotation.

### When notifications stop

`scripts/test_push.py` fails the job when the push service rejects the message
and prints what to do about it — a green run means the message was genuinely
accepted. The bot also records the last delivery failure and reports it in
`overseer-status.json` under `errors` (`push notification failing: …`), alongside
`last_notify_at`, so a dead channel shows up in monitoring instead of just
looking like a quiet week. Settings that are enabled but inert for want of a
secret (e.g. `sentiment_enabled: true` with no `ANTHROPIC_API_KEY`) are reported
the same way, as `config: …`.

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
  "risk_breaker": { "tripped_accounts": ["long_short"],
                    "since": { "long_short": "...Z" } },
  "last_fill_at": null, "last_notify_at": "...Z", "errors": [] }
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

`risk_breaker` appears only while at least one account's rolling-risk circuit
breaker is throttling new entries, naming the accounts and when each tripped —
so a book that halved its own size explains itself here rather than looking like
a quiet week. Its absence means everything is sizing normally.

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

`last_notify_at` is the last push the bot successfully delivered, and `errors`
carries any delivery failure (`push notification failing: …`) or enabled-but-inert
setting (`config: …`). Without these a rejected push was invisible: every trading
metric stayed healthy while the phone went quiet.

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
- `cost_floor_enabled`, `cost_floor_margin`, `cost_floor_samples` — the
  transaction-cost gate: skip entries whose projected move doesn't cover the
  round trip (see below).
- `risk_breaker_*` — the rolling-risk circuit breaker: shrink or pause new
  entries while trailing risk-adjusted performance stays negative (see below).
- `data_source` — `public` or `coinbase_advanced`.

### Transaction-cost gate (`cost_floor_*`)

A trade whose projected move is smaller than the cost of making it is a loss
the moment it fills, however good the setup looked. Before any new entry the
bot prices the round trip in basis points —

    round-trip cost = 2 x fee_rate + 2 x median(|recent slippage_bps|)

using the per-fill slippage already logged for that product — and compares it
to the move the trade would actually be managed toward (`take_profit_atr_mult`
x ATR, or the same reward:risk applied to `fallback_stop_pct` when no ATR is
available). The entry is allowed only when

    projected move >= round-trip cost x cost_floor_margin

The measurement runs on every entry candidate whether or not the gate is on and
is written to the signal log (`features.cost_floor`: `edge_bps`, `cost_bps`,
`required_bps`, `samples`), so you can read what the gate *would* have blocked
before enabling it. With `cost_floor_enabled: true` a failing entry is skipped
and logged with `reject_code: below_cost_floor`. Exits, covers, and protective
stops are never gated — an open position can always close. The backtester
applies the identical gate, so a sweep measures the live rule.

### Rolling-risk circuit breaker (`risk_breaker_*`)

Position sizing keys off *price* volatility (ATR), which knows nothing about
whether the strategy is actually working — a book with a −2.5 Sharpe sizes its
next trade exactly like a winning one. The breaker adds that feedback loop.

Every tick it evaluates the trailing risk-adjusted metrics (the same Sharpe and
Sortino the overseer reports, from the persisted equity curve) as of each of the
last `risk_breaker_days` days. A day counts as breaching only when **both**
ratios sit at or below their floors — they disagree exactly when the losses are
all in one tail, which is the case where throttling the whole book is the wrong
call. A day with too little curve to measure breaks the streak rather than
extending it: the breaker fires on evidence of bleeding, never on its absence.

When every one of those days breaches, new entries are sized at
`risk_breaker_size_mult` of normal (`0.0` pauses them entirely, logged as
`reject_code: risk_breaker`). Exits, covers, and protective stops are never
throttled — a de-risking book must still be able to reduce risk, and a size
throttled below the $10 dust floor is skipped rather than filled as a token
trade.

Nothing is latched: the state is recomputed from the curve each tick, so it
clears itself the moment performance recovers, survives restarts and
fresh-VM-per-tick cloud runs with no extra state, and can't get stuck on. Trips
and recoveries are logged, pushed as a notification, and reported in
`overseer-status.json` under `risk_breaker`. The backtester applies the same
throttle, so a sweep measures the live rule.

### Multiple accounts, multiple strategies

Add an `accounts:` block to run several independent paper accounts side by side,
each with its own strategy, markets, starting cash, and SQLite DB
(`trading.<name>.db`) — all surfaced in one dashboard with per-account tabs and a
portfolio-total summary. The Total tab shows a **strategy comparison table**
(per-account equity, return, realized/unrealized P&L, win rate over completed
round trips, profit factor, 30-day max drawdown/Sharpe, fees, open positions —
from the per-account `stats` block the bot exports) plus a **combined exposure**
line (gross long/short and net, by asset, summed across all accounts — the
accounts trade independently, so this is where a correlated all-in lean shows up). Omit `accounts:` to keep the original single-account
behavior. `strategy_type` selects the algorithm from the registry in
`strategies.py`:

- `ema_crossover` — trend-following EMA crossover (the original/default).
- `rsi_mean_reversion` — counter-trend: buy oversold RSI, sell back toward the mean.
- `donchian_breakout` — breakout: buy new N-bar highs, exit on M-bar lows.
- `trend_long_short` — symmetric EMA trend follower that can **short** confirmed
  downtrends as well as go long uptrends. The only sleeve that can make money
  while the market falls instead of sitting in cash. Needs `allow_short: true`.
- `regime` — stay-invested regime filter: hold a long while price is above the
  long-term trend MA (200-day by default), move to cash on a break below it.
  Built to recapture the buy-and-hold upside the tactical long-only accounts
  give up. Long/cash only.
- `momentum_rotation` — cross-sectional relative strength: rank the account's
  products by trailing `rotation_lookback_bars` return, hold ONLY the leader,
  and only while the leader is above its own trend MA **and its trailing
  return is positive** — otherwise cash (the least-bad loser is still a
  loser). The one strategy that compares assets *against each other* instead
  of against their own history. Long/cash only; price-only (no sentiment).
  ⚠ Multi-year backtest validation is still outstanding (the backtester is
  single-instrument; see `scripts/backtest.py`, which skips it). See
  [ENABLING.md](ENABLING.md) before turning it on.

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
  - name: long_short          # can profit when the market falls
    strategy_type: trend_long_short
    products: [BTC-USD, ETH-USD]
    starting_cash: 10000
    allow_short: true
  - name: regime              # hold the bull, cash the bear
    strategy_type: regime
    products: [BTC-USD, ETH-USD]
    starting_cash: 10000
    risk_per_trade_pct: 0.95  # size to the equity cap, not a tight stop
    max_position_pct: 0.95
    stop_loss_atr_mult: 10    # wide "disaster only" stop; exit on the regime break
    take_profit_atr_mult: 1000
    trailing_stop: false
```

Per-account `strategy:` overrides merge over the top-level strategy defaults, and
risk controls are inherited unless overridden per account. `allow_short`
(default `false`) is the single switch that lets an account open shorts — only
`trend_long_short` emits short entries. See `config.example.yaml` for the fully
commented version.

**Shorting mechanics (paper).** Positions are signed: a SELL while flat opens a
short (crediting the proceeds to cash), and a later BUY covers it, with realized
P&L of `(entry − exit) × qty`. Equity is `cash + quantity × price`, so as a
short's price falls its negative market value rises toward zero and equity grows.
Stops/targets invert for shorts (stop above entry, target below), and the
Chandelier trailing stop rides the lowest low since entry.

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
