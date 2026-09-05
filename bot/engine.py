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
from .market_data import _GRANULARITY_SECONDS, MarketData, closed_candles
from .notifier import Notifier
from .portfolio import InsufficientFunds, InsufficientPosition, Portfolio
from .publish import Publisher
from . import breaker, costs, risk
from .sentiment import SentimentAnalyzer
from .storage import Storage
from .strategies import make_strategy
from .strategy import BUY, HOLD, SELL

log = logging.getLogger(__name__)

# Equity snapshots read back for the rolling-risk breaker: enough to cover its
# trailing window with hourly ticks (30 days x 24 + headroom for the extra days
# it walks back), bounded so the query stays cheap.
EQUITY_CURVE_LIMIT = 1000

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
REENTRY_COOLDOWN = "reentry_cooldown"  # entry blocked: too soon after a stop-out
PORTFOLIO_EXPOSURE = "portfolio_exposure"  # entry vetoed: combined gross over cap
BELOW_COST_FLOOR = "below_cost_floor"  # entry vetoed: projected move < round-trip cost
RISK_BREAKER = "risk_breaker"  # entry paused: rolling risk-adjusted performance below floor
# reject_codes that mean "we wanted to BUY but couldn't" vs. "no actionable signal".
_REJECTED_CODES = frozenset(
    {MAX_OPEN_POSITIONS, SIZE_ZERO, INSUFFICIENT_BALANCE, REENTRY_COOLDOWN,
     PORTFOLIO_EXPOSURE, BELOW_COST_FLOOR, RISK_BREAKER}
)


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
        portfolio_guard=None,
    ):
        self.config = config
        self.market_data = market_data or MarketData(config)
        # Cross-account exposure guard (see bot/portfolio_guard.py). Only the
        # multi-account Runner wires one in; None means no guard, and a
        # disabled guard approves everything — either way entries behave as
        # before until portfolio_guard_enabled is set.
        self.portfolio_guard = portfolio_guard
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
        # Last cost-floor measurement per product (issue #44), attached to the
        # signal log so the gate's effect is readable before it's switched on.
        self._cost_floor: dict = {}
        # This tick's rolling-risk breaker state (issue #45), recomputed at the
        # top of every tick from the persisted equity curve.
        self._breaker: dict = breaker.breaker_state(config, [], time.time())
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

        # Rolling-risk breaker: one evaluation per tick, shared by every product
        # (it is a portfolio-level throttle, not a per-signal filter).
        self._refresh_breaker()

        executed = []
        prices: dict[str, float] = {}
        price_history: dict[str, list] = {}

        # Fetch every product's candles up front. Per-product strategies see no
        # difference; cross-sectional strategies (momentum_rotation) get the
        # whole universe via the optional prepare() hook before signals run.
        candles_by_product: dict[str, list] = {}
        for product_id in self.config.products:
            try:
                candles = self.market_data.get_candles(product_id)
            except Exception as exc:
                log.error("Failed to fetch candles for %s: %s", product_id, exc)
                continue
            if not candles:
                log.warning("No candles for %s", product_id)
                continue
            candles_by_product[product_id] = candles

        if hasattr(self.strategy, "prepare"):
            self.strategy.prepare(
                {
                    pid: closed_candles(c, self.config.candle_granularity)
                    for pid, c in candles_by_product.items()
                }
            )

        for product_id, candles in candles_by_product.items():
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
            # Edge-vs-cost economics for this signal, when an entry was on the
            # table: what the trade projected, what the round trip costs, and
            # what it needed to clear (issue #44).
            cost_floor = self._cost_floor.pop(product_id, None)
            if cost_floor is not None:
                features["cost_floor"] = cost_floor
            # Portfolio-level throttle in force this tick (issue #45), recorded
            # so a shrunk (or skipped) entry is explainable after the fact.
            if self._breaker["tripped"] or self._breaker["days_breached"]:
                features["risk_breaker"] = self._breaker
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
                # A skipped snapshot leaves the merged equity curve flat across
                # this store's ticks with no distinguishing signal (issue #50:
                # a 25h price freeze read as "equity didn't move" instead of
                # "we couldn't price it"). Persist it so overseer status can
                # report it as a fault; cleared the moment pricing recovers.
                self.storage.set_meta("last_equity_skip_at", str(time.time()))
                self.storage.set_meta(
                    "last_equity_skip_products", ",".join(unpriced)
                )
            else:
                current_equity = self.portfolio.total_equity(prices)
                self.storage.save_equity(
                    self.portfolio.cash,
                    self.portfolio.market_value(prices),
                    current_equity,
                )
                self.storage.set_meta("last_equity_skip_at", "")
                # Clear the products too. The read above is gated on _at, so a
                # stale list is inert today — but leaving a recovered store
                # claiming "no fresh price for ETH-USD" is a trap for the next
                # reader of this table.
                self.storage.set_meta("last_equity_skip_products", "")
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
            if self._in_reentry_cooldown(product_id):
                log.info("%s: in post-stop re-entry cooldown, skipping BUY", product_id)
                return None, REENTRY_COOLDOWN
            if self._at_max_positions():
                log.info("%s: at max open positions, skipping BUY", product_id)
                return None, MAX_OPEN_POSITIONS
            if self._below_cost_floor(product_id, price, atr):
                return None, BELOW_COST_FLOOR
            if self._breaker_pauses_entries():
                log.info("%s: %s, skipping BUY", product_id, breaker.describe(self._breaker))
                return None, RISK_BREAKER
            qty = self._position_size(price, atr, prices)
            if qty <= 0:
                log.info("%s: position size ~0 after risk limits, skipping BUY", product_id)
                return None, SIZE_ZERO
            veto = self._guard_vetoes_entry(product_id, qty * price, prices)
            if veto:
                return None, PORTFOLIO_EXPOSURE
            trade = self._buy(product_id, price, qty, signal.reasons, signal.indicators)
            return trade, ACTED if trade else INSUFFICIENT_BALANCE

        if signal.action == SELL and getattr(self.config, "allow_short", False):
            if self._in_reentry_cooldown(product_id):
                log.info("%s: in post-stop re-entry cooldown, skipping SHORT", product_id)
                return None, REENTRY_COOLDOWN
            if self._at_max_positions():
                log.info("%s: at max open positions, skipping SHORT", product_id)
                return None, MAX_OPEN_POSITIONS
            if self._below_cost_floor(product_id, price, atr):
                return None, BELOW_COST_FLOOR
            if self._breaker_pauses_entries():
                log.info("%s: %s, skipping SHORT", product_id, breaker.describe(self._breaker))
                return None, RISK_BREAKER
            qty = self._position_size(price, atr, prices, direction="short")
            if qty <= 0:
                log.info("%s: short size ~0 after risk limits, skipping", product_id)
                return None, SIZE_ZERO
            veto = self._guard_vetoes_entry(product_id, qty * price, prices)
            if veto:
                return None, PORTFOLIO_EXPOSURE
            trade = self._short(product_id, price, qty, signal.reasons, signal.indicators)
            return trade, ACTED if trade else INSUFFICIENT_BALANCE

        return None, NO_POSITION if signal.action == SELL else NO_SIGNAL

    def _guard_vetoes_entry(self, product_id: str, notional: float, prices: dict) -> bool:
        """Ask the cross-account guard about a NEW entry (never about exits).

        No guard wired, or guard disabled -> never vetoes.
        """
        if self.portfolio_guard is None:
            return False
        ok, why = self.portfolio_guard.allows_entry(notional, prices)
        if not ok:
            log.info("%s: portfolio guard vetoed entry — %s", product_id, why)
        return not ok

    def _refresh_breaker(self) -> None:
        """Recompute the rolling-risk breaker from the persisted equity curve.

        Evaluated once per tick and cached on the engine: it is a portfolio-level
        judgement about whether the strategy is working, so every product this
        tick sees the same answer. State transitions (trip / recover) are logged,
        persisted to meta for overseer status, and pushed — a book that quietly
        halved its own size is exactly the kind of change that must not be
        invisible. Any failure leaves sizing untouched: the breaker may never be
        the reason a tick dies.
        """
        try:
            rows = self.storage.load_equity_curve(limit=EQUITY_CURVE_LIMIT)
            curve = [(float(r["timestamp"]), float(r["equity"])) for r in rows]
            state = breaker.breaker_state(self.config, curve, time.time())
        except Exception as exc:
            log.warning("could not evaluate the risk breaker: %s", exc)
            return
        was_tripped = self.storage.get_meta("risk_breaker_tripped") == "1"
        self._breaker = state
        if state["tripped"] != was_tripped:
            log.warning("%s", breaker.describe(state))
            self.storage.set_meta("risk_breaker_tripped", "1" if state["tripped"] else "")
            self.storage.set_meta(
                "risk_breaker_changed_at", str(time.time())
            )
            self._notify(
                title=(
                    f"{self._notif_prefix()}Risk breaker "
                    f"{'tripped' if state['tripped'] else 'cleared'}"
                ),
                message=breaker.describe(state),
                tags="warning" if state["tripped"] else "white_check_mark",
                priority="high" if state["tripped"] else "default",
            )
        elif state["tripped"]:
            log.info("%s", breaker.describe(state))

    def _breaker_pauses_entries(self) -> bool:
        """True when the breaker is throttling new entries all the way to zero."""
        return self._breaker["tripped"] and self._breaker["size_multiplier"] <= 0

    def _below_cost_floor(self, product_id: str, price: float, atr) -> bool:
        """True when this entry's projected move doesn't clear its round-trip cost.

        Prices the round trip from the product's own recent fills (2 x fee_rate
        plus twice the median logged slippage) and compares it to the take-profit
        distance the trade would be managed toward, times ``cost_floor_margin``
        (issue #44). The measurement runs on every entry candidate either way and
        is stashed for the signal log; only ``cost_floor_enabled`` lets it veto.
        Never consulted on an exit — an open position must always be able to close.
        """
        samples: list = []
        reader = getattr(self.storage, "recent_slippage_bps", None)
        if reader is not None:
            try:
                samples = reader(product_id, self.config.cost_floor_samples)
            except Exception as exc:  # a cost estimate is never worth a failed tick
                log.warning("could not read slippage history for %s: %s", product_id, exc)
        verdict = costs.cost_floor_verdict(self.config, price, atr, samples)
        self._cost_floor[product_id] = verdict
        if verdict["blocked"]:
            log.info(
                "%s: below cost floor — projected %.0f bps < required %.0f bps "
                "(round-trip cost %.0f bps from %d fill(s)), skipping entry",
                product_id, verdict["edge_bps"], verdict["required_bps"],
                verdict["cost_bps"], verdict["samples"],
            )
        return verdict["blocked"]

    def _in_reentry_cooldown(self, product_id: str) -> bool:
        """True while new entries in this product are blocked after a stop-out.

        Bar length comes from the configured candle granularity; the stop-exit
        timestamp comes from the replayed trade log (see
        risk.reentry_cooldown_active), so this is restart-safe. Off by default
        (``reentry_cooldown_bars: 0``) — existing behavior is unchanged.
        """
        span = _GRANULARITY_SECONDS.get(self.config.candle_granularity, 0)
        return risk.reentry_cooldown_active(
            self.config, self.portfolio.trades, product_id, time.time(), span
        )

    def _at_max_positions(self) -> bool:
        """True once the portfolio holds the max concurrent positions (either
        direction counts toward the heat cap)."""
        open_count = sum(1 for p in self.portfolio.positions.values() if p.quantity != 0)
        return open_count >= self.config.max_open_positions

    def _protective_exit(self, product_id, pos, price, atr, candles):
        """Return an exit reason if a stop/target/trailing level is breached, or
        if the position has aged out without going anywhere (issue #52).

        Direction is read from the position's sign: a short trails the lowest low
        since entry and stops out *above* entry, the mirror of a long. Stops and
        targets are checked first — an aging exit is the fallback for a trade
        that triggers neither, never a reason to pre-empt one that does.
        """
        opened = self.portfolio.opened_at(product_id)
        direction = "short" if pos.quantity < 0 else "long"
        if pos.quantity < 0:
            lows = [
                c["low"] for c in candles
                if "low" in c and (opened is None or c.get("time", 0) >= opened)
            ]
            reason = risk.protective_exit_reason(
                self.config, pos.avg_price, price, atr,
                lows_since_entry=lows, direction="short",
            )
        else:
            highs = [
                c["high"] for c in candles
                if "high" in c and (opened is None or c.get("time", 0) >= opened)
            ]
            reason = risk.protective_exit_reason(
                self.config, pos.avg_price, price, atr, highs
            )
        if reason is not None:
            return reason
        return risk.aging_exit_reason(
            self.config, opened, time.time(), pos.avg_price, price, direction
        )

    def _position_size(self, price, atr, prices, direction="long"):
        """Volatility-based size so the stop distance risks ~risk_per_trade_pct.

        Scaled by the rolling-risk breaker's multiplier (1.0 unless the breaker
        is armed and tripped), so a bleeding book takes the same signals at
        reduced size instead of at full size (issue #45).
        """
        equity = self.portfolio.cash + self.portfolio.market_value(prices)
        return risk.position_size(
            self.config, equity, self.portfolio.cash, price, atr, direction=direction,
            size_mult=self._breaker["size_multiplier"],
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

    def _notify(self, title: str, message: str, tags: str = "", priority: str = "default") -> bool:
        """Send a push and persist the outcome.

        ``last_notify_at`` is what the Runner's heartbeat measures silence
        against, and ``last_push_error`` is what surfaces a dead push channel in
        overseer-status.json — without them a rejected push is invisible.
        """
        if not self.notifier.enabled:
            return False
        ok = self.notifier.send(title=title, message=message, tags=tags, priority=priority)
        if ok:
            self.storage.set_meta("last_notify_at", str(time.time()))
            self.storage.set_meta("last_push_error", "")
        else:
            self.storage.set_meta("last_push_error", self.notifier.last_error)
        return ok

    def last_notify_at(self) -> float:
        raw = self.storage.get_meta("last_notify_at")
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def _finalize(self, trade, price):
        trade.explanation = self.explainer.explain(
            trade, self.portfolio, {trade.product_id: price}
        )
        self.storage.save_trade(trade)
        log.info("EXECUTED %s | %s", trade.side, trade.explanation)
        # Realized P&L is non-zero only on a closing leg — a SELL closing a long
        # or a BUY covering a short — so this fires on either kind of round trip.
        # Losses notify too (unless notify_on_loss is off): alerting only on wins
        # meant a 0-for-12 losing streak produced exactly as many notifications
        # as a crashed bot, which is how six weeks of silence went unexplained.
        if trade.realized_pnl != 0:
            won = trade.realized_pnl > 0
            if won or getattr(self.config, "notify_on_loss", True):
                notional = trade.price * trade.quantity
                pct = (trade.realized_pnl / notional) * 100 if notional > 0 else 0
                verb = "Covered" if trade.side == BUY else "Sold"
                label = "Profit" if won else "Loss"
                self._notify(
                    title=(
                        f"{self._notif_prefix()}{label}: {trade.product_id} "
                        f"{trade.realized_pnl:+,.2f}"
                    ),
                    message=(
                        f"{verb} {trade.quantity:.6g} {trade.product_id} @ ${trade.price:,.2f}\n"
                        f"{label}: ${trade.realized_pnl:+,.2f} ({pct:+.1f}% of notional)\n"
                        f"{trade.explanation}"
                    ),
                    tags="money_bag,white_check_mark" if won else "chart_with_downwards_trend",
                    priority="high" if won else "default",
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
                self._notify(
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
