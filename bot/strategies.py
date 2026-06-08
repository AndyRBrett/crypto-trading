"""Pluggable strategies: a registry, a factory, and the alternative algorithms.

The original trend-following EMA-crossover lives in ``bot/strategy.py`` as
``Strategy``; it is registered here as ``"ema_crossover"`` (the default). Each
strategy shares the same contract — ``min_candles()`` and
``generate_signal(product_id, candles, sentiment=None) -> Signal`` returning the
same ``Signal`` shape — so the engine's risk/execution layer is agnostic to which
one ran. Every signal MUST carry ``atr`` in ``indicators`` (the engine's sizing
and protective stops read ``signal.indicators["atr"]``).

Pick a strategy per account with ``make_strategy(strategy_type, config)``.
"""

from __future__ import annotations

from typing import Sequence

from . import indicators
from .strategy import (
    BUY,
    HOLD,
    SELL,
    Signal,
    Strategy,
    StrategyConfig,
    apply_sentiment,
)

# -- registry ---------------------------------------------------------------

_REGISTRY: dict[str, type] = {}


def register(name: str):
    """Class decorator: register a strategy class under ``name``."""

    def deco(cls):
        _REGISTRY[name] = cls
        return cls

    return deco


def make_strategy(strategy_type: str, config: StrategyConfig | None = None):
    """Instantiate the strategy registered under ``strategy_type``."""
    cls = _REGISTRY.get(strategy_type)
    if cls is None:
        raise ValueError(
            f"unknown strategy_type {strategy_type!r}; known: {sorted(_REGISTRY)}"
        )
    return cls(config or StrategyConfig())


def available() -> list[str]:
    """Sorted list of registered strategy_type keys."""
    return sorted(_REGISTRY)


# The original trend-following EMA crossover (defined in strategy.py). Registered
# here rather than there to avoid an import cycle.
register("ema_crossover")(Strategy)


def _ohlc(candles: Sequence[dict]):
    """Pull close/high/low lists, defaulting high/low to close (close-only data)."""
    closes = [float(c["close"]) for c in candles]
    highs = [float(c.get("high", c["close"])) for c in candles]
    lows = [float(c.get("low", c["close"])) for c in candles]
    return closes, highs, lows


# -- RSI mean reversion -----------------------------------------------------


@register("rsi_mean_reversion")
class RsiMeanReversionStrategy:
    """Counter-trend: buy oversold weakness, sell back toward the mean.

    The opposite instinct to the EMA crossover — it fades extremes rather than
    chasing momentum, so it diverges meaningfully on the same market.
    """

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def min_candles(self) -> int:
        c = self.config
        return max(c.rsi_period + 1, c.atr_period + 1)

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes, highs, lows = _ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return Signal(
                product_id=product_id,
                action=HOLD,
                price=price,
                reasons=[f"Not enough data yet ({len(closes)}/{self.min_candles()} candles)."],
            )

        rsi_val = indicators.rsi(closes, c.rsi_period)
        atr_val = indicators.atr(highs, lows, closes, c.atr_period)

        snapshot = {"rsi": round(rsi_val, 2), "rsi_period": c.rsi_period}
        if atr_val is not None:
            snapshot["atr"] = round(atr_val, 2)

        reasons: list[str] = []
        action = HOLD
        strength = 0.0

        if rsi_val <= c.rsi_mr_oversold:
            action = BUY
            reasons.append(
                f"RSI {rsi_val:.1f} ≤ oversold ({c.rsi_mr_oversold:.0f}) — "
                f"fading weakness for a mean-reversion bounce."
            )
            span = max(c.rsi_mr_oversold, 1e-9)
            strength = min(1.0, (c.rsi_mr_oversold - rsi_val) / span + 0.5)
        elif rsi_val >= c.rsi_mr_overbought:
            action = SELL
            reasons.append(
                f"RSI {rsi_val:.1f} ≥ {c.rsi_mr_overbought:.0f} — reverted to the "
                f"mean, taking profit."
            )
            span = max(100 - c.rsi_mr_overbought, 1e-9)
            strength = min(1.0, (rsi_val - c.rsi_mr_overbought) / span + 0.5)
        else:
            reasons.append(
                f"RSI {rsi_val:.1f} is between {c.rsi_mr_oversold:.0f} and "
                f"{c.rsi_mr_overbought:.0f} — no edge, holding."
            )

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


# -- Donchian breakout ------------------------------------------------------


@register("donchian_breakout")
class DonchianBreakoutStrategy:
    """Breakout/trend via price channels (a different mechanism than MA crosses).

    BUY when price breaks above the highest high of the prior ``donchian_period``
    bars; SELL when it breaks below the lowest low of the prior
    ``donchian_exit_period`` bars. The engine's ATR stops/trailing ride on top.
    """

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def min_candles(self) -> int:
        c = self.config
        return max(c.donchian_period, c.donchian_exit_period, c.atr_period) + 1

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes, highs, lows = _ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return Signal(
                product_id=product_id,
                action=HOLD,
                price=price,
                reasons=[f"Not enough data yet ({len(closes)}/{self.min_candles()} candles)."],
            )

        # Channels over the PRIOR bars (exclude the current bar, which is breaking).
        upper = max(highs[-c.donchian_period - 1 : -1])
        lower = min(lows[-c.donchian_exit_period - 1 : -1])
        atr_val = indicators.atr(highs, lows, closes, c.atr_period)

        snapshot = {
            "donchian_upper": round(upper, 2),
            "donchian_lower": round(lower, 2),
            "donchian_period": c.donchian_period,
        }
        if atr_val is not None:
            snapshot["atr"] = round(atr_val, 2)

        reasons: list[str] = []
        action = HOLD
        strength = 0.0

        if price > upper:
            action = BUY
            reasons.append(
                f"Breakout: price ${price:,.2f} broke above the "
                f"{c.donchian_period}-bar high ${upper:,.2f} — momentum entry."
            )
            strength = min(1.0, 0.5 + (price - upper) / upper * 20 if upper else 0.5)
        elif price < lower:
            action = SELL
            reasons.append(
                f"Channel exit: price ${price:,.2f} broke below the "
                f"{c.donchian_exit_period}-bar low ${lower:,.2f} — exiting."
            )
            strength = min(1.0, 0.5 + (lower - price) / lower * 20 if lower else 0.5)
        else:
            reasons.append(
                f"Price ${price:,.2f} inside the channel "
                f"(${lower:,.2f} – ${upper:,.2f}) — holding."
            )

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
