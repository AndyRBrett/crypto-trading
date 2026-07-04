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
from .coordinate import Coordinator
from .explain import Explainer
from .market_data import MarketData, closed_candles
from .notifier import Notifier
from .portfolio import InsufficientFunds, InsufficientPosition, Portfolio
from .publish import Publisher
from . import risk
from .sentiment import SentimentAnalyzer
from .storage import Storage
from .strategies import make_strategy
from .strategy import BUY, HOLD, SELL

log = logging.getLogger(__name__)

# Why an evaluated signal did or didn't become a trade (issue #23). These stable
# enums land in signal_log.reject_code so the overseer can account for every
# evaluated signal — "4 of 6 didn't trade" becomes a breakdown of reasons rather
# than a silent gap. ACTED is the empty string so a filled signal carries no code.
ACTED = ""
NO_SIGNAL = "no_signal"            # strategy held while flat: no entry trigger
NO_POSITION = "no_position"        # strategy SELL while flat: nothing to sell
IN_POSITION = "in_position"        # holding; no protective exit or SELL fired
MAX_OPEN_POSITIONS = "max_open_positions"  # BUY blocked: at max concurrent positions
SIZE_ZERO = "size_zero"            # BUY sized to ~0 by risk limits / dust floor
INSUFFICIENT_BALANCE = "insufficient_balance"  # BUY rejected: not enough cash
# reject_codes that mean "we wanted to BUY but couldn't" vs. "no actionable signal".
_REJECTED_CODES = frozenset({MAX_OPEN_POSITIONS, SIZE_ZERO, INSUFFICIENT_BALANCE})


class Engine:
    def __init__(
        self,
        config: Config,
        market_data: MarketData | None = None,
        storage: Storage | None = None,
        explainer: Explainer | None = None,
        sentiment_analyzer: SentimentAnalyzer | None = None,
        publisher: Publisher | None = None,
        coordinator: Coordinator | None = None,
    ):
        self.config = config
        self.market_data = market_data or MarketData(config)
        self.coordinator = coordinator or Coordinator(config)
        # Pull the shared portfolio before opening the local DB (only when we
        # create the storage ourselves; injected storage in tests is left alone).
        if storage is None and self.coordinator.enabled:
            self.coordinator.pull_db(config.db_path)
        self.storage = storage or Storage(config.db_path)
        strategy_type = getattr(config, "strategy_type", None) or "ema_crossover"
        self.strategy = make_strategy(strategy_type, config.strategy)
        self.explainer = explainer or Explainer(config)
        self.analyzer = sentiment_analyzer
        if self.analyzer is None and config.sentiment_enabled:
            self.analyzer = SentimentAnalyzer(config)
            if not config.anthropic_api_key:
                log.warning(
                    "sentiment_enabled is set but ANTHROPIC_API_KEY is missing — "
                    "every sentiment score will be a neutral 0.0 until a key is provided."
                )
        self.publisher = publisher or Publisher(config)
        self.notifier = Notifier(config.push_subscription, config.vapid_private_key, config.vapid_claims_email)

        # Resume by replaying the persisted trade log.
        trades = self.storage.load_trades()
        self.portfolio = Portfolio.from_trades(
            config.starting_cash, config.fee_rate, trades
        )
        self.latest_signals: dict = {}
        # Last tick's market snapshot, surfaced for the multi-account Runner's
        # combined dashboard export.
        self.last_prices: dict = {}
        self.last_price_history: dict = {}
        if trades:
            log.info(
                "Resumed from %d trades. Cash=$%.2f", len(trades), self.portfolio.cash
            )

        # Peak equity is persisted in the meta table so a new portfolio
        # all-time high survives restarts and GitHub Actions ephemeral VMs.
        _peak = self.storage.get_meta("peak_equity")
        self._peak_equity: float | None = float(_peak) if _peak else None

    def tick(self) -> list:
        """Run one decision cycle across all products. Returns executed trades."""
        # Driver coordination: the cloud stands down while the laptop is active;
        # whoever runs refreshes the lease so the other side can see it.
        if self.coordinator.enabled:
            if self.config.driver_role == "cloud" and self.coordinator.laptop_active():
                log.info("Laptop driver is active; cloud standing down this run.")
                return []
            self.coordinator.claim_lease()

        executed = []
        prices: dict[str, float] = {}
        price_history: dict[str, list] = {}

        for product_id in self.config.products:
            try:
                candles = self.market_data.get_candles(product_id)
            except Exception as exc:
                log.error("Failed to fetch candles for %s: %s", product_id, exc)
                continue
            if not candles:
                log.warning("No candles for %s", product_id)
                continue

            # Recent OHLC for the dashboard's per-coin candlestick chart.
            price_history[product_id] = [
                {
                    "t": int(c["time"]),
                    "o": round(float(c["open"]), 2),
                    "h": round(float(c["high"]), 2),
                    "l": round(float(c["low"]), 2),
                    "c": round(float(c["close"]), 2),
                }
                for c in candles[-120:]
            ]

            sentiment = None
            if self.analyzer is not None:
                try:
                    sentiment = self.analyzer.analyze(product_id)
                    # Surface *why* the score is what it is — a 0.0 can mean
                    # "no key", "no relevant headlines", or a genuine neutral read,
                    # and the summary distinguishes them.
                    log.info(
                        "%s sentiment: %+.2f (%s, %d headlines) — %s",
                        product_id,
                        sentiment.score,
                        sentiment.label,
                        sentiment.headline_count,
                        sentiment.summary,
                    )
                except Exception as exc:
                    log.warning("sentiment analyze failed for %s: %s", product_id, exc)

            # Entries are evaluated only on *settled* candles: the exchange's
            # most recent bar is the still-forming current period, and ticking
            # every 15 min on hourly candles would otherwise re-detect the same
            # crossover on every tick off a moving target. Protective exits below
            # still use the live price, so stops react intra-candle as intended.
            signal_candles = closed_candles(candles, self.config.candle_granularity)
            signal = self.strategy.generate_signal(
                product_id, signal_candles, sentiment=sentiment
            )
            # Live price (the forming bar's latest close) drives sizing,
            # execution, equity, and the protective stops.
            price = float(candles[-1]["close"])
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

            trade, reject_code = self._manage(signal, price, candles, prices)
            if trade is not None:
                executed.append(trade)

            # Record every tick's decision (including HOLDs) as an activity log,
            # tagged with why it did or didn't trade and the realized slippage
            # between the signal price (last *closed* candle) and the live fill
            # price (issue #23). Slippage is meaningful only on acted signals.
            if trade is not None:
                outcome = "acted"
                slippage_bps = (
                    round((trade.price - signal.price) / signal.price * 1e4, 2)
                    if signal.price else None
                )
            else:
                outcome = "rejected" if reject_code in _REJECTED_CODES else "hold"
                slippage_bps = None
            # Snapshot the input features + distance to each decision threshold.
            # On a HOLD/no_signal this is the only record of *how close* the signal
            # came to firing — exactly what threshold tuning needs, since the
            # trade log only ever captures the signals that did fire.
            features = {
                "indicators": signal.indicators,
                "thresholds": signal.thresholds,
                "strength": signal.strength,
            }
            try:
                self.storage.save_signal(
                    time.time(), product_id, signal.action, price,
                    signal.reasons[0] if signal.reasons else "",
                    outcome=outcome, reject_code=reject_code, slippage_bps=slippage_bps,
                    features=features,
                )
            except Exception as exc:  # never let logging break a tick
                log.warning("could not record activity for %s: %s", product_id, exc)

        # Surface this tick's market snapshot for the Runner's combined export.
        self.last_prices = prices
        self.last_price_history = price_history

        # Snapshot equity using fresh prices, then export dashboard state.
        if prices:
            # A failed candle fetch leaves that product out of `prices`, and
            # market_value() silently values missing products at zero — an
            # equity snapshot taken then would record a false dip (corrupting
            # drawdown/Sharpe). Skip the snapshot unless every open position
            # was priced this tick; the state export below still runs.
            unpriced = [
                pid
                for pid, p in self.portfolio.positions.items()
                if p.quantity != 0 and pid not in prices
            ]
            if unpriced:
                log.warning(
                    "skipping equity snapshot: no fresh price for open position(s) %s",
                    unpriced,
                )
            else:
                current_equity = self.portfolio.total_equity(prices)
                self.storage.save_equity(
                    self.portfolio.cash,
                    self.portfolio.market_value(prices),
                    current_equity,
                )
                self._maybe_notify_new_high(current_equity)
            self.storage.export_state(
                self.config.dashboard_state_path,
                self.config,
                self.portfolio,
                prices,
                self.latest_signals,
                price_history,
            )
            if self.publisher.enabled:
                self.publisher.publish(self.config.dashboard_state_path)
            if self.coordinator.enabled:
                self.coordinator.push_db(self.config.db_path)
        return executed

    def _manage(self, signal, price: float, candles: list, prices: dict):
        """Risk-managed action for one product.

        While holding a long: protective exits (stop / take-profit / trailing)
        take priority, then a strategy SELL closes it. While holding a short:
        protective exits, then a strategy BUY covers it. While flat: a strategy
        BUY opens a long, and — when the account enables shorting — a strategy
        SELL opens a short. Sizes risk a fixed fraction of equity.

        Returns ``(trade_or_None, reject_code)`` where ``reject_code`` is a stable
        enum (``ACTED`` when a trade executed) recording why a signal didn't
        trade, so the activity log can explain every evaluated signal.
        """
        product_id = signal.product_id
        pos = self.portfolio.position(product_id)
        atr = signal.indicators.get("atr")

        if pos.quantity > 0:  # holding a long
            exit_reason = self._protective_exit(product_id, pos, price, atr, candles)
            if exit_reason is None and signal.action == SELL:
                exit_reason = "; ".join(signal.reasons)
            if exit_reason:
                trade = self._sell(product_id, price, pos.quantity, [exit_reason], signal.indicators)
                return trade, ACTED if trade else IN_POSITION
            return None, IN_POSITION

        if pos.quantity < 0:  # holding a short — cover on a stop or a BUY signal
            exit_reason = self._protective_exit(product_id, pos, price, atr, candles)
            if exit_reason is None and signal.action == BUY:
                exit_reason = "; ".join(signal.reasons)
            if exit_reason:
                trade = self._cover(product_id, price, -pos.quantity, [exit_reason], signal.indicators)
                return trade, ACTED if trade else IN_POSITION
            return None, IN_POSITION

        if signal.action == BUY:
            if self._at_max_positions():
                log.info("%s: at max open positions, skipping BUY", product_id)
                return None, MAX_OPEN_POSITIONS
            qty = self._position_size(price, atr, prices)
            if qty <= 0:
                log.info("%s: position size ~0 after risk limits, skipping BUY", product_id)
                return None, SIZE_ZERO
            trade = self._buy(product_id, price, qty, signal.reasons, signal.indicators)
            return trade, ACTED if trade else INSUFFICIENT_BALANCE

        if signal.action == SELL and getattr(self.config, "allow_short", False):
            if self._at_max_positions():
                log.info("%s: at max open positions, skipping SHORT", product_id)
                return None, MAX_OPEN_POSITIONS
            qty = self._position_size(price, atr, prices, direction="short")
            if qty <= 0:
                log.info("%s: short size ~0 after risk limits, skipping", product_id)
                return None, SIZE_ZERO
            trade = self._short(product_id, price, qty, signal.reasons, signal.indicators)
            return trade, ACTED if trade else INSUFFICIENT_BALANCE

        return None, NO_POSITION if signal.action == SELL else NO_SIGNAL

    def _at_max_positions(self) -> bool:
        """True once the portfolio holds the max concurrent positions (either
        direction counts toward the heat cap)."""
        open_count = sum(1 for p in self.portfolio.positions.values() if p.quantity != 0)
        return open_count >= self.config.max_open_positions

    def _protective_exit(self, product_id, pos, price, atr, candles):
        """Return an exit reason if a stop/target/trailing level is breached.

        Direction is read from the position's sign: a short trails the lowest low
        since entry and stops out *above* entry, the mirror of a long.
        """
        opened = self.portfolio.opened_at(product_id)
        if pos.quantity < 0:
            lows = [
                c["low"] for c in candles
                if "low" in c and (opened is None or c.get("time", 0) >= opened)
            ]
            return risk.protective_exit_reason(
                self.config, pos.avg_price, price, atr,
                lows_since_entry=lows, direction="short",
            )
        highs = [
            c["high"] for c in candles
            if "high" in c and (opened is None or c.get("time", 0) >= opened)
        ]
        return risk.protective_exit_reason(self.config, pos.avg_price, price, atr, highs)

    def _position_size(self, price, atr, prices, direction="long"):
        """Volatility-based size so the stop distance risks ~risk_per_trade_pct."""
        equity = self.portfolio.cash + self.portfolio.market_value(prices)
        return risk.position_size(
            self.config, equity, self.portfolio.cash, price, atr, direction=direction
        )

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

    def _short(self, product_id, price, qty, reasons, indicators):
        """Open a short: a SELL while flat credits cash and leaves a negative
        position the engine later covers."""
        try:
            trade = self.portfolio.execute(
                SELL, product_id, price, qty, reasons=reasons, indicators=indicators
            )
        except InsufficientPosition as exc:
            log.warning("%s: %s", product_id, exc)
            return None
        return self._finalize(trade, price)

    def _cover(self, product_id, price, qty, reasons, indicators):
        """Cover a short: a BUY that buys the position back to flat."""
        try:
            trade = self.portfolio.execute(
                BUY, product_id, price, qty, reasons=reasons, indicators=indicators
            )
        except (InsufficientFunds, InsufficientPosition) as exc:
            log.warning("%s: %s", product_id, exc)
            return None
        return self._finalize(trade, price)

    def _notif_prefix(self) -> str:
        """`[name] ` tag for multi-account push notifications; empty for the
        single-account/default path so legacy alerts read exactly as before."""
        name = getattr(self.config, "account_name", "")
        return f"[{name}] " if name and name != "default" else ""

    def _finalize(self, trade, price):
        trade.explanation = self.explainer.explain(
            trade, self.portfolio, {trade.product_id: price}
        )
        self.storage.save_trade(trade)
        log.info("EXECUTED %s | %s", trade.side, trade.explanation)
        # Realized P&L is non-zero only on a closing leg — a SELL closing a long
        # or a BUY covering a short — so this fires on either kind of win.
        if trade.realized_pnl > 0:
            notional = trade.price * trade.quantity
            pct = (trade.realized_pnl / notional) * 100 if notional > 0 else 0
            verb = "Covered" if trade.side == BUY else "Sold"
            self.notifier.send(
                title=f"{self._notif_prefix()}Profit: {trade.product_id} +${trade.realized_pnl:,.2f}",
                message=(
                    f"{verb} {trade.quantity:.6g} {trade.product_id} @ ${trade.price:,.2f}\n"
                    f"Profit: +${trade.realized_pnl:,.2f} ({pct:.1f}% of notional)\n"
                    f"{trade.explanation}"
                ),
                tags="money_bag,white_check_mark",
                priority="high",
            )
        return trade

    def _maybe_notify_new_high(self, current_equity: float) -> None:
        """Send a notification when the portfolio reaches a new all-time high.

        Requires at least 0.5% above the previous peak to avoid alerting on
        every tick during a slow grind up.
        """
        threshold = (self._peak_equity or 0) * 1.005
        if self._peak_equity is None or current_equity > threshold:
            if self._peak_equity is not None:
                change = current_equity - self._peak_equity
                pct = change / self._peak_equity * 100
                self.notifier.send(
                    title=f"{self._notif_prefix()}New portfolio high: ${current_equity:,.2f}",
                    message=(
                        f"Portfolio hit a new all-time high of ${current_equity:,.2f} "
                        f"(+${change:,.2f} / +{pct:.1f}% above previous peak)"
                    ),
                    tags="rocket,chart_with_upwards_trend",
                    priority="default",
                )
            self._peak_equity = current_equity
            self.storage.set_meta("peak_equity", str(current_equity))

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
