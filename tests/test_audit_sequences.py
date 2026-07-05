"""Audit tests: hand-constructed price sequences with independently computed
expectations, one per registered strategy.

Unlike the unit tests (which mostly use generated ramps), each sequence here is
built so the expected indicator values and the exact trigger bar can be derived
by hand — these tests pin down boundary behavior (strict vs. inclusive
comparisons, current-bar exclusion, crossover edge detection) that a ramp can't.
"""

import unittest

from bot.strategies import make_strategy
from bot.strategy import BUY, HOLD, SELL, StrategyConfig


def candles(closes, highs=None, lows=None, start_t=1_000_000, span=3600):
    highs = highs or closes
    lows = lows or closes
    return [
        {"time": start_t + i * span, "open": c, "high": h, "low": l, "close": c}
        for i, (c, h, l) in enumerate(zip(closes, highs, lows))
    ]


class DonchianHandSequence(unittest.TestCase):
    """donchian_period=20, exit=10, atr=14. Channel bounds computed by hand."""

    def setUp(self):
        self.cfg = StrategyConfig(donchian_period=20, donchian_exit_period=10, atr_period=14)
        self.strat = make_strategy("donchian_breakout", self.cfg)

    def test_breakout_fires_only_above_exact_prior_high(self):
        # 20 prior bars whose highs peak at exactly 110 (bar 5), then the
        # current bar closes at 110.01. Upper channel must be 110 (prior bars
        # only, current excluded), and the strict > must fire.
        highs = [100 + (10 if i == 5 else i % 3) for i in range(20)]  # max = 110
        closes = [h - 0.5 for h in highs]
        lows = [h - 1.0 for h in highs]
        highs += [110.01]
        closes += [110.01]
        lows += [109.0]
        sig = self.strat.generate_signal("TEST-USD", candles(closes, highs, lows))
        self.assertEqual(sig.action, BUY)
        self.assertEqual(sig.indicators["donchian_upper"], 110.0)

    def test_close_equal_to_channel_high_holds(self):
        # Exactly AT the prior high is not a breakout (strict >).
        highs = [100 + (10 if i == 5 else i % 3) for i in range(20)]
        closes = [h - 0.5 for h in highs]
        lows = [h - 1.0 for h in highs]
        highs += [110.0]
        closes += [110.0]
        lows += [109.0]
        sig = self.strat.generate_signal("TEST-USD", candles(closes, highs, lows))
        self.assertEqual(sig.action, HOLD)

    def test_current_bar_high_never_raises_its_own_channel(self):
        # The current bar spikes to 120 intra-bar but the channel must still be
        # the PRIOR bars' 110 — including the current bar would make a breakout
        # impossible by definition (price can never exceed its own high).
        highs = [100 + (10 if i == 5 else 0) for i in range(20)]
        closes = [h - 0.5 for h in highs]
        lows = [h - 1.0 for h in highs]
        highs += [120.0]
        closes += [115.0]
        lows += [109.0]
        sig = self.strat.generate_signal("TEST-USD", candles(closes, highs, lows))
        self.assertEqual(sig.action, BUY)
        self.assertEqual(sig.indicators["donchian_upper"], 110.0)

    def test_exit_below_exact_prior_low(self):
        # Lows of the prior 10 bars bottom at exactly 90; close at 89.99 exits.
        highs = [101.0] * 20 + [100.0]
        lows = [90 + (0 if i == 15 else 1 + i % 2) for i in range(20)]  # min (last 10) = 90
        closes = [92.0] * 20 + [89.99]
        lows += [89.5]
        sig = self.strat.generate_signal("TEST-USD", candles(closes, highs, lows))
        self.assertEqual(sig.action, SELL)
        self.assertEqual(sig.indicators["donchian_lower"], 90.0)


class RsiMeanReversionHandSequence(unittest.TestCase):
    """rsi_period=14. Sequences whose RSI is exact by construction."""

    def setUp(self):
        self.cfg = StrategyConfig(rsi_period=14, rsi_mr_oversold=30, rsi_mr_overbought=55)
        self.strat = make_strategy("rsi_mean_reversion", self.cfg)

    def test_all_losses_rsi_zero_buys(self):
        # 15 straight -1 moves: avg_gain = 0 -> RSI = 0 exactly -> BUY.
        closes = [100.0 - i for i in range(16)]
        sig = self.strat.generate_signal("TEST-USD", candles(closes))
        self.assertEqual(sig.action, BUY)
        self.assertEqual(sig.indicators["rsi"], 0.0)

    def test_all_gains_rsi_hundred_sells(self):
        # 15 straight +1 moves: avg_loss = 0 -> RSI = 100 exactly -> SELL.
        closes = [100.0 + i for i in range(16)]
        sig = self.strat.generate_signal("TEST-USD", candles(closes))
        self.assertEqual(sig.action, SELL)
        self.assertEqual(sig.indicators["rsi"], 100.0)

    def test_balanced_updown_rsi_fifty_holds(self):
        # Alternate +1/-1 for 14 deltas: avg_gain == avg_loss -> RSI = 50 -> HOLD
        # (50 sits between the 30 buy and 55 sell triggers).
        closes = [100.0]
        for i in range(14):
            closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
        sig = self.strat.generate_signal("TEST-USD", candles(closes))
        self.assertEqual(sig.action, HOLD)
        self.assertEqual(sig.indicators["rsi"], 50.0)

    def test_exact_threshold_is_inclusive(self):
        # 7 gains of g and 7 losses of l in the first window give
        # RSI = 100*g/(g+l). Pick g=3, l=7 -> RSI = 30.0 exactly -> BUY (<=).
        closes = [100.0]
        for i in range(14):
            closes.append(closes[-1] + (3.0 if i % 2 == 0 else -7.0))
        sig = self.strat.generate_signal("TEST-USD", candles(closes))
        self.assertEqual(sig.indicators["rsi"], 30.0)
        self.assertEqual(sig.action, BUY)


class EmaCrossoverHandSequence(unittest.TestCase):
    """Small periods so the crossover bar is hand-verifiable; filters off where
    they'd need 200 bars, then tested separately."""

    def cfg(self, **kw):
        base = dict(
            fast_period=2,
            slow_period=4,
            ma_type="sma",  # SMAs make the crossover arithmetic exact by hand
            rsi_period=3,
            # A flat warmup pins Wilder RSI to exactly 100 (avg_loss = 0), so the
            # overbought gate is parked above 100 — these tests isolate the MA
            # logic; the RSI gate has its own tests elsewhere.
            rsi_overbought=101,
            trend_filter=False,
            adx_filter=False,
            allow_trend_reentry=False,  # test the pure edge trigger
        )
        base.update(kw)
        return StrategyConfig(**base)

    def test_cross_fires_on_exact_bar_and_only_that_bar(self):
        # closes: 10,10,10,10,10 -> flat (fast == slow, no cross)
        # append 14: fast SMA2 = 12, slow SMA4 = 11 -> fast>slow, prev fast<=prev slow -> BUY
        strat = make_strategy("ema_crossover", self.cfg())
        flat = [10.0] * 5
        sig = strat.generate_signal("T", candles(flat))
        self.assertEqual(sig.action, HOLD)

        crossed = flat + [14.0]
        sig = strat.generate_signal("T", candles(crossed))
        # hand check: fast = (10+14)/2 = 12; slow = (10+10+10+14)/4 = 11
        self.assertEqual(sig.indicators["fast_sma"], 12.0)
        self.assertEqual(sig.indicators["slow_sma"], 11.0)
        self.assertEqual(sig.action, BUY)

        # One bar later (still above, no new cross, reentry disabled): HOLD.
        after = crossed + [14.0]
        sig = strat.generate_signal("T", candles(after))
        self.assertEqual(sig.action, HOLD)

    def test_reentry_level_trigger_fires_after_the_cross_bar(self):
        strat = make_strategy("ema_crossover", self.cfg(allow_trend_reentry=True))
        seq = [10.0] * 5 + [14.0, 14.0]  # one bar past the cross
        sig = strat.generate_signal("T", candles(seq))
        self.assertEqual(sig.action, BUY)  # level trigger re-arms the entry

    def test_bearish_cross_sells_on_exact_bar(self):
        strat = make_strategy("ema_crossover", self.cfg())
        seq = [10.0] * 5 + [6.0]
        # fast = (10+6)/2 = 8; slow = (10+10+10+6)/4 = 9 -> fast<slow, cross down
        sig = strat.generate_signal("T", candles(seq))
        self.assertEqual(sig.indicators["fast_sma"], 8.0)
        self.assertEqual(sig.indicators["slow_sma"], 9.0)
        self.assertEqual(sig.action, SELL)

    def test_trend_filter_blocks_buy_below_trend_ma(self):
        # trend SMA(8) over mostly-100s stays near 100 while price pops from
        # far below it: crossover fires but price 60 < trend MA -> HOLD.
        strat = make_strategy(
            "ema_crossover", self.cfg(trend_filter=True, trend_period=8)
        )
        seq = [100.0] * 5 + [50.0, 50.0, 50.0, 50.0, 60.0]
        # fast SMA2 = (50+60)/2 = 55 > slow SMA4 = (50*3+60)/4 = 52.5 (fresh cross:
        # prev fast = prev slow = 50); trend SMA8 = (100*3+50*4+60)/8 = 70 > 60.
        sig = strat.generate_signal("T", candles(seq))
        self.assertEqual(sig.action, HOLD)
        self.assertIn("below trend MA", sig.reasons[0])


class TrendLongShortHandSequence(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(
            fast_period=2, slow_period=4, ma_type="sma", rsi_period=3,
            # Flat-then-jump tapes pin RSI to exactly 100 (all-gain) or 0
            # (all-loss), which would trip the RSI gates; park both outside
            # [0, 100] to isolate the MA/trend logic. The gates get their own
            # dedicated test below.
            rsi_overbought=101, rsi_oversold=-1,
            trend_filter=True, trend_period=8, adx_filter=False,
            allow_trend_reentry=False,
        )
        base.update(kw)
        return StrategyConfig(**base)

    def test_short_signal_is_exact_mirror_of_long(self):
        strat = make_strategy("trend_long_short", self.cfg())
        # Downtrend mirror of the long test: price collapses below the trend MA.
        seq = [100.0] * 8 + [60.0]
        # fast SMA2 = (100+60)/2 = 80 < slow SMA4 = (100*3+60)/4 = 90 (fresh
        # bearish cross); trend SMA8 = (100*7+60)/8 = 92.5 > 60 (downtrend).
        sig = strat.generate_signal("T", candles(seq))
        self.assertEqual(sig.indicators["fast_sma"], 80.0)
        self.assertEqual(sig.indicators["slow_sma"], 90.0)
        self.assertEqual(sig.action, SELL)

        # Uptrend: 50s then pop to 90 -> fast SMA2 = 70 > slow SMA4 = 60, trend
        # SMA8 = (50*7+90)/8 = 55 < 90 -> uptrend confirmed -> BUY.
        seq_up = [50.0] * 8 + [90.0]
        sig = strat.generate_signal("T", candles(seq_up))
        self.assertEqual(sig.action, BUY)

    def test_rsi_floor_blocks_short_at_washout_extreme(self):
        # Straight-down closes pin RSI to exactly 0 (< oversold floor 5):
        # the short must be blocked — standing aside at a washed-out extreme.
        strat = make_strategy("trend_long_short", self.cfg(rsi_oversold=5))
        seq = [100.0 - 2 * i for i in range(9)]
        sig = strat.generate_signal("T", candles(seq))
        self.assertEqual(sig.indicators["rsi"], 0.0)
        self.assertEqual(sig.action, HOLD)


class RegimeHandSequence(unittest.TestCase):
    def test_buy_above_ma_sell_below_at_exact_values(self):
        cfg = StrategyConfig(trend_period=4, ma_type="sma", atr_period=3)
        strat = make_strategy("regime", cfg)
        # 100,100,100,108 -> SMA4 = 102; price 108 > 102 -> BUY (bull regime)
        sig = strat.generate_signal("T", candles([100.0, 100.0, 100.0, 100.0, 108.0]))
        self.assertEqual(sig.action, BUY)
        # Same book but last close 96 -> SMA4 of last 4 = (100+100+100+96)/4 = 99
        # price 96 < 99 -> SELL (regime break)
        sig = strat.generate_signal("T", candles([100.0, 100.0, 100.0, 100.0, 96.0]))
        self.assertEqual(sig.action, SELL)

    def test_price_exactly_on_ma_holds(self):
        cfg = StrategyConfig(trend_period=2, ma_type="sma", atr_period=1)
        strat = make_strategy("regime", cfg)
        # closes 90, 110, 100: SMA2 = (110+100)/2 = 105... construct equality:
        # closes a, b with (a+b)/2 == b requires a == b: flat tape sits ON the MA.
        sig = strat.generate_signal("T", candles([100.0, 100.0, 100.0]))
        self.assertEqual(sig.action, HOLD)


class MinCandlesBoundary(unittest.TestCase):
    """Off-by-one: exactly min_candles() bars must produce a real signal, and
    one fewer must hold with the not-enough-data reason."""

    def assert_boundary(self, strategy_type, cfg):
        strat = make_strategy(strategy_type, cfg)
        n = strat.min_candles()
        closes = [100.0 + (i % 5) for i in range(n)]
        sig = strat.generate_signal("T", candles(closes))
        self.assertFalse(
            sig.reasons and sig.reasons[0].startswith("Not enough data"),
            f"{strategy_type}: {n} bars (== min_candles) still 'not enough data'",
        )
        sig = strat.generate_signal("T", candles(closes[:-1]))
        self.assertTrue(
            sig.reasons and sig.reasons[0].startswith("Not enough data"),
            f"{strategy_type}: min_candles()-1 bars did not report 'not enough data'",
        )

    def test_all_strategies(self):
        cfg = StrategyConfig(
            fast_period=3, slow_period=5, trend_period=10, rsi_period=4,
            adx_period=4, atr_period=4, donchian_period=6, donchian_exit_period=4,
        )
        for stype in (
            "ema_crossover", "rsi_mean_reversion", "donchian_breakout",
            "trend_long_short", "regime",
        ):
            with self.subTest(stype):
                self.assert_boundary(stype, cfg)


if __name__ == "__main__":
    unittest.main()
