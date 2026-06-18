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


def test_realized_pnl_includes_buy_side_fee():
    # Round trip with a 1% fee on both legs. Buy 10 @ 100 (fee 10), sell 10 @ 120
    # (fee 12). Realized P&L must net BOTH fees: 200 gross - 10 - 12 = 178.
    p = Portfolio(starting_cash=10_000, fee_rate=0.01)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    p.execute(SELL, "BTC-USD", price=120.0, quantity=10)
    assert p.realized_pnl() == pytest.approx(178.0)
    # And realized P&L must reconcile exactly with the cash-based result once flat.
    assert p.realized_pnl() == pytest.approx(p.cash - p.starting_cash)


def test_unrealized_pnl_reflects_buy_fee():
    # Right after buying, the position is underwater by the entry fee even at a
    # flat price — equity (cash + market value) must equal starting cash minus fee.
    p = Portfolio(starting_cash=10_000, fee_rate=0.01)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)
    prices = {"BTC-USD": 100.0}
    assert p.unrealized_pnl(prices) == pytest.approx(-10.0)
    assert p.total_equity(prices) == pytest.approx(9_990.0)


def test_partial_sell_attributes_proportional_buy_fee():
    p = Portfolio(starting_cash=10_000, fee_rate=0.01)
    p.execute(BUY, "BTC-USD", price=100.0, quantity=10)  # entry fee 10
    p.execute(SELL, "BTC-USD", price=100.0, quantity=5)  # sell fee 5, half entry fee
    # Flat-price half exit: 0 gross - 5 sell fee - 5 (half of 10 entry fee) = -10.
    assert p.realized_pnl() == pytest.approx(-10.0)
    assert p.position("BTC-USD").entry_fees == pytest.approx(5.0)


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
