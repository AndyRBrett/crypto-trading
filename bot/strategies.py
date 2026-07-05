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
    insufficient_data,
    ohlc,
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
        closes, highs, lows = ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return insufficient_data(product_id, price, len(closes), self.min_candles())

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

        # Distance to each trigger: RSI must fall to/below oversold to BUY, or
        # rise to/above overbought to SELL. Signed gaps make a HOLD legible.
        thresholds = {
            # <= 0 once RSI has fallen to the oversold BUY trigger.
            "rsi_to_oversold": round(rsi_val - c.rsi_mr_oversold, 2),
            # <= 0 once RSI has risen to the overbought SELL trigger.
            "rsi_to_overbought": round(c.rsi_mr_overbought - rsi_val, 2),
        }

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
            thresholds=thresholds,
        )


# -- Symmetric long/short trend follower ------------------------------------


@register("trend_long_short")
class TrendLongShortStrategy:
    """The EMA-crossover trend follower, made symmetric so it can SHORT downtrends.

    Mirror image of the long-only ``ema_crossover``:

      * BUY  when the fast MA is above the slow MA *and* price is above the
             long-term trend MA *and* ADX confirms a trend *and* RSI isn't
             overbought — the long entry (and, while short, the cover signal).
      * SELL when the fast MA is below the slow MA *and* price is below the trend
             MA *and* ADX confirms *and* RSI isn't oversold — the short entry
             (and, while long, the exit signal).

    Direction is the engine's job: a BUY opens a long while flat or covers a
    short; a SELL opens a short (only on a short-enabled account) while flat or
    closes a long. The engine's direction-aware ATR stops/targets ride on top.
    """

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def _ma(self, values, period):
        return indicators.moving_average(values, period, self.config.ma_type)

    def min_candles(self) -> int:
        # Same history requirement as the long-only crossover it mirrors.
        return Strategy(self.config).min_candles()

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes, highs, lows = ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return insufficient_data(product_id, price, len(closes), self.min_candles())

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
        downtrend = trend_ma is None or price < trend_ma
        trend_ok = adx_val is None or adx_val >= c.adx_min
        # Re-arm both sides: a level trigger (fast above/below slow) in addition
        # to the edge-based cross, so a trend isn't skipped just because the bot
        # wasn't ticking on the exact crossover bar.
        long_trigger = bullish_cross or (c.allow_trend_reentry and fast > slow)
        short_trigger = bearish_cross or (c.allow_trend_reentry and fast < slow)

        reasons: list[str] = []
        action = HOLD
        strength = 0.0

        if long_trigger and uptrend and trend_ok and rsi_val < c.rsi_overbought:
            action = BUY
            verb = "crossed above" if bullish_cross else "above"
            reasons.append(
                f"Fast {c.ma_type.upper()}({c.fast_period}) {verb} slow "
                f"{c.ma_type.upper()}({c.slow_period}) ({fast:.2f} > {slow:.2f}) — long the uptrend."
            )
            if trend_ma is not None:
                reasons.append(f"Price ${price:,.2f} above trend MA({c.trend_period}) ${trend_ma:,.2f}.")
            if adx_val is not None:
                reasons.append(f"ADX {adx_val:.1f} ≥ {c.adx_min:.0f} — trend has strength.")
            strength = min(1.0, 0.5 + gap * 10)
        elif short_trigger and downtrend and trend_ok and rsi_val > c.rsi_oversold:
            action = SELL
            verb = "crossed below" if bearish_cross else "below"
            reasons.append(
                f"Fast {c.ma_type.upper()}({c.fast_period}) {verb} slow "
                f"{c.ma_type.upper()}({c.slow_period}) ({fast:.2f} < {slow:.2f}) — short the downtrend."
            )
            if trend_ma is not None:
                reasons.append(f"Price ${price:,.2f} below trend MA({c.trend_period}) ${trend_ma:,.2f}.")
            if adx_val is not None:
                reasons.append(f"ADX {adx_val:.1f} ≥ {c.adx_min:.0f} — trend has strength.")
            strength = min(1.0, 0.5 + gap * 10)
        else:
            trend_word = "above" if fast > slow else "below"
            reasons.append(
                f"No qualifying trend. Fast MA {trend_word} slow MA; RSI {rsi_val:.1f}; "
                f"waiting for a confirmed trend with the long-term MA."
            )

        # Signed distance to the gates, for the HOLD activity log / tuning.
        thresholds = {
            "ma_gap_pct": round((fast - slow) / slow * 100, 3) if slow else 0.0,
            "rsi_to_overbought": round(c.rsi_overbought - rsi_val, 2),
            "rsi_to_oversold": round(rsi_val - c.rsi_oversold, 2),
        }
        if trend_ma is not None and trend_ma > 0:
            thresholds["price_to_trend_pct"] = round((price - trend_ma) / trend_ma * 100, 3)
        if adx_val is not None:
            thresholds["adx_to_min"] = round(adx_val - c.adx_min, 2)

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
            thresholds=thresholds,
        )


# -- Regime filter (200-day trend) ------------------------------------------


@register("regime")
class RegimeStrategy:
    """Stay-invested-in-the-bull regime filter: long while above the trend MA.

    Built to close the buy-and-hold gap that long-only tactical TA leaves on the
    table (see TODO.md). It holds a long whenever price is above the long-term
    trend MA (``trend_period``, default 200) — riding the secular uptrend the way
    a holder would — and moves fully to cash when price breaks below it, sidestep-
    ping deep bear markets. Long/cash only; it never shorts.

    The thesis is "ignore the noise, follow the 200-day," so it deliberately does
    NOT fold in news sentiment. Pair it (in config) with loose/disabled ATR stops
    and a high ``risk_per_trade_pct`` so it sizes to the equity cap and holds
    through ordinary pullbacks — the only routine exit is the regime break.
    """

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def _ma(self, values, period):
        return indicators.moving_average(values, period, self.config.ma_type)

    def min_candles(self) -> int:
        c = self.config
        return max(c.trend_period, c.atr_period) + 1

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes, highs, lows = ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return insufficient_data(product_id, price, len(closes), self.min_candles())

        trend_ma = self._ma(closes, c.trend_period)
        atr_val = indicators.atr(highs, lows, closes, c.atr_period)

        snapshot = {"trend_ma": round(trend_ma, 2), "trend_period": c.trend_period}
        if atr_val is not None:
            snapshot["atr"] = round(atr_val, 2)

        reasons: list[str] = []
        if price > trend_ma:
            action = BUY
            reasons.append(
                f"Price ${price:,.2f} above the {c.trend_period}-period trend MA "
                f"${trend_ma:,.2f} — bull regime, holding the long."
            )
            strength = min(1.0, (price - trend_ma) / trend_ma * 10) if trend_ma else 0.5
        elif price < trend_ma:
            action = SELL
            reasons.append(
                f"Price ${price:,.2f} broke below the {c.trend_period}-period trend MA "
                f"${trend_ma:,.2f} — regime break, moving to cash."
            )
            strength = min(1.0, (trend_ma - price) / trend_ma * 10) if trend_ma else 0.5
        else:
            action = HOLD
            strength = 0.0
            reasons.append(f"Price ${price:,.2f} sitting on the trend MA — holding.")

        thresholds = {
            "price_to_trend_pct": round((price - trend_ma) / trend_ma * 100, 3)
            if trend_ma else 0.0,
        }
        # Intentionally no sentiment gating — the regime model follows price only.
        return Signal(
            product_id=product_id,
            action=action,
            price=price,
            indicators=snapshot,
            reasons=reasons,
            strength=round(strength, 2),
            thresholds=thresholds,
        )


# -- Cross-sectional momentum rotation ---------------------------------------


@register("momentum_rotation")
class MomentumRotationStrategy:
    """Relative-strength rotation: hold only the strongest asset, gated by trend.

    A genuinely different mechanism from the other five strategies, which are
    all *time-series* rules evaluated per asset in isolation: this one is
    *cross-sectional* — every tick it ranks the whole configured universe by
    trailing ``rotation_lookback_bars`` return and wants to hold only the
    leader, and only while that leader trades above its own long-term trend MA
    (``trend_period``, default 200). Everything else (and the leader, in a bear
    regime) gets a SELL, which the engine treats as "exit if held" — so the
    account rotates into strength and sits in cash when even the leader is
    below trend.

    Cross-sectional ranking needs the full universe, which the per-product
    ``generate_signal`` contract can't supply — the engine's ``prepare`` hook
    provides it once per tick. Called without ``prepare`` (e.g. the
    single-product backtester), it emits HOLD rather than degenerate into a
    one-horse race. Price-only by design: no sentiment gating.

    Backtest validation is OUTSTANDING: the backtester is single-instrument by
    design and the exchange isn't reachable from this environment, so this
    strategy ships unit-tested against hand-constructed sequences only.
    """

    # scripts/backtest skips strategies that need the whole universe at once.
    requires_universe = True

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()
        # Trailing lookback return per product, refreshed by prepare() each
        # tick. Ordinary dict: insertion order (= config product order) breaks
        # exact momentum ties deterministically.
        self._momentum: dict[str, float] = {}

    def _ma(self, values, period):
        return indicators.moving_average(values, period, self.config.ma_type)

    def min_candles(self) -> int:
        c = self.config
        return max(c.rotation_lookback_bars + 1, c.trend_period, c.atr_period + 1)

    def prepare(self, candles_by_product: dict) -> None:
        """Rank inputs for this tick: trailing return per product (closed bars)."""
        c = self.config
        self._momentum = {}
        for pid, candles in candles_by_product.items():
            closes = [float(x["close"]) for x in candles]
            if len(closes) >= c.rotation_lookback_bars + 1 and closes[-c.rotation_lookback_bars - 1] > 0:
                self._momentum[pid] = closes[-1] / closes[-c.rotation_lookback_bars - 1] - 1.0

    def generate_signal(
        self, product_id: str, candles: Sequence[dict], sentiment=None
    ) -> Signal:
        c = self.config
        closes, highs, lows = ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return insufficient_data(product_id, price, len(closes), self.min_candles())

        trend_ma = self._ma(closes, c.trend_period)
        atr_val = indicators.atr(highs, lows, closes, c.atr_period)
        own_mom = self._momentum.get(product_id)

        snapshot = {
            "trend_ma": round(trend_ma, 2),
            "trend_period": c.trend_period,
            "rotation_lookback_bars": c.rotation_lookback_bars,
        }
        if atr_val is not None:
            snapshot["atr"] = round(atr_val, 2)
        if own_mom is not None:
            snapshot["momentum_pct"] = round(own_mom * 100, 2)

        if not self._momentum:
            return Signal(
                product_id=product_id,
                action=HOLD,
                price=price,
                indicators=snapshot,
                reasons=[
                    "Rotation needs the tick's full market snapshot (prepare() "
                    "was not called) — holding."
                ],
            )

        leader = max(self._momentum, key=self._momentum.get)
        leader_mom = self._momentum[leader]
        snapshot["leader"] = leader
        snapshot["leader_momentum_pct"] = round(leader_mom * 100, 2)

        reasons: list[str] = []
        rank_txt = ", ".join(
            f"{pid} {mom * 100:+.1f}%" for pid, mom in self._momentum.items()
        )

        if product_id == leader and price > trend_ma:
            action = BUY
            reasons.append(
                f"{product_id} leads {c.rotation_lookback_bars}-bar momentum "
                f"({rank_txt}) — rotating into the strongest asset."
            )
            reasons.append(
                f"Price ${price:,.2f} above trend MA({c.trend_period}) "
                f"${trend_ma:,.2f} — leader confirmed by its own trend."
            )
            others = [m for pid, m in self._momentum.items() if pid != leader]
            edge = leader_mom - max(others) if others else leader_mom
            strength = min(1.0, 0.5 + edge * 5)
        elif product_id == leader:
            action = SELL
            reasons.append(
                f"{product_id} leads momentum ({rank_txt}) but price "
                f"${price:,.2f} is below its trend MA({c.trend_period}) "
                f"${trend_ma:,.2f} — even the leader is in a bear regime, cash."
            )
            strength = 0.5
        else:
            action = SELL
            reasons.append(
                f"Not the momentum leader ({rank_txt} — leader: {leader}) — "
                f"exiting/standing aside to hold only the strongest."
            )
            strength = 0.5

        thresholds = {
            # <= 0 once this product has caught up to the leader's momentum.
            "momentum_to_leader_pct": round((leader_mom - (own_mom or 0.0)) * 100, 3),
            "price_to_trend_pct": round((price - trend_ma) / trend_ma * 100, 3)
            if trend_ma
            else 0.0,
        }
        # Price-only by design (like the regime filter): no sentiment gating.
        return Signal(
            product_id=product_id,
            action=action,
            price=price,
            indicators=snapshot,
            reasons=reasons,
            strength=round(strength, 2),
            thresholds=thresholds,
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
        closes, highs, lows = ohlc(candles)
        price = closes[-1] if closes else 0.0

        if len(closes) < self.min_candles():
            return insufficient_data(product_id, price, len(closes), self.min_candles())

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

        # Distance to each channel edge: price breaking above the upper channel
        # is the BUY trigger, below the lower channel is the SELL/exit. Signed
        # gaps make a HOLD-inside-the-channel legible (how near a breakout was).
        thresholds = {
            # > 0 once price breaks above the entry channel high (BUY).
            "breakout_dist_pct": round((price - upper) / upper * 100, 3) if upper else 0.0,
            # < 0 once price breaks below the exit channel low (SELL).
            "exit_dist_pct": round((price - lower) / lower * 100, 3) if lower else 0.0,
        }

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
            thresholds=thresholds,
        )
