"""Signal generation.

The default strategy is an SMA crossover gated by an RSI filter:

  * BUY  when the fast SMA crosses *above* the slow SMA (bullish crossover)
         and RSI is not already overbought.
  * SELL when the fast SMA crosses *below* the slow SMA (bearish crossover),
         or RSI is overbought (take profit).
  * HOLD otherwise.

Every signal carries the indicator snapshot and a list of plain-English
reasons. Those reasons are what the dashboard shows and what Claude turns
into a human explanation, so the trading logic stays fully auditable.
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


@dataclass
class StrategyConfig:
    fast_period: int = 12
    slow_period: int = 26
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # News-sentiment gating (only applied when a Sentiment is supplied).
    sentiment_buy_veto: float = -0.4  # block BUYs when sentiment <= this
    sentiment_sell_trigger: float = -0.6  # risk-off SELL when sentiment <= this


class Strategy:
    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def min_candles(self) -> int:
        """Minimum candles needed to produce a meaningful signal."""
        c = self.config
        # +1 so we can also compute the *previous* SMAs for crossover detection.
        return max(c.slow_period, c.rsi_period + 1) + 1

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes = [float(candle["close"]) for candle in candles]
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return Signal(
                product_id=product_id,
                action=HOLD,
                price=price,
                reasons=[f"Not enough data yet ({len(closes)}/{self.min_candles()} candles)."],
            )

        fast = indicators.sma(closes, c.fast_period)
        slow = indicators.sma(closes, c.slow_period)
        prev_fast = indicators.sma(closes[:-1], c.fast_period)
        prev_slow = indicators.sma(closes[:-1], c.slow_period)
        rsi_val = indicators.rsi(closes, c.rsi_period)

        snapshot = {
            "fast_sma": round(fast, 2),
            "slow_sma": round(slow, 2),
            "prev_fast_sma": round(prev_fast, 2),
            "prev_slow_sma": round(prev_slow, 2),
            "rsi": round(rsi_val, 2),
            "fast_period": c.fast_period,
            "slow_period": c.slow_period,
            "rsi_period": c.rsi_period,
        }

        bullish_cross = prev_fast <= prev_slow and fast > slow
        bearish_cross = prev_fast >= prev_slow and fast < slow
        gap = abs(fast - slow) / slow if slow else 0.0

        reasons: list[str] = []
        action = HOLD
        strength = 0.0

        if bullish_cross and rsi_val < c.rsi_overbought:
            action = BUY
            reasons.append(
                f"Fast SMA({c.fast_period}) crossed above slow SMA({c.slow_period}) "
                f"({fast:.2f} > {slow:.2f}) — bullish momentum."
            )
            reasons.append(f"RSI {rsi_val:.1f} below overbought ({c.rsi_overbought:.0f}) — room to run.")
            strength = min(1.0, 0.5 + gap * 10)
        elif bullish_cross and rsi_val >= c.rsi_overbought:
            reasons.append(
                f"Bullish SMA crossover, but RSI {rsi_val:.1f} is overbought "
                f"(>= {c.rsi_overbought:.0f}) — skipping the buy."
            )
        elif bearish_cross:
            action = SELL
            reasons.append(
                f"Fast SMA({c.fast_period}) crossed below slow SMA({c.slow_period}) "
                f"({fast:.2f} < {slow:.2f}) — bearish momentum."
            )
            strength = min(1.0, 0.5 + gap * 10)
        elif rsi_val >= c.rsi_overbought:
            action = SELL
            reasons.append(
                f"RSI {rsi_val:.1f} is overbought (>= {c.rsi_overbought:.0f}) — taking profit."
            )
            strength = min(1.0, (rsi_val - c.rsi_overbought) / (100 - c.rsi_overbought))
        else:
            trend = "above" if fast > slow else "below"
            reasons.append(
                f"No crossover. Fast SMA {trend} slow SMA; RSI {rsi_val:.1f} is neutral."
            )

        # Fold in news sentiment, if available. Sentiment confirms or vetoes the
        # price-based decision; it never invents a BUY on its own.
        if sentiment is not None:
            snapshot["sentiment_score"] = round(sentiment.score, 3)
            snapshot["sentiment_label"] = sentiment.label
            if action == BUY and sentiment.score <= c.sentiment_buy_veto:
                action = HOLD
                strength = 0.0
                reasons.append(
                    f"Vetoed BUY: news sentiment bearish "
                    f"({sentiment.score:+.2f}) — {sentiment.summary}"
                )
            elif action != BUY and sentiment.score <= c.sentiment_sell_trigger:
                if action != SELL:
                    reasons.append(
                        f"Risk-off SELL: news sentiment strongly bearish "
                        f"({sentiment.score:+.2f}) — {sentiment.summary}"
                    )
                action = SELL
                strength = max(strength, min(1.0, abs(sentiment.score)))
            else:
                reasons.append(
                    f"News sentiment {sentiment.label} ({sentiment.score:+.2f})."
                )
                if action == BUY and sentiment.score > 0:
                    strength = min(1.0, strength + 0.2)

        return Signal(
            product_id=product_id,
            action=action,
            price=price,
            indicators=snapshot,
            reasons=reasons,
            strength=round(strength, 2),
        )
