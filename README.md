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
  portfolio.py     paper portfolio: cash, positions, cost basis, P&L
  storage.py       SQLite (durable) + dashboard JSON export
  explain.py       Claude trade explanations (+ deterministic fallback)
  publish.py       push state.json to GitHub Pages (phone viewing)
  engine.py        one tick: data -> signal -> trade -> explain -> persist
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
   *Run workflow*. After that it runs hourly on its own.

Notes:
- Cron timing is approximate (GitHub may delay a run by several minutes).
- The cloud and your laptop keep **separate** portfolios. Pick one as your
  "real" run — if the cloud is on, you don't need the local loop.
- Edit `config.ci.yaml` to change what the cloud bot trades or how it behaves.

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
- [ ] Backtesting harness over historical candles.
- [ ] More indicators / strategies (MACD, Bollinger, multi-timeframe).
- [ ] Always-on hosting option.
