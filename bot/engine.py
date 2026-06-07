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
import time

from .config import Config
from .explain import Explainer
from .market_data import MarketData
from .portfolio import InsufficientFunds, InsufficientPosition, Portfolio
from .publish import Publisher
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
        publisher: Publisher | None = None,
    ):
        self.config = config
        self.market_data = market_data or MarketData(config)
        self.storage = storage or Storage(config.db_path)
        self.strategy = Strategy(config.strategy)
        self.explainer = explainer or Explainer(config)
        self.analyzer = sentiment_analyzer
        if self.analyzer is None and config.sentiment_enabled:
            self.analyzer = SentimentAnalyzer(config)
        self.publisher = publisher or Publisher(config)

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
            # Record every tick's decision (including HOLDs) as an activity log.
            try:
                self.storage.save_signal(
                    time.time(), product_id, signal.action, price,
                    signal.reasons[0] if signal.reasons else "",
                )
            except Exception as exc:  # never let logging break a tick
                log.warning("could not record activity for %s: %s", product_id, exc)

            trade = self._manage(signal, price, candles, prices)
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
            if self.publisher.enabled:
                self.publisher.publish(self.config.dashboard_state_path)
        return executed

    def _manage(self, signal, price: float, candles: list, prices: dict):
        """Risk-managed action for one product.

        While holding: protective exits (stop / take-profit / trailing) take
        priority, then a strategy SELL. While flat: a strategy BUY, sized by
        volatility so each trade risks a fixed fraction of equity.
        """
        product_id = signal.product_id
        pos = self.portfolio.position(product_id)
        atr = signal.indicators.get("atr")

        if pos.quantity > 0:
            exit_reason = self._protective_exit(product_id, pos, price, atr, candles)
            if exit_reason is None and signal.action == SELL:
                exit_reason = "; ".join(signal.reasons)
            if exit_reason:
                return self._sell(product_id, price, pos.quantity, [exit_reason], signal.indicators)
            return None

        if signal.action == BUY:
            open_count = sum(1 for p in self.portfolio.positions.values() if p.quantity > 0)
            if open_count >= self.config.max_open_positions:
                log.info("%s: at max open positions (%d), skipping BUY", product_id, open_count)
                return None
            qty = self._position_size(price, atr, prices)
            if qty <= 0:
                log.info("%s: position size ~0 after risk limits, skipping BUY", product_id)
                return None
            return self._buy(product_id, price, qty, signal.reasons, signal.indicators)

        return None

    def _protective_exit(self, product_id, pos, price, atr, candles):
        """Return an exit reason if a stop/target/trailing level is breached."""
        cfg = self.config
        entry = pos.avg_price
        if atr and atr > 0:
            stop = entry - cfg.stop_loss_atr_mult * atr
            target = entry + cfg.take_profit_atr_mult * atr
            if cfg.trailing_stop:
                opened = self.portfolio.opened_at(product_id)
                highs = [
                    c["high"] for c in candles
                    if "high" in c and (opened is None or c.get("time", 0) >= opened)
                ]
                if highs:
                    stop = max(stop, max(highs) - cfg.stop_loss_atr_mult * atr)
        else:
            stop = entry * (1 - cfg.fallback_stop_pct)
            target = None

        if price <= stop:
            return (
                f"Stop-loss: price ${price:,.2f} hit stop ${stop:,.2f} "
                f"(entry ${entry:,.2f}) — cutting the loss / locking in gains."
            )
        if target is not None and price >= target:
            return (
                f"Take-profit: price ${price:,.2f} reached target ${target:,.2f} "
                f"(entry ${entry:,.2f})."
            )
        return None

    def _position_size(self, price, atr, prices):
        """Volatility-based size so the stop distance risks ~risk_per_trade_pct."""
        cfg = self.config
        if price <= 0:
            return 0.0
        equity = self.portfolio.cash + self.portfolio.market_value(prices)
        if equity <= 0:
            return 0.0
        stop_dist = cfg.stop_loss_atr_mult * atr if (atr and atr > 0) else price * cfg.fallback_stop_pct
        if stop_dist <= 0:
            return 0.0
        qty_by_risk = (equity * cfg.risk_per_trade_pct) / stop_dist
        qty_by_cap = (equity * cfg.max_position_pct) / price
        qty_by_cash = (self.portfolio.cash * 0.999) / (price * (1 + cfg.fee_rate))
        qty = min(qty_by_risk, qty_by_cap, qty_by_cash)
        if qty * price < 10:  # ignore dust trades
            return 0.0
        return qty

    def _buy(self, product_id, price, qty, reasons, indicators):
        try:
            trade = self.portfolio.execute(
                BUY, product_id, price, qty, reasons=reasons, indicators=indicators
            )
        except InsufficientFunds as exc:
            log.warning("%s: %s", product_id, exc)
            return None
        return self._finalize(trade, price)

    def _sell(self, product_id, price, qty, reasons, indicators):
        try:
            trade = self.portfolio.execute(
                SELL, product_id, price, qty, reasons=reasons, indicators=indicators
            )
        except InsufficientPosition as exc:
            log.warning("%s: %s", product_id, exc)
            return None
        return self._finalize(trade, price)

    def _finalize(self, trade, price):
        trade.explanation = self.explainer.explain(
            trade, self.portfolio, {trade.product_id: price}
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
