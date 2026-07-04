"""In-memory paper portfolio.

Tracks cash, open positions (with average cost basis), and a trade log.
No real orders are ever placed — this simulates fills at the supplied price
and applies a configurable taker fee so results stay honest.

Positions are *signed*: a positive quantity is a long, a negative quantity is a
short. A BUY always costs cash and a SELL always credits cash, regardless of
direction — so a SELL while flat opens a short (you receive the proceeds), and a
later BUY covers it. With ``equity = cash + quantity * price`` that signed
convention prices both directions correctly: as a short's price falls, its
(negative) market value rises toward zero and equity grows. Shorting is only
ever *initiated* by the engine when an account enables it; the portfolio itself
just executes the fills it's handed.
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
    # Signed: > 0 long, < 0 short, 0 flat.
    quantity: float = 0.0
    # The entry price (always a positive number, for either direction).
    avg_price: float = 0.0
    # Fees paid to open the currently-held quantity. Kept separate from
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
        # Signed net value: longs add, shorts subtract. With cash holding a
        # short's proceeds, ``cash + market_value`` is the correct equity for
        # either direction.
        total = 0.0
        for product_id, pos in self.positions.items():
            if pos.quantity != 0 and product_id in prices:
                total += pos.quantity * prices[product_id]
        return total

    def total_equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for product_id, pos in self.positions.items():
            if pos.quantity != 0 and product_id in prices:
                # Signed quantity makes one formula serve both directions:
                # for a short (quantity < 0) this is (avg_price - price) * |qty|.
                total += (prices[product_id] - pos.avg_price) * pos.quantity
                total -= pos.entry_fees  # the fee already paid to open
        return total

    def realized_pnl(self) -> float:
        return sum(t.realized_pnl for t in self.trades)

    def opened_at(self, product_id: str) -> float | None:
        """Timestamp the current open position was first opened.

        Used by the trailing stop to find the highest high (long) or lowest low
        (short) *since entry*. Magnitude-based so it works for either direction.
        Returns None if the position is currently flat.
        """
        qty = 0.0
        opened: float | None = None
        for t in sorted(self.trades, key=lambda x: x.timestamp):
            if t.product_id != product_id:
                continue
            was_flat = abs(qty) <= 1e-9
            qty += t.quantity if t.side == BUY else -t.quantity
            if was_flat and abs(qty) > 1e-9:
                opened = t.timestamp
            if abs(qty) <= 1e-9:
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

        pos = self.positions.get(product_id, Position())
        fee = price * quantity * self.fee_rate
        if side == BUY:
            if pos.quantity < 0:
                # Buying back a short (cover). We can't cover more than we're
                # short — the engine never flips in a single fill.
                if quantity > -pos.quantity + 1e-9:
                    raise InsufficientPosition(
                        f"Tried to cover {quantity} but only short {-pos.quantity}"
                    )
                # No funds check on a cover: it's paid for out of the proceeds
                # already booked to cash when the short was opened.
            elif price * quantity + fee > self.cash + 1e-9:
                # Opening/adding a long must fit in cash.
                raise InsufficientFunds(
                    f"Need {price * quantity + fee:.2f} but only {self.cash:.2f} cash available"
                )
        elif side == SELL:
            # Selling more than a long position is over-selling; a SELL while
            # flat or short instead opens/adds a short (allowed).
            if pos.quantity > 0 and quantity > pos.quantity + 1e-9:
                raise InsufficientPosition(
                    f"Tried to sell {quantity} but only hold {pos.quantity}"
                )
        else:
            raise ValueError(f"unknown side {side!r}")

        realized = self._apply(side, product_id, price, quantity, fee)
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

    def _apply(
        self, side: str, product_id: str, price: float, quantity: float, fee: float
    ) -> float:
        """Mutate cash + position for one fill; return realized P&L.

        Direction-agnostic and validation-free (``execute`` validates; replay
        from a trade log trusts the log). A BUY always debits cash and a SELL
        always credits it; whether that opens, adds to, or closes a position
        depends on the current sign. Realized P&L is attributed only to the
        closing leg, net of this fill's fee plus a proportional share of the
        entry fees — so it reconciles exactly with cash once flat.
        """
        pos = self.positions.setdefault(product_id, Position())
        realized = 0.0
        if side == BUY:
            self.cash -= price * quantity + fee
            if pos.quantity < 0:  # covering a short
                mag = -pos.quantity
                entry_fee_share = pos.entry_fees * (quantity / mag) if mag else 0.0
                realized = (pos.avg_price - price) * quantity - fee - entry_fee_share
                pos.entry_fees -= entry_fee_share
                pos.quantity += quantity  # toward zero
            else:  # opening / adding a long
                new_qty = pos.quantity + quantity
                pos.avg_price = (pos.avg_price * pos.quantity + price * quantity) / new_qty
                pos.quantity = new_qty
                pos.entry_fees += fee
        else:  # SELL
            self.cash += price * quantity - fee
            if pos.quantity > 0:  # closing a long
                entry_fee_share = (
                    pos.entry_fees * (quantity / pos.quantity) if pos.quantity else 0.0
                )
                realized = (price - pos.avg_price) * quantity - fee - entry_fee_share
                pos.entry_fees -= entry_fee_share
                pos.quantity -= quantity
            else:  # opening / adding a short
                mag = -pos.quantity
                new_mag = mag + quantity
                pos.avg_price = (pos.avg_price * mag + price * quantity) / new_mag
                pos.quantity = -new_mag
                pos.entry_fees += fee
        if abs(pos.quantity) <= 1e-9:
            pos.quantity = 0.0
            pos.avg_price = 0.0
            pos.entry_fees = 0.0
        return realized

    @classmethod
    def from_trades(
        cls, starting_cash: float, fee_rate: float, trades: list[Trade]
    ) -> "Portfolio":
        """Rebuild portfolio state by replaying a trade log (used on restart).

        Each fill's cash/position effect is re-applied using the stored fee (no
        recompute); the shared helper handles longs and shorts alike. The
        replay's ``realized_pnl`` overwrites the logged value on each trade:
        rows persisted before the fee-accounting fix (2026-06-18) carry P&L
        computed without the entry-fee share, and trusting them would mix two
        formulas in every realized-P&L total. The replay is the single source
        of truth, so totals reconcile exactly with cash once flat.
        """
        p = cls(starting_cash, fee_rate)
        for t in sorted(trades, key=lambda x: x.timestamp):
            t.realized_pnl = p._apply(t.side, t.product_id, t.price, t.quantity, t.fee)
            p.trades.append(t)
        return p


def closing_legs(trades: list[Trade]) -> list[Trade]:
    """The fills that reduced an open position toward zero — one per exit.

    A SELL closing a long or a BUY covering a short. Replaying the signed
    position makes this direction-agnostic (long-only reduces to "every SELL"),
    which is the correct way to count round trips / win rate now that shorts
    realize their P&L on BUY legs. Shared by the backtester and write_status.
    """
    exits: list[Trade] = []
    running: dict[str, float] = {}
    for t in sorted(trades, key=lambda x: x.timestamp):
        pos = running.get(t.product_id, 0.0)
        signed = t.quantity if t.side == BUY else -t.quantity
        if pos != 0 and (pos > 0) != (signed > 0):
            exits.append(t)
        running[t.product_id] = pos + signed
    return exits
