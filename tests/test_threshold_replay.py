"""Tests for the what-if threshold replay (bot/threshold_replay.py).

The point of the tool is trust in the counterfactual arithmetic, so these
tests hand-construct logged ticks with known indicator values and check the
exact hypothetical outcome: which thresholds capture which signals, when the
protective stops fire, and that P&L / alpha-vs-buy-hold come out to the
hand-computed numbers.
"""

import math

import pytest

from bot.config import Config
from bot.storage import Storage
from bot.strategy import StrategyConfig
from bot.threshold_replay import (
    LoggedTick,
    build_params,
    load_ticks,
    replay,
    sweep_param,
)

NOTIONAL = 1000.0


def _tick(ts, product, price, indicators, action="HOLD", outcome="hold",
          reject_code="no_signal"):
    return LoggedTick(
        timestamp=ts, product_id=product, action=action, price=price,
        outcome=outcome, reject_code=reject_code, indicators=indicators,
        thresholds={},
    )


def _risk_cfg(**overrides):
    """Engine-like risk config; trailing off by default so tests stay simple."""
    defaults = dict(
        fee_rate=0.006, stop_loss_atr_mult=2.0, take_profit_atr_mult=6.0,
        trailing_stop=False, fallback_stop_pct=0.08,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _mr_ticks():
    """RSI mean-reversion path: dips to 33 (oversold only if threshold >= 33),
    reverts through the overbought exit at 55, then drifts down."""
    seq = [
        (100.0, 45.0),  # neutral
        (90.0, 33.0),   # the rejected signal: oversold at threshold >= 33
        (97.0, 41.0),   # recovering
        (105.0, 56.0),  # >= 55: mean-reversion exit
        (98.0, 50.0),   # window ends lower than the exit
    ]
    return [
        _tick(1000.0 + i, "SOL-USD", price, {"rsi": rsi, "atr": 5.0})
        for i, (price, rsi) in enumerate(seq)
    ]


class TestMeanReversionSweep:
    def test_baseline_threshold_captures_nothing(self):
        out = replay(_mr_ticks(), "rsi_mean_reversion", StrategyConfig(),
                     _risk_cfg(), "rsi_mr_oversold", 30, notional=NOTIONAL)
        assert out.triggers == 0
        assert out.entries == 0
        assert out.strategy_pnl == 0.0
        assert out.alpha_pct == 0.0

    def test_relaxed_threshold_captures_the_rejected_signal(self):
        out = replay(_mr_ticks(), "rsi_mean_reversion", StrategyConfig(),
                     _risk_cfg(), "rsi_mr_oversold", 35, notional=NOTIONAL)
        assert out.triggers == 1
        assert out.entries == 1
        assert out.closed == 1
        assert out.wins == 1
        assert out.open_at_end == 0
        trade = out.trades[0]
        assert trade.entry_price == 90.0
        assert trade.exit_price == 105.0
        assert trade.exit_reason == "strategy_exit"
        # Hand-computed: qty = 1000/90; gross = qty*15; fees = 6 entry + qty*105*0.006 exit.
        qty = NOTIONAL / 90.0
        expected = qty * 15.0 - NOTIONAL * 0.006 - qty * 105.0 * 0.006
        assert out.strategy_pnl == pytest.approx(expected)

    def test_alpha_vs_buy_hold_uses_same_entry_and_final_price(self):
        out = replay(_mr_ticks(), "rsi_mean_reversion", StrategyConfig(),
                     _risk_cfg(), "rsi_mr_oversold", 35, notional=NOTIONAL)
        # Buy-hold: same qty from 90, held to the final tick at 98.
        qty = NOTIONAL / 90.0
        expected_bh = qty * 8.0 - NOTIONAL * 0.006 - qty * 98.0 * 0.006
        assert out.buy_hold_pnl == pytest.approx(expected_bh)
        assert out.deployed == NOTIONAL
        expected_alpha = (out.strategy_pnl - out.buy_hold_pnl) / NOTIONAL * 100
        assert out.alpha_pct == pytest.approx(expected_alpha)
        # The strategy sold the top; buy-hold rode back down — alpha is positive.
        assert out.alpha_pct > 0

    def test_triggers_counted_even_while_holding(self):
        ticks = [
            _tick(1.0, "SOL-USD", 100.0, {"rsi": 33.0, "atr": 5.0}),
            _tick(2.0, "SOL-USD", 99.0, {"rsi": 32.0, "atr": 5.0}),  # still oversold
            _tick(3.0, "SOL-USD", 101.0, {"rsi": 40.0, "atr": 5.0}),
        ]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(), _risk_cfg(),
                     "rsi_mr_oversold", 35, notional=NOTIONAL)
        assert out.triggers == 2  # both oversold ticks would have fired
        assert out.entries == 1  # but only one position at a time per product

    def test_sweep_returns_one_outcome_per_value(self):
        outs = sweep_param(_mr_ticks(), "rsi_mean_reversion", StrategyConfig(),
                           _risk_cfg(), "rsi_mr_oversold", [30, 35, 45])
        assert [o.value for o in outs] == [30, 35, 45]
        # 45 takes TWO entries: the first (at 100) stops out at 90 (2xATR stop),
        # then RSI 41 <= 45 re-enters at 97 — looser thresholds trade more.
        assert [o.entries for o in outs] == [0, 1, 2]


class TestProtectiveExits:
    def test_stop_loss_closes_the_position(self):
        ticks = [
            _tick(1.0, "BTC-USD", 100.0, {"rsi": 30.0, "atr": 5.0}),  # entry
            _tick(2.0, "BTC-USD", 60.0, {"rsi": 45.0, "atr": 5.0}),   # below 100-2*5=90
        ]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(), _risk_cfg(),
                     "rsi_mr_oversold", 30, notional=NOTIONAL)
        assert out.closed == 1
        assert out.wins == 0
        assert out.trades[0].exit_reason == "Stop-loss"
        qty = NOTIONAL / 100.0
        expected = qty * -40.0 - NOTIONAL * 0.006 - qty * 60.0 * 0.006
        assert out.strategy_pnl == pytest.approx(expected)

    def test_take_profit_closes_the_position(self):
        ticks = [
            _tick(1.0, "BTC-USD", 100.0, {"rsi": 30.0, "atr": 5.0}),  # entry
            _tick(2.0, "BTC-USD", 131.0, {"rsi": 50.0, "atr": 5.0}),  # above 100+6*5=130
        ]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(), _risk_cfg(),
                     "rsi_mr_oversold", 30, notional=NOTIONAL)
        assert out.closed == 1
        assert out.wins == 1
        assert out.trades[0].exit_reason == "Take-profit"

    def test_open_position_is_marked_at_the_final_price(self):
        ticks = [
            _tick(1.0, "BTC-USD", 100.0, {"rsi": 30.0, "atr": 5.0}),  # entry
            _tick(2.0, "BTC-USD", 104.0, {"rsi": 45.0, "atr": 5.0}),  # no exit
        ]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(), _risk_cfg(),
                     "rsi_mr_oversold", 30, notional=NOTIONAL)
        assert out.closed == 0
        assert out.open_at_end == 1
        assert out.trades[0].exit_reason == "end_of_data"
        qty = NOTIONAL / 100.0
        expected = qty * 4.0 - NOTIONAL * 0.006 - qty * 104.0 * 0.006
        assert out.strategy_pnl == pytest.approx(expected)
        # Marked-to-market strategy equals buy-hold exactly: zero alpha.
        assert out.alpha_pct == pytest.approx(0.0)


class TestDonchianBuffer:
    def _ticks(self):
        ind = {"donchian_upper": 100.0, "donchian_lower": 80.0, "atr": 2.0}
        return [
            _tick(1.0, "ETH-USD", 98.0, ind),   # 2% below the channel high
            _tick(2.0, "ETH-USD", 103.0, ind),  # would be a real breakout
            _tick(3.0, "ETH-USD", 104.0, ind),
        ]

    def test_strict_gate_misses_the_near_breakout(self):
        out = replay(self._ticks(), "donchian_breakout", StrategyConfig(),
                     _risk_cfg(), "entry_buffer_pct", 0, notional=NOTIONAL)
        assert out.entries == 1  # only the true breakout at 103
        assert out.trades[0].entry_price == 103.0

    def test_buffer_captures_the_near_breakout(self):
        out = replay(self._ticks(), "donchian_breakout", StrategyConfig(),
                     _risk_cfg(), "entry_buffer_pct", 3, notional=NOTIONAL)
        assert out.entries == 1
        assert out.trades[0].entry_price == 98.0  # entered on the near-miss tick


class TestEmaCrossoverGates:
    def _ind(self, adx, rsi=50.0, fast=105.0, slow=100.0, trend=90.0):
        return {"fast_sma": fast, "slow_sma": slow, "rsi": rsi,
                "trend_ma": trend, "adx": adx, "atr": 2.0}

    def test_adx_gate_blocks_and_admits(self):
        ticks = [_tick(1.0, "BTC-USD", 100.0, self._ind(adx=12.0))]
        cfg = StrategyConfig()
        blocked = replay(ticks, "ema_crossover", cfg, _risk_cfg(), "adx_min", 20)
        admitted = replay(ticks, "ema_crossover", cfg, _risk_cfg(), "adx_min", 10)
        assert blocked.entries == 0
        assert admitted.entries == 1

    def test_trend_buffer_admits_price_below_trend_ma(self):
        ind = self._ind(adx=25.0, trend=110.0)  # price 100 is ~9.1% below trend
        ticks = [_tick(1.0, "BTC-USD", 100.0, ind)]
        cfg = StrategyConfig()
        strict = replay(ticks, "ema_crossover", cfg, _risk_cfg(), "trend_buffer_pct", 0)
        relaxed = replay(ticks, "ema_crossover", cfg, _risk_cfg(), "trend_buffer_pct", 10)
        assert strict.entries == 0
        assert relaxed.entries == 1

    def test_exit_on_bearish_ma_gap(self):
        cfg = StrategyConfig()
        ticks = [
            _tick(1.0, "BTC-USD", 100.0, self._ind(adx=25.0)),
            _tick(2.0, "BTC-USD", 99.0, self._ind(adx=25.0, fast=95.0)),  # fast < slow
        ]
        out = replay(ticks, "ema_crossover", cfg, _risk_cfg(), "adx_min", 20)
        assert out.closed == 1
        assert out.trades[0].exit_reason == "strategy_exit"


class TestSentimentVeto:
    def test_bearish_sentiment_blocks_the_hypothetical_entry(self):
        ind = {"rsi": 25.0, "atr": 5.0, "sentiment_score": -0.5}
        ticks = [_tick(1.0, "SOL-USD", 100.0, ind)]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(), _risk_cfg(),
                     "rsi_mr_oversold", 30)
        assert out.triggers == 0
        assert out.entries == 0

    def test_regime_ignores_sentiment_by_design(self):
        ind = {"trend_ma": 90.0, "atr": 5.0, "sentiment_score": -0.9}
        ticks = [_tick(1.0, "BTC-USD", 100.0, ind)]
        out = replay(ticks, "regime", StrategyConfig(), _risk_cfg(),
                     "trend_buffer_pct", 0)
        assert out.entries == 1


class TestRegimeBuffer:
    def test_buffered_gate_is_symmetric_for_entry_and_exit(self):
        ind = {"trend_ma": 100.0, "atr": 5.0}
        ticks = [
            _tick(1.0, "ETH-USD", 90.0, ind),  # 10% below trend
            _tick(2.0, "ETH-USD", 91.0, ind),  # still below trend, above the buffer
            _tick(3.0, "ETH-USD", 82.0, ind),  # breaks the buffered gate -> exit
        ]
        out = replay(ticks, "regime", StrategyConfig(), _risk_cfg(take_profit_atr_mult=1000),
                     "trend_buffer_pct", 15, notional=NOTIONAL)
        assert out.entries == 1
        assert out.trades[0].entry_price == 90.0
        assert out.trades[0].exit_price == 82.0
        assert out.trades[0].exit_reason == "strategy_exit"


class TestMultiProduct:
    def test_products_are_independent_books(self):
        ticks = [
            _tick(1.0, "BTC-USD", 100.0, {"rsi": 30.0, "atr": 5.0}),
            _tick(1.0, "SOL-USD", 50.0, {"rsi": 30.0, "atr": 2.0}),
            _tick(2.0, "BTC-USD", 110.0, {"rsi": 60.0, "atr": 5.0}),
            _tick(2.0, "SOL-USD", 55.0, {"rsi": 60.0, "atr": 2.0}),
        ]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(), _risk_cfg(),
                     "rsi_mr_oversold", 30, notional=NOTIONAL)
        assert out.entries == 2
        assert out.closed == 2
        assert out.deployed == 2 * NOTIONAL
        assert {t.product_id for t in out.trades} == {"BTC-USD", "SOL-USD"}


class TestBuildParams:
    def test_defaults_come_from_strategy_config(self):
        cfg = StrategyConfig(rsi_overbought=85.0, adx_min=20.0)
        p = build_params(cfg)
        assert p["rsi_overbought"] == 85.0
        assert p["adx_min"] == 20.0
        assert p["trend_buffer_pct"] == 0.0  # virtual knob: strict by default

    def test_override_applies_last(self):
        p = build_params(StrategyConfig(), {"adx_min": 5.0})
        assert p["adx_min"] == 5.0

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="no replay rules"):
            replay([], "momentum_rotation", StrategyConfig(), _risk_cfg(),
                   "adx_min", 20)


class TestLoadTicks:
    def test_reads_feature_rows_and_skips_featureless_ones(self, tmp_path):
        db = str(tmp_path / "t.db")
        storage = Storage(db)
        storage.save_signal(1.0, "BTC-USD", "HOLD", 100.0, "warmup")  # features {}
        storage.save_signal(
            2.0, "BTC-USD", "HOLD", 101.0, "no edge",
            outcome="hold", reject_code="no_signal",
            features={"indicators": {"rsi": 42.0, "atr": 3.0},
                      "thresholds": {"rsi_to_oversold": 12.0}},
        )
        storage.close()
        ticks = load_ticks(db)
        assert len(ticks) == 1
        t = ticks[0]
        assert (t.product_id, t.price, t.reject_code) == ("BTC-USD", 101.0, "no_signal")
        assert t.indicators == {"rsi": 42.0, "atr": 3.0}
        assert t.thresholds == {"rsi_to_oversold": 12.0}

    def test_pre_features_store_yields_nothing(self, tmp_path):
        import sqlite3

        db = str(tmp_path / "old.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE signal_log (id INTEGER PRIMARY KEY, timestamp REAL, "
            "product_id TEXT, action TEXT, price REAL, reason TEXT)"
        )
        conn.execute(
            "INSERT INTO signal_log(timestamp, product_id, action, price, reason) "
            "VALUES (1.0, 'BTC-USD', 'HOLD', 100.0, 'legacy')"
        )
        conn.commit()
        conn.close()
        assert load_ticks(db) == []


class TestTrailingStop:
    def test_trailing_stop_locks_in_gains(self):
        # Entry 100, ATR 5, trailing on: price runs to 130 (stop ratchets to
        # 120), then falls to 118 — below the trailed stop but far above the
        # initial 90 stop and take-profit disabled by a huge multiple.
        ticks = [
            _tick(1.0, "BTC-USD", 100.0, {"rsi": 30.0, "atr": 5.0}),
            _tick(2.0, "BTC-USD", 130.0, {"rsi": 50.0, "atr": 5.0}),
            _tick(3.0, "BTC-USD", 118.0, {"rsi": 50.0, "atr": 5.0}),
        ]
        out = replay(ticks, "rsi_mean_reversion", StrategyConfig(),
                     _risk_cfg(trailing_stop=True, take_profit_atr_mult=1000),
                     "rsi_mr_oversold", 30, notional=NOTIONAL)
        assert out.closed == 1
        assert out.wins == 1
        assert out.trades[0].exit_reason == "Stop-loss"
        assert out.trades[0].exit_price == 118.0
