"""In-memory paper portfolio.

Tracks cash, open positions (with average cost basis), and a trade log.
No real orders are ever placed — this simulates fills at the supplied price
and applies a configurable taker fee so results stay honest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .strategy import BUY, SELL


@dataclass
class Trade:
    timestamp: float
    product_id: str
    side: str  # BUY or SELL
    price: float
    quantity: float
    fee: float
    cash_after: float
    realized_pnl: float = 0.0  # only meaningful on SELLs
    reasons: list[str] = field(default_factory=list)
    indicators: dict = field(default_factory=dict)
    explanation: str = ""

    def notional(self) -> float:
        return self.price * self.quantity


@dataclass
class Position:
    quantity: float = 0.0
    avg_price: float = 0.0
    # Buy-side fees paid to open the currently-held quantity. Kept separate from
    # avg_price (so stops/targets still key off the clean entry price) but folded
    # into realized/unrealized P&L so fees are never silently dropped.
    entry_fees: float = 0.0


class InsufficientFunds(Exception):
    pass


class InsufficientPosition(Exception):
    pass


class Portfolio:
    def __init__(self, starting_cash: float, fee_rate: float = 0.006):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.fee_rate = fee_rate
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []

    # -- queries -----------------------------------------------------------

    def position(self, product_id: str) -> Position:
        return self.positions.get(product_id, Position())

    def market_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for product_id, pos in self.positions.items():
            if pos.quantity > 0 and product_id in prices:
                total += pos.quantity * prices[product_id]
        return total

    def total_equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for product_id, pos in self.positions.items():
            if pos.quantity > 0 and product_id in prices:
                total += (prices[product_id] - pos.avg_price) * pos.quantity
                total -= pos.entry_fees  # the buy-side fee already paid to open
        return total

    def realized_pnl(self) -> float:
        return sum(t.realized_pnl for t in self.trades)

    def opened_at(self, product_id: str) -> float | None:
        """Timestamp the current open position was first opened (0 -> long).

        Used by the trailing stop to find the highest high *since entry*.
        Returns None if the position is currently flat.
        """
        qty = 0.0
        opened: float | None = None
        for t in sorted(self.trades, key=lambda x: x.timestamp):
            if t.product_id != product_id:
                continue
            was_flat = qty <= 1e-9
            qty += t.quantity if t.side == BUY else -t.quantity
            if was_flat and qty > 1e-9:
                opened = t.timestamp
            if qty <= 1e-9:
                qty = 0.0
                opened = None
        return opened

    # -- mutations ---------------------------------------------------------

    def execute(
        self,
        side: str,
        product_id: str,
        price: float,
        quantity: float,
        timestamp: float | None = None,
        reasons: list[str] | None = None,
        indicators: dict | None = None,
    ) -> Trade:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        timestamp = timestamp if timestamp is not None else time.time()
        reasons = reasons or []
        indicators = indicators or {}

        if side == BUY:
            cost = price * quantity
            fee = cost * self.fee_rate
            if cost + fee > self.cash + 1e-9:
                raise InsufficientFunds(
                    f"Need {cost + fee:.2f} but only {self.cash:.2f} cash available"
                )
            self.cash -= cost + fee
            pos = self.positions.setdefault(product_id, Position())
            new_qty = pos.quantity + quantity
            pos.avg_price = (pos.avg_price * pos.quantity + price * quantity) / new_qty
            pos.quantity = new_qty
            pos.entry_fees += fee
            realized = 0.0
        elif side == SELL:
            pos = self.positions.get(product_id, Position())
            if quantity > pos.quantity + 1e-9:
                raise InsufficientPosition(
                    f"Tried to sell {quantity} but only hold {pos.quantity}"
                )
            proceeds = price * quantity
            fee = proceeds * self.fee_rate
            # Attribute a proportional share of the buy-side fees to the sold
            # quantity so realized P&L reflects the *round-trip* cost, not just
            # the sell fee. (Without this, realized_pnl overstates profit by the
            # entry fees and never reconciles with the cash balance.)
            entry_fee_share = (
                pos.entry_fees * (quantity / pos.quantity) if pos.quantity else 0.0
            )
            realized = (price - pos.avg_price) * quantity - fee - entry_fee_share
            self.cash += proceeds - fee
            pos.quantity -= quantity
            pos.entry_fees -= entry_fee_share
            if pos.quantity <= 1e-9:
                pos.quantity = 0.0
                pos.avg_price = 0.0
                pos.entry_fees = 0.0
        else:
            raise ValueError(f"unknown side {side!r}")

        trade = Trade(
            timestamp=timestamp,
            product_id=product_id,
            side=side,
            price=price,
            quantity=quantity,
            fee=fee,
            cash_after=self.cash,
            realized_pnl=realized,
            reasons=reasons,
            indicators=indicators,
        )
        self.trades.append(trade)
        return trade

    @classmethod
    def from_trades(
        cls, starting_cash: float, fee_rate: float, trades: list[Trade]
    ) -> "Portfolio":
        """Rebuild portfolio state by replaying a trade log (used on restart)."""
        p = cls(starting_cash, fee_rate)
        for t in sorted(trades, key=lambda x: x.timestamp):
            # Re-apply the cash/position effects without recomputing fees.
            if t.side == BUY:
                p.cash -= t.notional() + t.fee
                pos = p.positions.setdefault(t.product_id, Position())
                new_qty = pos.quantity + t.quantity
                pos.avg_price = (
                    pos.avg_price * pos.quantity + t.price * t.quantity
                ) / new_qty
                pos.quantity = new_qty
                pos.entry_fees += t.fee
            else:  # SELL
                pos = p.positions.setdefault(t.product_id, Position())
                p.cash += t.notional() - t.fee
                if pos.quantity:
                    pos.entry_fees -= pos.entry_fees * (t.quantity / pos.quantity)
                pos.quantity = max(0.0, pos.quantity - t.quantity)
                if pos.quantity <= 1e-9:
                    pos.quantity = 0.0
                    pos.avg_price = 0.0
                    pos.entry_fees = 0.0
            p.trades.append(t)
        return p
