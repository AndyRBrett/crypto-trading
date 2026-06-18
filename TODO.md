# TODO / Future Exploration

Ideas worth exploring, with enough context to pick up cold later.

## Close (or beat) the buy-and-hold gap

**Status:** open / exploratory.

**Context.** Multi-year daily backtests (run via `python -m scripts.sweep` /
`python -m scripts.backtest`) showed the current long-only TA strategies are
genuinely profitable net of fees *on the daily timeframe* and with low drawdown
(~5–7%), and the edge is robust (e.g. 193/324 EMA combos net-positive in both
in-sample and holdout). **But every variant trails buy-and-hold by a wide
margin** — the strategies returned tens of percent while simply holding returned
hundreds of percent over the same window. The `B&H (vs …)` column in the
backtest output makes this gap explicit.

Why: long-only trend/breakout exit on weakness, so they sit in cash through the
dips that holders ride to new highs. No parameter tuning fixes this — it's
structural to long-only TA in a secular bull market.

**Goal.** Capture more of the upside without giving up the low-drawdown profile —
i.e. beat buy-and-hold on a risk-adjusted basis, and ideally narrow the absolute
gap. Honest expectation: matching B&H *absolute* return with lower drawdown is a
realistic target; beating it outright likely needs leverage or shorting.

**Approaches to try (roughly easiest → hardest):**

1. **Stay-in-the-trend-longer variant.** Drop the fixed take-profit, loosen the
   trailing stop, and/or add partial scaling-out so winners run far longer
   instead of capping at the ATR target. Measure how much of the B&H run this
   recaptures.
2. **Long-bias / "hold unless clear breakdown" overlay.** Default to holding a
   core long position; only exit on a strong bearish regime signal (e.g. price
   below the 200-day MA *and* a momentum break), then re-enter on recovery. This
   keeps you invested through normal pullbacks.
3. **Regime switch.** Use a regime filter (trend strength / volatility) to hold
   passively in confirmed bull regimes and switch to tactical TA only in
   chop/bear regimes — get B&H upside in trends, TA's drawdown protection otherwise.
4. **Core + tactical allocation.** Keep a fixed passive core (e.g. 50% buy-and-hold)
   plus a tactical TA sleeve on the rest. Trivially narrows the gap; tune the split.
5. **Leverage / shorting** (out of the current long-only, paper-only scope —
   only if we deliberately expand the mandate). Highest ceiling, highest risk.

**How to measure.** Build each as a strategy in `bot/strategies.py`, then judge it
with the backtester's `B&H (vs …)` column and the sweep's in-sample→holdout split.
Success = beats B&H risk-adjusted (or matches absolute return at lower drawdown)
on out-of-sample data, across multiple markets — not curve-fit to one coin/window.

## Other ideas

- More indicators / strategies: MACD, Bollinger-band mean reversion, multi-timeframe confirmation.
- Natural-language strategy config compiled into rules.
- Walk-forward / multi-market sweep mode (test a setting across several coins at once for a stronger anti-overfitting signal).
