"""Cross-sectional momentum rotation: hand-constructed sequences.

Same audit style as tests/test_audit_sequences.py — each universe is built so
the momentum ranking and trend-MA gate are computable by hand, pinning the
rotation rules: hold ONLY the leader, only while the leader is above its own
trend MA, everything else (or everyone, in a bear regime) gets a SELL.

NOTE: live/historical backtest validation of this strategy is still
outstanding — the backtester is single-instrument and the exchange isn't
reachable from this environment. These unit tests are the current evidence.
"""

import unittest

from bot.strategies import make_strategy
from bot.strategy import BUY, HOLD, SELL, StrategyConfig

from tests.test_engine import FakeStorage, RecordingStorage, make_engine


def candles(closes, start_t=1_000_000, span=86_400):
    return [
        {"time": start_t + i * span, "open": c, "high": c, "low": c, "close": c}
        for i, c in enumerate(closes)
    ]


def _cfg(**kw):
    # Small windows so every expectation is hand-checkable: momentum over the
    # last 4 bars, trend gate = SMA(8) of the product's own closes.
    base = dict(rotation_lookback_bars=4, trend_period=8, ma_type="sma", atr_period=3)
    base.update(kw)
    return StrategyConfig(**base)


def _strategy(universe, **kw):
    strat = make_strategy("momentum_rotation", _cfg(**kw))
    strat.prepare(universe)
    return strat


class RotationHandSequence(unittest.TestCase):
    def universe(self):
        # 9 daily bars each (min_candles = max(4+1, 8, 3+1) = 8).
        # A: 100 flat, then last 4 bars climb to 120  -> momentum 120/100-1 = +20%
        #    trend SMA8 (last 8) < 120                -> above trend: BUY-able
        # B: 100 flat, then last 4 bars climb to 110  -> +10%, not the leader
        # C: 100 flat, then last 4 bars fall to 80    -> -20%, not the leader
        return {
            "A-USD": candles([100, 100, 100, 100, 100, 105, 110, 115, 120]),
            "B-USD": candles([100, 100, 100, 100, 100, 102, 105, 108, 110]),
            "C-USD": candles([100, 100, 100, 100, 100, 95, 90, 85, 80]),
        }

    def test_leader_gets_buy_others_get_sell(self):
        uni = self.universe()
        strat = _strategy(uni)
        # Hand check the ranking: A +20% > B +10% > C -20%.
        self.assertAlmostEqual(strat._momentum["A-USD"], 0.20)
        self.assertAlmostEqual(strat._momentum["B-USD"], 0.10)
        self.assertAlmostEqual(strat._momentum["C-USD"], -0.20)

        sig_a = strat.generate_signal("A-USD", uni["A-USD"])
        self.assertEqual(sig_a.action, BUY)
        self.assertEqual(sig_a.indicators["leader"], "A-USD")

        sig_b = strat.generate_signal("B-USD", uni["B-USD"])
        self.assertEqual(sig_b.action, SELL)
        self.assertIn("Not the momentum leader", sig_b.reasons[0])

        sig_c = strat.generate_signal("C-USD", uni["C-USD"])
        self.assertEqual(sig_c.action, SELL)

    def test_leader_below_its_trend_ma_means_cash_for_everyone(self):
        # Every asset fell hard, A least badly: A still "leads" momentum but
        # sits below its own SMA8 -> even the leader is SELL (cash), and the
        # laggards are SELL as usual: the whole book goes/stays flat.
        uni = {
            # A: last-4 momentum 90/100-1 = -10%; SMA8 = (3*100+100+97+95+92+90)/8
            #    = 96.75 > 90 -> below trend.
            "A-USD": candles([100, 100, 100, 100, 100, 97, 95, 92, 90]),
            # B: -30%.
            "B-USD": candles([100, 100, 100, 100, 100, 90, 80, 75, 70]),
        }
        strat = _strategy(uni)
        self.assertAlmostEqual(strat._momentum["A-USD"], -0.10)
        sig_a = strat.generate_signal("A-USD", uni["A-USD"])
        self.assertEqual(sig_a.action, SELL)
        self.assertIn("bear regime", sig_a.reasons[0])
        sig_b = strat.generate_signal("B-USD", uni["B-USD"])
        self.assertEqual(sig_b.action, SELL)

    def test_leader_with_negative_momentum_means_cash_even_above_trend_ma(self):
        # The gate added after the real-data check (2026-07-04: SOL "led" at
        # −0.1% vs BTC −8.5% / ETH −15.4%): a leader that is merely the
        # least-bad loser must NOT be bought, even when it trades above its
        # own trend MA. Both gates — trend AND positive momentum — must hold.
        uni = {
            # A: crash long ago, flat-ish since. Last-4 momentum 96/100-1 = -4%
            #    but SMA8 = (3*50 + 100+99+98+97+96)/8 = 80 < 96 -> ABOVE its MA.
            "A-USD": candles([50, 50, 50, 50, 100, 99, 98, 97, 96]),
            # B: same shape, much worse: -40%.
            "B-USD": candles([50, 50, 50, 50, 100, 90, 80, 70, 60]),
        }
        strat = _strategy(uni)
        self.assertAlmostEqual(strat._momentum["A-USD"], -0.04)
        self.assertAlmostEqual(strat._momentum["B-USD"], -0.40)

        sig_a = strat.generate_signal("A-USD", uni["A-USD"])
        # Above trend MA (96 > 80), leads the ranking — and still cash.
        self.assertGreater(96, sig_a.indicators["trend_ma"])
        self.assertEqual(sig_a.indicators["leader"], "A-USD")
        self.assertEqual(sig_a.action, SELL)
        self.assertIn("non-positive", sig_a.reasons[0])

        sig_b = strat.generate_signal("B-USD", uni["B-USD"])
        self.assertEqual(sig_b.action, SELL)

    def test_leader_momentum_exactly_zero_is_cash(self):
        # Strict > 0: a flat leader (0.0%) is not strength.
        uni = {
            "A-USD": candles([50, 50, 50, 50, 100, 99, 98, 99, 100]),  # 100/100-1 = 0
            "B-USD": candles([50, 50, 50, 50, 100, 90, 80, 70, 60]),
        }
        strat = _strategy(uni)
        self.assertAlmostEqual(strat._momentum["A-USD"], 0.0)
        sig = strat.generate_signal("A-USD", uni["A-USD"])
        self.assertEqual(sig.action, SELL)
        self.assertIn("non-positive", sig.reasons[0])

    def test_exact_momentum_tie_breaks_to_first_configured_product(self):
        seq = [100, 100, 100, 100, 100, 105, 110, 115, 120]
        uni = {"A-USD": candles(seq), "B-USD": candles(list(seq))}
        strat = _strategy(uni)
        self.assertEqual(
            strat.generate_signal("A-USD", uni["A-USD"]).action, BUY
        )
        self.assertEqual(
            strat.generate_signal("B-USD", uni["B-USD"]).action, SELL
        )

    def test_without_prepare_it_holds_instead_of_degenerating(self):
        strat = make_strategy("momentum_rotation", _cfg())
        seq = candles([100, 100, 100, 100, 100, 105, 110, 115, 120])
        sig = strat.generate_signal("A-USD", seq)
        self.assertEqual(sig.action, HOLD)
        self.assertIn("full market snapshot", sig.reasons[0])

    def test_min_candles_boundary(self):
        strat = make_strategy("momentum_rotation", _cfg())
        n = strat.min_candles()
        self.assertEqual(n, 8)  # max(lookback 4 + 1, trend 8, atr 3 + 1)
        seq = candles([100.0 + i for i in range(n)])
        strat.prepare({"A-USD": seq})
        sig = strat.generate_signal("A-USD", seq)
        self.assertFalse(sig.reasons[0].startswith("Not enough data"))
        short = seq[:-1]
        strat.prepare({"A-USD": short})
        sig = strat.generate_signal("A-USD", short)
        self.assertTrue(sig.reasons[0].startswith("Not enough data"))


class MultiProductFakeMarketData:
    def __init__(self, by_product):
        self._by_product = by_product

    def get_candles(self, product_id):
        return self._by_product[product_id]


class RotationThroughEngine(unittest.TestCase):
    """End-to-end tick: the engine's prepare() hook feeds the whole universe to
    the strategy, and one tick buys the leader and nothing else."""

    def test_tick_buys_only_the_momentum_leader(self):
        span = 86_400
        # Old enough that the last bar has settled (closed_candles keeps it).
        import time
        start = time.time() - 20 * span
        mk = lambda closes: [
            {"time": start + i * span, "open": c, "high": c + 1, "low": c - 1, "close": c}
            for i, c in enumerate(closes)
        ]
        uni = {
            "A-USD": mk([100, 100, 100, 100, 100, 105, 110, 115, 120]),
            "B-USD": mk([100, 100, 100, 100, 100, 102, 105, 108, 110]),
        }
        eng = make_engine(
            starting_cash=10_000,
            products=["A-USD", "B-USD"],
            candle_granularity="ONE_DAY",
        )
        eng.storage = RecordingStorage()
        eng.market_data = MultiProductFakeMarketData(uni)
        eng.strategy = make_strategy("momentum_rotation", _cfg())

        executed = eng.tick()

        self.assertEqual([t.product_id for t in executed], ["A-USD"])
        self.assertEqual(executed[0].side, BUY)
        self.assertGreater(eng.portfolio.position("A-USD").quantity, 0)
        self.assertEqual(eng.portfolio.position("B-USD").quantity, 0)

    def test_leadership_flip_rotates_the_position(self):
        span = 86_400
        import time
        start = time.time() - 30 * span
        mk = lambda closes: [
            {"time": start + i * span, "open": c, "high": c + 1, "low": c - 1, "close": c}
            for i, c in enumerate(closes)
        ]
        eng = make_engine(
            starting_cash=10_000,
            products=["A-USD", "B-USD"],
            candle_granularity="ONE_DAY",
            # The A position's exit must come from the rotation SELL, not an
            # ATR stop/target — park the protective exits out of the way. With
            # a 1000-ATR stop, 1% risk sizing would round to dust, so size to
            # the equity cap instead (same shape as the live regime account).
            stop_loss_atr_mult=1000.0,
            take_profit_atr_mult=1000.0,
            trailing_stop=False,
            risk_per_trade_pct=0.95,
        )
        eng.storage = RecordingStorage()
        eng.strategy = make_strategy("momentum_rotation", _cfg())

        # Tick 1: A leads -> long A.
        uni1 = {
            "A-USD": mk([100, 100, 100, 100, 100, 105, 110, 115, 120]),
            "B-USD": mk([100, 100, 100, 100, 100, 102, 105, 108, 110]),
        }
        eng.market_data = MultiProductFakeMarketData(uni1)
        eng.tick()
        self.assertGreater(eng.portfolio.position("A-USD").quantity, 0)

        # Tick 2 (next bar): A stalls (momentum +9.5%), B surges (+27%);
        # both stay above their trend MAs. A gets SELL (closed), B gets BUY.
        uni2 = {
            "A-USD": mk([100, 100, 100, 100, 100, 105, 110, 115, 120, 115]),
            "B-USD": mk([100, 100, 100, 100, 100, 102, 105, 108, 110, 140]),
        }
        eng.market_data = MultiProductFakeMarketData(uni2)
        executed = eng.tick()

        sides = [(t.product_id, t.side) for t in executed]
        self.assertIn(("A-USD", SELL), sides)
        self.assertIn(("B-USD", BUY), sides)
        self.assertEqual(eng.portfolio.position("A-USD").quantity, 0)
        self.assertGreater(eng.portfolio.position("B-USD").quantity, 0)


if __name__ == "__main__":
    unittest.main()
