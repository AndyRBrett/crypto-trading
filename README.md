# crypto-trading — Paper Trading Bot 🤖

A crypto **paper trading** bot: it pulls real market data from Coinbase, runs a
technical strategy, and simulates trades against a virtual portfolio — no real
money, ever. Each trade is explained in plain English by **Claude**, and a
static dashboard shows your equity curve, positions, and the bot's reasoning.

> Status: **v1**. Local Python loop · SMA-crossover + RSI strategy ·
> Claude trade explanations · static dashboard.

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
2. **Signal** — the strategy computes SMAs + RSI and emits `BUY` / `SELL` / `HOLD`
   with a list of plain-English reasons.
3. **Decide** — respecting current position, available cash, and position sizing.
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
  indicators.py    SMA / EMA / RSI  (pure, unit-tested)
  strategy.py      SMA crossover + RSI filter -> signals with reasons
  portfolio.py     paper portfolio: cash, positions, cost basis, P&L
  storage.py       SQLite (durable) + dashboard JSON export
  explain.py       Claude trade explanations (+ deterministic fallback)
  engine.py        one tick: data -> signal -> trade -> explain -> persist
  main.py          CLI: once / run / status / verify / reset
dashboard/
  index.html       static dashboard (reads state.json)
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
- `starting_cash`, `fee_rate`, `buy_fraction` — the paper account.
- `poll_interval`, `candle_granularity` — cadence (keep them aligned).
- `strategy.{fast_period, slow_period, rsi_period, rsi_overbought, rsi_oversold}`.
- `data_source` — `public` or `coinbase_advanced`.

## Testing

```bash
python -m pytest
```

Covers the indicators (SMA/EMA/RSI), the paper portfolio (fills, fees, cost
basis, P&L, restart-replay), and the strategy's signal logic.

## Notes & caveats

- **Paper only.** Nothing here places real orders. The portfolio is virtual.
- The default strategy is a textbook SMA crossover — a learning scaffold, not
  alpha. Tune it, or swap in your own in `strategy.py`.
- Restart-safe: portfolio state is reconstructed by replaying the trade log
  from SQLite, so you can stop and resume without losing history.

## Roadmap

- [ ] **LLM news sentiment** — pull crypto headlines, have Claude score
      sentiment, feed it into the signal.
- [ ] **Natural-language strategy config** — "be aggressive when BTC dominance
      is rising" compiled into rules.
- [ ] Backtesting harness over historical candles.
- [ ] More indicators / strategies (MACD, Bollinger, multi-timeframe).
- [ ] Always-on hosting option.
