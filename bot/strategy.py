"""Signal generation.

A trend-following strategy built the way a discretionary-systematic trader
would gate entries — multiple confirmations, trade *with* the higher-timeframe
trend, and stay out of chop:

  * BUY  when the fast MA crosses *above* the slow MA (momentum turning up),
         AND price is above the long-term trend MA (trade with the trend),
         AND ADX shows a real trend (not chop),
         AND RSI is not already overbought.
  * SELL when the fast MA crosses *below* the slow MA, or RSI is overbought.
         (Hard stops, take-profits and the trailing stop live in the engine,
         since they react to price between signals, not just on a crossover.)
  * HOLD otherwise.

News sentiment, when supplied, can veto a BUY or force a risk-off SELL — it
never invents a BUY on its own.

Every signal carries the indicator snapshot (including ATR, used by the engine
for sizing and stops) and plain-English reasons, so the logic stays auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import indicators

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


@dataclass
class Signal:
    product_id: str
    action: str
    price: float
    indicators: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    strength: float = 0.0  # 0..1, rough confidence for display


def apply_sentiment(action, strength, snapshot, sentiment, config, reasons):
    """Fold news sentiment into a price-based decision.

    Sentiment confirms or vetoes; it never invents a BUY on its own. Shared by
    all strategies so the gating logic stays consistent. Mutates ``snapshot``
    and ``reasons`` in place; returns the (possibly updated) ``(action, strength)``.
    """
    if sentiment is None:
        return action, strength
    snapshot["sentiment_score"] = round(sentiment.score, 3)
    snapshot["sentiment_label"] = sentiment.label
    if action == BUY and sentiment.score <= config.sentiment_buy_veto:
        reasons.append(
            f"Vetoed BUY: news sentiment bearish "
            f"({sentiment.score:+.2f}) — {sentiment.summary}"
        )
        return HOLD, 0.0
    if action != BUY and sentiment.score <= config.sentiment_sell_trigger:
        if action != SELL:
            reasons.append(
                f"Risk-off SELL: news sentiment strongly bearish "
                f"({sentiment.score:+.2f}) — {sentiment.summary}"
            )
        return SELL, max(strength, min(1.0, abs(sentiment.score)))
    reasons.append(f"News sentiment {sentiment.label} ({sentiment.score:+.2f}).")
    if action == BUY and sentiment.score > 0:
        strength = min(1.0, strength + 0.2)
    return action, strength


@dataclass
class StrategyConfig:
    # Moving-average crossover.
    fast_period: int = 20
    slow_period: int = 50
    ma_type: str = "ema"  # "ema" (default) or "sma"
    # Re-arm entries: enter whenever the fast MA is above the slow MA and the
    # filters pass, not only on the exact crossover bar. A crossover is a
    # single-bar event — if the bot isn't ticking at that instant, or a filter
    # blocks it that once, an edge-triggered entry is missed forever. Level-based
    # re-arming lets the entry fire on any qualifying bar while flat (the engine
    # only buys when there's no open position), so trends aren't skipped.
    allow_trend_reentry: bool = True
    # Momentum oscillator.
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # Trend filter: only go long while price is above this long-term MA.
    trend_filter: bool = True
    trend_period: int = 200
    # Chop filter: only trade when ADX shows a trend of at least this strength.
    adx_filter: bool = True
    adx_period: int = 14
    adx_min: float = 20.0
    # Volatility (ATR) — surfaced for the engine's sizing/stops.
    atr_period: int = 14
    # News-sentiment gating (only applied when a Sentiment is supplied).
    sentiment_buy_veto: float = -0.4  # block BUYs when sentiment <= this
    sentiment_sell_trigger: float = -0.6  # risk-off SELL when sentiment <= this
    # Donchian breakout (used by the "donchian_breakout" strategy).
    donchian_period: int = 20  # entry channel: break the high of this many bars
    donchian_exit_period: int = 10  # exit channel: break the low of this many bars
    # RSI mean-reversion (used by the "rsi_mean_reversion" strategy).
    rsi_mr_oversold: float = 30.0  # BUY when RSI <= this (oversold bounce)
    rsi_mr_overbought: float = 55.0  # SELL when RSI >= this (reverted to mean)


class Strategy:
    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def _ma(self, values, period):
        if self.config.ma_type == "sma":
            return indicators.sma(values, period)
        return indicators.ema(values, period)

    def min_candles(self) -> int:
        """Minimum candles needed to produce a meaningful signal."""
        c = self.config
        base = max(c.slow_period, c.rsi_period + 1)
        if c.trend_filter:
            base = max(base, c.trend_period)
        if c.adx_filter:
            base = max(base, 2 * c.adx_period + 1)
        # +1 so we can also compute the *previous* MAs for crossover detection.
        return base + 1

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes = [float(candle["close"]) for candle in candles]
        # High/low default to close so close-only candles (e.g. tests) still work.
        highs = [float(candle.get("high", candle["close"])) for candle in candles]
        lows = [float(candle.get("low", candle["close"])) for candle in candles]
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return Signal(
                product_id=product_id,
                action=HOLD,
                price=price,
                reasons=[f"Not enough data yet ({len(closes)}/{self.min_candles()} candles)."],
            )

        fast = self._ma(closes, c.fast_period)
        slow = self._ma(closes, c.slow_period)
        prev_fast = self._ma(closes[:-1], c.fast_period)
        prev_slow = self._ma(closes[:-1], c.slow_period)
        rsi_val = indicators.rsi(closes, c.rsi_period)
        trend_ma = self._ma(closes, c.trend_period) if c.trend_filter else None
        atr_val = indicators.atr(highs, lows, closes, c.atr_period)
        adx_val = indicators.adx(highs, lows, closes, c.adx_period) if c.adx_filter else None

        snapshot = {
            "fast_sma": round(fast, 2),
            "slow_sma": round(slow, 2),
            "prev_fast_sma": round(prev_fast, 2),
            "prev_slow_sma": round(prev_slow, 2),
            "rsi": round(rsi_val, 2),
            "ma_type": c.ma_type,
            "fast_period": c.fast_period,
            "slow_period": c.slow_period,
            "rsi_period": c.rsi_period,
        }
        if trend_ma is not None:
            snapshot["trend_ma"] = round(trend_ma, 2)
        if atr_val is not None:
            snapshot["atr"] = round(atr_val, 2)
        if adx_val is not None:
            snapshot["adx"] = round(adx_val, 2)

        bullish_cross = prev_fast <= prev_slow and fast > slow
        bearish_cross = prev_fast >= prev_slow and fast < slow
        gap = abs(fast - slow) / slow if slow else 0.0
        uptrend = trend_ma is None or price > trend_ma
        trend_ok = adx_val is None or adx_val >= c.adx_min
        # Re-arm: a level-based entry trigger (fast above slow) in addition to the
        # edge-based crossover, so an uptrend isn't skipped just because the bot
        # wasn't ticking on the exact crossover bar. The engine still only acts on
        # a BUY while flat, so this re-enters trends rather than stacking.
        reentry = c.allow_trend_reentry and fast > slow
        entry_trigger = bullish_cross or reentry

        reasons: list[str] = []
        action = HOLD
        strength = 0.0

        if entry_trigger and rsi_val < c.rsi_overbought and uptrend and trend_ok:
            action = BUY
            if bullish_cross:
                reasons.append(
                    f"Fast {c.ma_type.upper()}({c.fast_period}) crossed above slow "
                    f"{c.ma_type.upper()}({c.slow_period}) ({fast:.2f} > {slow:.2f}) — bullish momentum."
                )
            else:
                reasons.append(
                    f"Fast {c.ma_type.upper()}({c.fast_period}) above slow "
                    f"{c.ma_type.upper()}({c.slow_period}) ({fast:.2f} > {slow:.2f}) — established uptrend, entering."
                )
            if trend_ma is not None:
                reasons.append(f"Price ${price:,.2f} above trend MA({c.trend_period}) ${trend_ma:,.2f} — with the trend.")
            if adx_val is not None:
                reasons.append(f"ADX {adx_val:.1f} ≥ {c.adx_min:.0f} — trend has strength.")
            reasons.append(f"RSI {rsi_val:.1f} below overbought ({c.rsi_overbought:.0f}) — room to run.")
            strength = min(1.0, 0.5 + gap * 10)
        elif entry_trigger and not uptrend:
            reasons.append(
                f"Fast MA above slow, but price ${price:,.2f} is below trend MA({c.trend_period}) "
                f"${trend_ma:,.2f} — counter-trend, skipping the buy."
            )
        elif entry_trigger and not trend_ok:
            reasons.append(
                f"Fast MA above slow, but ADX {adx_val:.1f} < {c.adx_min:.0f} — market is choppy, skipping."
            )
        elif bullish_cross and rsi_val >= c.rsi_overbought:
            # On a *fresh* crossover that's already overbought, stand aside rather
            # than chase. (A re-arm entry that's overbought falls through to the
            # take-profit SELL below instead — we'd be holding by then.)
            reasons.append(
                f"Bullish crossover, but RSI {rsi_val:.1f} is overbought "
                f"(>= {c.rsi_overbought:.0f}) — skipping the buy."
            )
        elif bearish_cross:
            action = SELL
            reasons.append(
                f"Fast {c.ma_type.upper()}({c.fast_period}) crossed below slow "
                f"{c.ma_type.upper()}({c.slow_period}) ({fast:.2f} < {slow:.2f}) — bearish momentum."
            )
            strength = min(1.0, 0.5 + gap * 10)
        elif rsi_val >= c.rsi_overbought:
            action = SELL
            reasons.append(
                f"RSI {rsi_val:.1f} is overbought (>= {c.rsi_overbought:.0f}) — taking profit."
            )
            strength = min(1.0, (rsi_val - c.rsi_overbought) / (100 - c.rsi_overbought))
        else:
            trend_word = "above" if fast > slow else "below"
            reasons.append(
                f"No crossover. Fast MA {trend_word} slow MA; RSI {rsi_val:.1f} is neutral."
            )

        # Fold in news sentiment, if available. Sentiment confirms or vetoes the
        # price-based decision; it never invents a BUY on its own.
        action, strength = apply_sentiment(
            action, strength, snapshot, sentiment, c, reasons
        )

        return Signal(
            product_id=product_id,
            action=action,
            price=price,
            indicators=snapshot,
            reasons=reasons,
            strength=round(strength, 2),
        )
