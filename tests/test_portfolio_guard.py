"""Cross-account PortfolioGuard: exposure snapshot + opt-in entry veto.

Never touches exits; disabled guard (the default) approves everything, so
wiring it into the Runner changes nothing until portfolio_guard_enabled is set.
"""

import time

from bot.config import Config
from bot.engine import ACTED, PORTFOLIO_EXPOSURE
from bot.portfolio import Portfolio
from bot.portfolio_guard import PortfolioGuard
from bot.strategy import BUY, SELL, Signal

from tests.test_engine import FakeStorage, make_engine


class StubEngine:
    """Duck-typed account for the guard: a portfolio + last known prices."""

    def __init__(self, portfolio, last_prices=None):
        self.portfolio = portfolio
        self.last_prices = last_prices or {}


def _cfg(enabled=False, cap=1.5):
    cfg = Config()
    cfg.portfolio_guard_enabled = enabled
    cfg.max_gross_exposure_pct = cap
    return cfg


def test_snapshot_sums_long_short_and_equity_across_accounts():
    long_p = Portfolio(10_000, 0.0)
    long_p.execute(BUY, "BTC-USD", 100.0, 10.0, timestamp=1)   # $1200 long @ 120
    short_p = Portfolio(10_000, 0.0)
    short_p.execute(SELL, "ETH-USD", 50.0, 4.0, timestamp=1)   # $180 short @ 45

    guard = PortfolioGuard(_cfg())
    guard.register(StubEngine(long_p, {"BTC-USD": 120.0}))
    guard.register(StubEngine(short_p, {"ETH-USD": 45.0}))
    snap = guard.snapshot()

    assert snap["gross_long"] == 1200.0
    assert snap["gross_short"] == 180.0
    assert snap["gross"] == 1380.0
    assert snap["net_exposure"] == 1020.0
    assert snap["by_asset"] == {"BTC-USD": 1200.0, "ETH-USD": -180.0}
    # equity: (9000 cash + 1200) + (10200 cash - 180)
    assert snap["equity"] == 10_200.0 + 10_020.0


def test_disabled_guard_approves_everything():
    p = Portfolio(1_000, 0.0)
    p.execute(BUY, "BTC-USD", 100.0, 9.0, timestamp=1)  # 90% of equity gross
    guard = PortfolioGuard(_cfg(enabled=False, cap=0.1))  # cap far exceeded
    guard.register(StubEngine(p, {"BTC-USD": 100.0}))
    ok, why = guard.allows_entry(1_000_000.0)
    assert ok and why == ""


def test_enabled_guard_vetoes_entry_over_cap_and_allows_under():
    p = Portfolio(10_000, 0.0)
    p.execute(BUY, "BTC-USD", 100.0, 50.0, timestamp=1)  # $5000 gross, equity 10k
    guard = PortfolioGuard(_cfg(enabled=True, cap=0.6))  # cap = $6000
    guard.register(StubEngine(p, {"BTC-USD": 100.0}))

    ok, why = guard.allows_entry(500.0)   # 5000 + 500 <= 6000
    assert ok
    ok, why = guard.allows_entry(1500.0)  # 5000 + 1500 > 6000
    assert not ok
    assert "exceed the cap" in why


def test_engine_entry_vetoed_but_exits_untouched():
    """End-to-end through Engine._manage: with the guard enabled and the cap
    already used up by ANOTHER account, a new BUY is vetoed with the
    portfolio_exposure reject code — but a protective-stop exit on an existing
    position executes normally (the guard must never block risk reduction)."""
    # Another account's book consumes the whole cap.
    other = Portfolio(10_000, 0.0)
    other.execute(BUY, "BTC-USD", 100.0, 100.0, timestamp=1)  # $10k gross

    eng = make_engine(starting_cash=10_000, products=["ETH-USD"])
    eng.storage = FakeStorage()
    guard = PortfolioGuard(_cfg(enabled=True, cap=0.5))  # cap = 0.5 * ~20k = ~10k
    guard.register(StubEngine(other, {"BTC-USD": 100.0}))
    guard.register(eng)
    eng.portfolio_guard = guard

    # New entry: gross $10k + new $3k > $10k cap -> vetoed, nothing opened.
    buy = Signal(product_id="ETH-USD", action=BUY, price=100.0, indicators={"atr": 5.0})
    trade, code = eng._manage(buy, 100.0, [], {"ETH-USD": 100.0})
    assert trade is None and code == PORTFOLIO_EXPOSURE
    assert eng.portfolio.position("ETH-USD").quantity == 0
    # The other account's position is untouched by the veto.
    assert other.position("BTC-USD").quantity == 100.0

    # Exits are never consulted: hold a position, stay over cap, and let the
    # stop fire — the close must execute.
    eng.portfolio.execute(BUY, "ETH-USD", 100.0, 5.0, timestamp=time.time() - 60)
    hold = Signal(product_id="ETH-USD", action="HOLD", price=80.0, indicators={"atr": 5.0})
    candles = [{"time": time.time(), "high": 101.0, "low": 79.0}]
    trade, code = eng._manage(hold, 80.0, candles, {"ETH-USD": 80.0})
    assert code == ACTED
    assert trade is not None and trade.side == SELL  # stop-loss close went through
    assert eng.portfolio.position("ETH-USD").quantity == 0


def test_engine_without_guard_behaves_as_before():
    eng = make_engine(starting_cash=10_000, products=["ETH-USD"])
    eng.storage = FakeStorage()
    assert eng.portfolio_guard is None
    buy = Signal(product_id="ETH-USD", action=BUY, price=100.0, indicators={"atr": 5.0})
    trade, code = eng._manage(buy, 100.0, [], {"ETH-USD": 100.0})
    assert code == ACTED and trade is not None


import math
import pytest


def _history(returns, start=100, end=None):
    end = int(time.time() // 3600) * 3600 - 3600 if end is None else end
    prices = [start]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    return [{'time': end - (len(prices) - 1 - i) * 3600, 'close': p}
            for i, p in enumerate(prices)]


def _correlated_guard(returns_b=None):
    cfg = Config(correlation_guard_enabled=True, correlation_min_samples=4,
                 correlation_lookback=8, max_asset_exposure_pct=0.6,
                 max_correlated_exposure_pct=0.65)
    guard = PortfolioGuard(cfg)
    p = Portfolio(10000, 0)
    p.execute(BUY, 'BTC-USD', 100, 40, timestamp=1)
    guard.register(StubEngine(p, {'BTC-USD': 100}))
    a = [.01, -.01, .01, -.01] * 2
    b = a if returns_b is None else returns_b
    guard.prepare({'BTC-USD': _history(a), 'ETH-USD': _history(b)}, 3600)
    return guard


def test_correlated_entry_blocked_but_diversified_entry_allowed():
    guard = _correlated_guard()
    assert guard.allows_entry(3000, {'BTC-USD': 100}, product_id='ETH-USD')[0] is False
    guard = _correlated_guard([.01, .01, -.01, -.01] * 2)
    assert guard.allows_entry(3000, {'BTC-USD': 100}, product_id='ETH-USD')[0] is True
    risk = guard._risk({'BTC-USD': 4000, 'ETH-USD': 3000}, 10000)
    assert risk['effective_beta'] == pytest.approx(0.5)
    assert len(risk['clusters']) == 2


def test_missing_constant_stale_and_misaligned_data_are_conservative():
    guard = _correlated_guard()
    a = _history([.01, -.01] * 4)
    for b in ([], _history([0] * 8), _history([.01, -.01] * 4, end=100000),
              [{**c, 'time': c['time'] + 1} for c in a]):
        guard.prepare({'BTC-USD': a, 'ETH-USD': b}, 3600)
        rho, _, assumed = guard._correlation('BTC-USD', 'ETH-USD')
        assert rho == 1 and assumed
        assert not guard.allows_entry(3000, {'BTC-USD': 100}, product_id='ETH-USD')[0]
    # New failed fetch clears previously useful history.
    guard.prepare({}, 3600)
    assert guard._returns == {}


def test_opposite_accounts_do_not_cancel_concentration():
    guard = _correlated_guard()
    short = Portfolio(10000, 0)
    short.execute(SELL, 'BTC-USD', 100, 40, timestamp=1)
    guard.register(StubEngine(short, {'BTC-USD': 100}))
    snap = guard.snapshot({'BTC-USD': 100})
    assert snap['by_asset']['BTC-USD'] == 0
    assert snap['gross_by_asset']['BTC-USD'] == 8000
    guard.asset_cap = 0.4
    ok, reason = guard.allows_entry(1, {'BTC-USD': 100}, product_id='BTC-USD')
    assert not ok and 'concentration' in reason


def test_negative_correlation_has_no_hedge_credit():
    guard = _correlated_guard([-.01, .01] * 4)
    risk = guard._risk({'BTC-USD': 4000, 'ETH-USD': 3000}, 10000)
    assert risk['effective_beta'] == pytest.approx(.7)
    assert risk['clusters'][0]['assets'] == ['BTC-USD', 'ETH-USD']
    assert risk['pairs'][0]['correlation'] == pytest.approx(-1)


def test_new_fills_consume_capacity_before_next_entry():
    guard = _correlated_guard()
    guard.correlated_cap = .8
    assert guard.allows_entry(3000, {'BTC-USD': 100}, product_id='ETH-USD')[0]
    guard._engines[0].portfolio.execute(BUY, 'ETH-USD', 100, 30, timestamp=2)
    assert not guard.allows_entry(2000, {'BTC-USD': 100, 'ETH-USD': 100}, product_id='SOL-USD')[0]


def test_engine_short_entry_veto_and_cover_allowed():
    eng = make_engine(starting_cash=10000, products=['ETH-USD'], allow_short=True)
    eng.storage = FakeStorage()
    guard = _correlated_guard()
    guard.correlated_cap = .1
    guard.register(eng)
    eng.portfolio_guard = guard
    sell = Signal('ETH-USD', SELL, 100, indicators={'atr': 5})
    trade, code = eng._manage(sell, 100, [], {'ETH-USD': 100})
    assert trade is None and code == PORTFOLIO_EXPOSURE
    eng.portfolio.execute(SELL, 'ETH-USD', 100, 1, timestamp=1)
    buy = Signal('ETH-USD', BUY, 100, indicators={'atr': 5}, reasons=['cover'])
    trade, code = eng._manage(buy, 100, [], {'ETH-USD': 100})
    assert trade and code == ACTED


@pytest.mark.parametrize('overrides', [
    {'correlation_min_samples': 1}, {'correlation_lookback': 1.5},
    {'correlation_min_samples': 61}, {'max_asset_exposure_pct': float('nan')},
    {'max_correlated_exposure_pct': -1}, {'correlation_cluster_threshold': 1.1},
])
def test_invalid_correlation_config_rejected(overrides):
    with pytest.raises(ValueError):
        PortfolioGuard(Config(**overrides))
