"""Durable storage + dashboard export.

Trades and equity snapshots are persisted to SQLite so the bot can resume
after a restart by replaying its trade log. The dashboard reads a single
exported ``state.json`` — the bot is the only writer, the dashboard is a
read-only viewer.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .portfolio import Portfolio, Trade


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                product_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                fee REAL NOT NULL,
                cash_after REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                reasons TEXT NOT NULL,
                indicators TEXT NOT NULL,
                explanation TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                equity REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                product_id TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- activity log (every tick's decision, including HOLDs) --------------

    def save_signal(self, timestamp: float, product_id: str, action: str, price: float, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO signal_log(timestamp, product_id, action, price, reason) "
            "VALUES(?, ?, ?, ?, ?)",
            (timestamp, product_id, action, price, reason),
        )
        self.conn.commit()

    def load_activity(self, limit: int = 60) -> list[dict]:
        rows = self.conn.execute(
            "SELECT timestamp, product_id, action, price, reason "
            "FROM signal_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- trades ------------------------------------------------------------

    def save_trade(self, trade: Trade) -> None:
        self.conn.execute(
            """INSERT INTO trades(timestamp, product_id, side, price, quantity, fee,
                 cash_after, realized_pnl, reasons, indicators, explanation)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.timestamp,
                trade.product_id,
                trade.side,
                trade.price,
                trade.quantity,
                trade.fee,
                trade.cash_after,
                trade.realized_pnl,
                json.dumps(trade.reasons),
                json.dumps(trade.indicators),
                trade.explanation,
            ),
        )
        self.conn.commit()

    def load_trades(self) -> list[Trade]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY timestamp").fetchall()
        trades = []
        for r in rows:
            trades.append(
                Trade(
                    timestamp=r["timestamp"],
                    product_id=r["product_id"],
                    side=r["side"],
                    price=r["price"],
                    quantity=r["quantity"],
                    fee=r["fee"],
                    cash_after=r["cash_after"],
                    realized_pnl=r["realized_pnl"],
                    reasons=json.loads(r["reasons"]),
                    indicators=json.loads(r["indicators"]),
                    explanation=r["explanation"],
                )
            )
        return trades

    # -- equity ------------------------------------------------------------

    def save_equity(self, cash: float, market_value: float, equity: float) -> None:
        self.conn.execute(
            "INSERT INTO equity(timestamp, cash, market_value, equity) VALUES(?,?,?,?)",
            (time.time(), cash, market_value, equity),
        )
        self.conn.commit()

    def load_equity_curve(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT timestamp, cash, market_value, equity FROM equity "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -- dashboard export --------------------------------------------------

    def export_state(
        self,
        path: str,
        config,
        portfolio: Portfolio,
        prices: dict[str, float],
        latest_signals: dict,
    ) -> None:
        positions = []
        for pid, pos in portfolio.positions.items():
            if pos.quantity <= 0:
                continue
            price = prices.get(pid, pos.avg_price)
            positions.append(
                {
                    "product_id": pid,
                    "quantity": pos.quantity,
                    "avg_price": pos.avg_price,
                    "price": price,
                    "value": pos.quantity * price,
                    "unrealized_pnl": (price - pos.avg_price) * pos.quantity,
                }
            )

        recent = self.load_trades()[-50:][::-1]
        equity = portfolio.total_equity(prices)
        state = {
            "updated_at": time.time(),
            "products": config.products,
            "starting_cash": portfolio.starting_cash,
            "cash": portfolio.cash,
            "market_value": portfolio.market_value(prices),
            "equity": equity,
            "total_return_pct": (
                (equity / portfolio.starting_cash - 1) * 100
                if portfolio.starting_cash
                else 0.0
            ),
            "realized_pnl": portfolio.realized_pnl(),
            "unrealized_pnl": portfolio.unrealized_pnl(prices),
            "prices": prices,
            "positions": positions,
            "latest_signals": latest_signals,
            "activity": self.load_activity(),
            "equity_curve": self.load_equity_curve(),
            "trades": [
                {
                    "timestamp": t.timestamp,
                    "product_id": t.product_id,
                    "side": t.side,
                    "price": t.price,
                    "quantity": t.quantity,
                    "notional": t.notional(),
                    "fee": t.fee,
                    "realized_pnl": t.realized_pnl,
                    "reasons": t.reasons,
                    "indicators": t.indicators,
                    "explanation": t.explanation,
                }
                for t in recent
            ],
        }
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(state, indent=2))

    def close(self) -> None:
        self.conn.close()
