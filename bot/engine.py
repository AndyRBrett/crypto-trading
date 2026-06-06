"""The trading engine: one tick wires everything together.

For each product on every tick:
  1. Fetch candles from the market data backend.
  2. Generate a signal from the strategy.
  3. Decide whether to act (respecting current position + cash + sizing).
  4. Execute the paper trade against the portfolio.
  5. Ask Claude to explain it (with a deterministic fallback).
  6. Persist the trade + equity snapshot and export dashboard state.
"""

from __future__ import annotations

import logging

from .config import Config
from .explain import Explainer
from .market_data import MarketData
from .portfolio import InsufficientFunds, InsufficientPosition, Portfolio
from .sentiment import SentimentAnalyzer
from .storage import Storage
from .strategy import BUY, HOLD, SELL, Strategy

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        config: Config,
        market_data: MarketData | None = None,
        storage: Storage | None = None,
        explainer: Explainer | None = None,
        sentiment_analyzer: SentimentAnalyzer | None = None,
    ):
        self.config = config
        self.market_data = market_data or MarketData(config)
        self.storage = storage or Storage(config.db_path)
        self.strategy = Strategy(config.strategy)
        self.explainer = explainer or Explainer(config)
        self.analyzer = sentiment_analyzer
        if self.analyzer is None and config.sentiment_enabled:
            self.analyzer = SentimentAnalyzer(config)

        # Resume by replaying the persisted trade log.
        trades = self.storage.load_trades()
        self.portfolio = Portfolio.from_trades(
            config.starting_cash, config.fee_rate, trades
        )
        self.latest_signals: dict = {}
        if trades:
            log.info(
                "Resumed from %d trades. Cash=$%.2f", len(trades), self.portfolio.cash
            )

    def tick(self) -> list:
        """Run one decision cycle across all products. Returns executed trades."""
        executed = []
        prices: dict[str, float] = {}

        for product_id in self.config.products:
            try:
                candles = self.market_data.get_candles(product_id)
            except Exception as exc:
                log.error("Failed to fetch candles for %s: %s", product_id, exc)
                continue
            if not candles:
                log.warning("No candles for %s", product_id)
                continue

            sentiment = None
            if self.analyzer is not None:
                try:
                    sentiment = self.analyzer.analyze(product_id)
                except Exception as exc:
                    log.warning("sentiment analyze failed for %s: %s", product_id, exc)

            signal = self.strategy.generate_signal(
                product_id, candles, sentiment=sentiment
            )
            price = signal.price
            prices[product_id] = price
            self.latest_signals[product_id] = {
                "action": signal.action,
                "price": price,
                "strength": signal.strength,
                "reasons": signal.reasons,
                "indicators": signal.indicators,
                "sentiment": sentiment.to_dict() if sentiment else None,
            }
            log.info(
                "%s: %s @ $%.2f (%s)",
                product_id,
                signal.action,
                price,
                "; ".join(signal.reasons),
            )

            trade = self._maybe_trade(signal, price)
            if trade is not None:
                executed.append(trade)

        # Snapshot equity using fresh prices, then export dashboard state.
        if prices:
            self.storage.save_equity(
                self.portfolio.cash,
                self.portfolio.market_value(prices),
                self.portfolio.total_equity(prices),
            )
            self.storage.export_state(
                self.config.dashboard_state_path,
                self.config,
                self.portfolio,
                prices,
                self.latest_signals,
            )
        return executed

    def _maybe_trade(self, signal, price: float):
        product_id = signal.product_id
        pos = self.portfolio.position(product_id)

        if signal.action == BUY:
            if pos.quantity > 0:
                log.info("%s: already holding, skipping BUY", product_id)
                return None
            budget = self.portfolio.cash * self.config.buy_fraction
            # Reserve for the fee so the buy doesn't overshoot available cash.
            qty = budget / (price * (1 + self.config.fee_rate)) if price > 0 else 0
            if qty <= 0 or budget < 1:
                log.info("%s: insufficient cash to BUY", product_id)
                return None
            try:
                trade = self.portfolio.execute(
                    BUY, product_id, price, qty,
                    reasons=signal.reasons, indicators=signal.indicators,
                )
            except InsufficientFunds as exc:
                log.warning("%s: %s", product_id, exc)
                return None
        elif signal.action == SELL:
            if pos.quantity <= 0:
                return None
            try:
                trade = self.portfolio.execute(
                    SELL, product_id, price, pos.quantity,
                    reasons=signal.reasons, indicators=signal.indicators,
                )
            except InsufficientPosition as exc:
                log.warning("%s: %s", product_id, exc)
                return None
        else:  # HOLD
            return None

        trade.explanation = self.explainer.explain(
            trade, self.portfolio, {product_id: price}
        )
        self.storage.save_trade(trade)
        log.info("EXECUTED %s | %s", trade.side, trade.explanation)
        return trade

    def status(self) -> dict:
        prices = self.market_data.get_prices(self.config.products)
        equity = self.portfolio.total_equity(prices)
        return {
            "cash": self.portfolio.cash,
            "equity": equity,
            "starting_cash": self.portfolio.starting_cash,
            "total_return_pct": (equity / self.portfolio.starting_cash - 1) * 100,
            "realized_pnl": self.portfolio.realized_pnl(),
            "unrealized_pnl": self.portfolio.unrealized_pnl(prices),
            "positions": {
                pid: {
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "price": prices.get(pid),
                }
                for pid, p in self.portfolio.positions.items()
                if p.quantity > 0
            },
            "prices": prices,
            "num_trades": len(self.portfolio.trades),
        }

    def close(self) -> None:
        self.storage.close()
