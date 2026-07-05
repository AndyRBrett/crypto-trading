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
