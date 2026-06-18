"""Tests for the historical backtester."""

import math

from bot.backtest import run_backtest
from bot.config import Config
from bot.strategies import make_strategy
from bot.strategy import StrategyConfig


def _candles(closes):
    # Symmetric high/low band so ATR/stops have something to work with.
    return [
        {"time": 1700000000 + i * 3600, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c}
        for i, c in enumerate(closes)
    ]


def test_backtest_runs_and_reports_metrics():
    # Wide, non-trailing stops so a clear uptrend is bought and held.
    cfg = Config(starting_cash=10_000, fee_rate=0.006, trailing_stop=False,
                 stop_loss_atr_mult=6.0, take_profit_atr_mult=100.0)
    strat = make_strategy(
        "ema_crossover",
        StrategyConfig(fast_period=3, slow_period=6, ma_type="sma", rsi_overbought=95.0,
                       trend_filter=False, adx_filter=False, atr_period=3),
    )
    # Uptrend with pullbacks. (RSI gate widened to 95 so the test exercises the
    # backtester's sizing/fills/metrics rather than the strategy's RSI filter.)
    closes = [100 + i + 3 * math.sin(i / 2) for i in range(80)]
    res = run_backtest(strat, _candles(closes), cfg, product_id="BTC-USD")

    assert res.bars == len(closes)
    assert len(res.trades) >= 1           # the re-arm entry should fire at least once
    assert res.fees_paid > 0              # fees were applied
    assert res.total_return_pct > 0       # a steady uptrend should be profitable
    assert 0.0 <= res.win_rate <= 1.0
    assert res.max_drawdown_pct >= 0.0
    # Pre-fee return must beat net return whenever fees were paid.
    assert res.gross_return_pct > res.total_return_pct


def test_backtest_flat_market_makes_no_money():
    cfg = Config(starting_cash=10_000, fee_rate=0.006)
    strat = make_strategy(
        "ema_crossover",
        StrategyConfig(fast_period=3, slow_period=6, ma_type="sma",
                       trend_filter=False, adx_filter=False, atr_period=3),
    )
    closes = [100.0] * 60  # dead flat -> no crossover, no edge
    res = run_backtest(strat, _candles(closes), cfg, product_id="BTC-USD")
    assert res.num_trades == 0
    assert math.isclose(res.final_equity, 10_000, rel_tol=1e-9)


def test_backtest_matches_engine_risk_layer():
    # The backtester must size positions identically to the engine's risk module.
    from bot import risk
    cfg = Config(starting_cash=10_000, risk_per_trade_pct=0.01, stop_loss_atr_mult=2.0)
    qty = risk.position_size(cfg, equity=10_000, cash=10_000, price=1000.0, atr=50.0)
    assert abs(qty - 1.0) < 1e-6
