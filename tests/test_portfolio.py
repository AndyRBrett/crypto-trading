import pytest

from bot.portfolio import (
    InsufficientFunds,
    InsufficientPosition,
    Portfolio,
)
from bot.strategy import BUY, SELL


def test_buy_reduces_cash_and_opens_position():
    p = Portfolio(starting_cash=10_000, fee_rate=0.0)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    assert p.cash == 9_000
    pos = p.position("BTC-USD")
    assert pos.quantity == 10
    assert pos.avg_price == 100.0


def test_buy_applies_fee():
    p = Portfolio(starting_cash=10_000, fee_rate=0.01)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    # 1000 notional + 10 fee
    assert p.cash == pytest.approx(8_990)


def test_sell_realizes_pnl():
    p = Portfolio(starting_cash=10_000, fee_rate=0.0)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    p.execute(SELL, "BTC-USD", price=120.0, quantity=10)
    assert p.position("BTC-USD").quantity == 0
    assert p.realized_pnl() == pytest.approx(200.0)
    assert p.cash == pytest.approx(10_200)


def test_average_cost_basis_on_multiple_buys():
    p = Portfolio(starting_cash=10_000, fee_rate=0.0)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    p.execute(BUY, "BTC-USD", price=200.0, quantity=10)
    assert p.position("BTC-USD").avg_price == pytest.approx(150.0)


def test_insufficient_funds_raises():
    p = Portfolio(starting_cash=100, fee_rate=0.0)
    with pytest.raises(InsufficientFunds):
        p.execute(BUY, "BTC-USD", price=100.0, quantity=10)


def test_insufficient_position_raises():
    p = Portfolio(starting_cash=10_000, fee_rate=0.0)
    with pytest.raises(InsufficientPosition):
        p.execute(SELL, "BTC-USD", price=100.0, quantity=1)


def test_equity_and_unrealized_pnl():
    p = Portfolio(starting_cash=10_000, fee_rate=0.0)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    prices = {"BTC-USD": 150.0}
    assert p.market_value(prices) == 1_500
    assert p.total_equity(prices) == pytest.approx(10_500)
    assert p.unrealized_pnl(prices) == pytest.approx(500)


def test_from_trades_replays_state():
    p = Portfolio(starting_cash=10_000, fee_rate=0.001)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    p.execute(SELL, "BTC-USD", price=110.0, quantity=5)

    rebuilt = Portfolio.from_trades(10_000, 0.001, p.trades)
    assert rebuilt.cash == pytest.approx(p.cash)
    assert rebuilt.position("BTC-USD").quantity == pytest.approx(
        p.position("BTC-USD").quantity
    )
