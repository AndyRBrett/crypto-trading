# Bot paused — 2026-11-07

The scheduled bot is **paused**, not deleted. Nothing is running, and the bot
makes no Anthropic (Claude), Coinbase or push-notification calls.

## What was done

- **[PR #80](https://github.com/AndyRBrett/crypto-trading/pull/80)** added a
  job-level `if: ${{ false }}` guard to `.github/workflows/run-bot.yml`. Every
  trigger now produces a skipped run that does no work: the hourly cron, the
  external scheduler's `workflow_dispatch`, and manual runs from the Actions tab.
- The external scheduler (cron-job.org) is **not** controlled from this repo.
  It keeps dispatching the workflow each hour until it's paused there. Each of
  those runs is skipped and costs nothing.

## Why

The main reason was to cut Claude API spend. Claude was called for news
sentiment (Haiku, once per daily bar per coin, so about 3 calls a day) and for
one short explanation per trade. The spend was small, but it bought nothing:
the strategies weren't working. By the time of the pause every account's
risk breaker had tripped, so new trades were already being made at half size.

## Performance at pause (Jun 8 – Nov 7, 2026, paper money)

Each account started with $10,000.

| Account        | Live since | Equity  | Return | Closed trades (wins) | Max drawdown |
|----------------|------------|---------|--------|----------------------|--------------|
| trend          | Jun 8      | $9,763  | −2.4%  | 13 (4)               | −5.7%        |
| breakout       | Jun 8      | $9,626  | −3.7%  | 14 (4)               | −6.5%        |
| mean_reversion | Jul 10     | $9,927  | −0.7%  | 0 (1 open)           | −1.0%        |
| regime         | Oct 20     | $9,106  | −8.9%  | 3 (0)                | −11.3%       |
| long_short     | Nov 3      | $9,884  | −1.2%  | 1 (0)                | −1.4%        |
| **Total**      |            | **$48,306** | **−3.4% (−$1,694)** | **31 (8, 26% win rate)** | |

Buy-and-hold over the same window: BTC +19%, ETH +33%, SOL +33%.

- Fees (~$692) were about 40% of the total loss. Before fees, trend roughly
  broke even and breakout came out slightly ahead.
- Regime's BTC exit on Nov 7 (−$842) was half of all losses.
- This matches `TODO.md`: long-only technical-analysis strategies trail
  buy-and-hold in a rising market.

## State left in place

- **Trade history:** `trading.<account>.db` files on the `bot-state` branch,
  untouched.
- **Two open paper positions,** frozen as they were: a SOL long
  (mean_reversion) and an ETH short (long_short).
- **GitHub Pages dashboard:** still published, showing the last state.
- **`overseer-status.json`:** no longer refreshes, so the Overseer monitor may
  report it as stale.

## To resume

1. Delete the `if: ${{ false }}` line (and its comment block) from
   `.github/workflows/run-bot.yml`.
2. Re-enable the cron-job.org job if it was paused.
3. Nothing else needs resetting. The risk breakers aren't latched: they
   recalculate from the equity curve every tick and clear themselves once
   performance recovers. Until then, new trades are made at half size. Don't
   run `python -m bot.main reset` unless you want to wipe the trade history.

## To retire fully (not done)

Disable or delete `run-bot.yml`, delete the cron-job.org job, remove the
`ANTHROPIC_API_KEY` Actions secret, and decide whether to keep the `bot-state`
branch and the Pages site.
