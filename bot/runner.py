"""Multi-account orchestrator.

The bot can run several paper accounts at once, each with its own strategy,
products, starting cash, and SQLite DB. The :class:`Runner` owns the shared,
stateless services (one market-data feed, one sentiment analyzer, one explainer,
one publisher, one coordinator/lease) and builds one :class:`Engine` per account.
On each tick it drives every engine, then writes ONE combined ``state.json`` for
the unified dashboard, publishes it once, and pushes each account's DB once.

Backward compatible: a config with no ``accounts:`` block synthesizes a single
"default" account in :meth:`Config.load`, so the Runner ticks one engine whose
output mirrors the legacy single-account behavior.
"""

from __future__ import annotations

import dataclasses
import logging
import json
import os
import tempfile
import time

from .config import Config
from .coordinate import Coordinator
from .engine import Engine
from .explain import Explainer
from .market_data import MarketData, closed_candles, _GRANULARITY_SECONDS
from .notifier import Notifier, derive_public_key
from .portfolio_guard import PortfolioGuard
from .publish import Publisher
from .sentiment import SentimentAnalyzer
from .storage import export_combined_state

log = logging.getLogger(__name__)


class CachedMarketData:
    """Wrap a MarketData so each (product, granularity, count) is fetched once
    per tick — shared across accounts that trade overlapping products. Call
    :meth:`clear` at the start of every tick."""

    def __init__(self, inner):
        self.inner = inner
        self._cache: dict = {}

    def clear(self) -> None:
        self._cache.clear()

    def get_candles(self, product_id, granularity=None, count=None):
        key = (product_id, granularity, count)
        if key not in self._cache:
            self._cache[key] = self.inner.get_candles(product_id, granularity, count)
        return self._cache[key]

    def get_price(self, product_id):
        return self.inner.get_price(product_id)

    def get_prices(self, product_ids):
        return self.inner.get_prices(product_ids)

    def verify_credentials(self):
        return self.inner.verify_credentials()

    def _public_price(self, product_id):  # used by `main verify`
        return self.inner.get_price(product_id)


class Runner:
    def __init__(
        self,
        config: Config,
        market_data=None,
        explainer: Explainer | None = None,
        sentiment_analyzer: SentimentAnalyzer | None = None,
        publisher: Publisher | None = None,
        coordinator: Coordinator | None = None,
    ):
        self.config = config
        # Shared services, built once and injected into every engine.
        self.coordinator = coordinator or Coordinator(config)
        self.publisher = publisher or Publisher(config)
        self.market_data = CachedMarketData(market_data or MarketData(config))
        self.explainer = explainer or Explainer(config)
        # Runner-level notifier for the cross-account heartbeat. Per-trade alerts
        # stay with the engine that made the trade.
        self.notifier = Notifier(
            config.push_subscription, config.vapid_private_key, config.vapid_claims_email
        )
        self.analyzer = sentiment_analyzer
        if self.analyzer is None and config.sentiment_enabled:
            self.analyzer = SentimentAnalyzer(config)
            # The Engine warns about this too, but only when IT builds the
            # analyzer — on the multi-account path the Runner builds it first, so
            # the warning never fired and sentiment sat silently neutral for
            # months with sentiment_enabled: true in config.ci.yaml.
            if not config.anthropic_api_key:
                log.warning(
                    "sentiment_enabled is set but ANTHROPIC_API_KEY is missing — every "
                    "sentiment score will be a neutral 0.0 and trade explanations will "
                    "use the templated fallback until a key is provided."
                )

        self.accounts = config.accounts
        # One guard across all account engines: read-only exposure snapshot
        # every tick, entry veto only when portfolio_guard_enabled is set.
        self.portfolio_guard = PortfolioGuard(config)
        self.engines: list[tuple] = []  # (account, engine)
        for acct in self.accounts:
            acct_cfg = self._account_config(acct)
            # Pull the account's shared DB before the engine opens sqlite.
            if self.coordinator.enabled:
                self.coordinator.pull_db_for(acct.name, acct_cfg.db_path)
            engine = Engine(
                acct_cfg,
                market_data=self.market_data,
                explainer=self.explainer,
                sentiment_analyzer=self.analyzer,
                portfolio_guard=self.portfolio_guard,
            )
            self.portfolio_guard.register(engine)
            self.engines.append((acct, engine))

    def _account_config(self, acct) -> Config:
        """A per-account Config clone: account fields applied, publish/coordinate
        disabled (the Runner owns those), and a scratch dashboard path so the
        per-engine export never clobbers the combined state.json."""
        base = self.config

        def pick(override, fallback):
            return fallback if override is None else override

        return dataclasses.replace(
            base,
            products=acct.products,
            starting_cash=acct.starting_cash,
            db_path=acct.resolved_db_path(),
            strategy=acct.strategy,
            strategy_type=acct.strategy_type,
            account_name=acct.name,
            fee_rate=pick(acct.fee_rate, base.fee_rate),
            risk_per_trade_pct=pick(acct.risk_per_trade_pct, base.risk_per_trade_pct),
            max_position_pct=pick(acct.max_position_pct, base.max_position_pct),
            max_open_positions=pick(acct.max_open_positions, base.max_open_positions),
            stop_loss_atr_mult=pick(acct.stop_loss_atr_mult, base.stop_loss_atr_mult),
            take_profit_atr_mult=pick(acct.take_profit_atr_mult, base.take_profit_atr_mult),
            trailing_stop=pick(acct.trailing_stop, base.trailing_stop),
            fallback_stop_pct=pick(acct.fallback_stop_pct, base.fallback_stop_pct),
            allow_short=pick(acct.allow_short, base.allow_short),
            reentry_cooldown_bars=pick(acct.reentry_cooldown_bars, base.reentry_cooldown_bars),
            cost_floor_enabled=pick(acct.cost_floor_enabled, base.cost_floor_enabled),
            cost_floor_margin=pick(acct.cost_floor_margin, base.cost_floor_margin),
            cost_floor_samples=pick(acct.cost_floor_samples, base.cost_floor_samples),
            risk_breaker_enabled=pick(acct.risk_breaker_enabled, base.risk_breaker_enabled),
            risk_breaker_days=pick(acct.risk_breaker_days, base.risk_breaker_days),
            risk_breaker_size_mult=pick(acct.risk_breaker_size_mult, base.risk_breaker_size_mult),
            risk_breaker_sharpe_floor=pick(
                acct.risk_breaker_sharpe_floor, base.risk_breaker_sharpe_floor
            ),
            risk_breaker_sortino_floor=pick(
                acct.risk_breaker_sortino_floor, base.risk_breaker_sortino_floor
            ),
            max_hold_days=pick(acct.max_hold_days, base.max_hold_days),
            max_hold_min_gain_pct=pick(
                acct.max_hold_min_gain_pct, base.max_hold_min_gain_pct
            ),
            vol_target_enabled=pick(acct.vol_target_enabled, base.vol_target_enabled),
            vol_target_pct=pick(acct.vol_target_pct, base.vol_target_pct),
            vol_lookback_bars=pick(acct.vol_lookback_bars, base.vol_lookback_bars),
            # The Runner publishes/coordinates once per tick, not per engine.
            publish_enabled=False,
            coordinate_enabled=False,
            # Scratch path in the temp dir: the engine's own per-account export
            # is discarded — the Runner writes the authoritative combined file.
            dashboard_state_path=os.path.join(
                tempfile.gettempdir(), f"bot-state-{acct.name}.json"
            ),
            accounts=[],
        )

    def tick(self) -> list:
        """Tick every account, then export/publish/push once. Returns all trades."""
        # One lease decision for the whole runner (not per account).
        if self.coordinator.enabled:
            if self.config.driver_role == "cloud" and self.coordinator.laptop_active():
                log.info("Laptop driver is active; cloud standing down this run.")
                return []
            self.coordinator.claim_lease()

        self.market_data.clear()
        self._record_config_warnings()
        # Prepare the complete cross-account universe before ANY entry, so
        # account iteration order cannot change the correlation evidence.
        histories = {}
        for pid in sorted({pid for acct, _ in self.engines for pid in acct.products}):
            try:
                histories[pid] = closed_candles(
                    self.market_data.get_candles(pid), self.config.candle_granularity)
            except Exception as exc:
                log.warning("Correlation history unavailable for %s: %s", pid, exc)
        self.portfolio_guard.prepare(
            histories, _GRANULARITY_SECONDS[self.config.candle_granularity])
        all_trades: list = []
        for acct, engine in self.engines:
            all_trades += engine.tick()

        # Read-only exposure heartbeat: the combined footprint of all accounts,
        # logged whether or not the veto is enabled.
        snap = self.portfolio_guard.snapshot()
        log.info(
            "portfolio guard%s: gross long $%.2f, gross short $%.2f, net $%.2f "
            "(equity $%.2f)",
            "" if self.portfolio_guard.enabled else " (veto off)",
            snap["gross_long"], snap["gross_short"], snap["net_exposure"],
            snap["equity"],
        )

        if self.engines:
            self.engines[0][1].storage.set_meta("portfolio_risk", json.dumps(snap))
        self._export_combined()
        self._maybe_heartbeat()
        if self.publisher.enabled:
            self.publisher.publish(self.config.dashboard_state_path)
        if self.coordinator.enabled:
            for acct, engine in self.engines:
                self.coordinator.push_db_for(acct.name, engine.config.db_path)
        return all_trades

    def _maybe_heartbeat(self) -> None:
        """Push a "still alive" summary after a long stretch with no other push.

        Trade and new-high alerts only fire when something good happens, so a
        bot parked in cash through a downtrend is silent for weeks — identical
        to a crashed workflow or an expired push subscription from the phone's
        point of view. The heartbeat makes healthy-and-idle say so out loud.
        """
        days = getattr(self.config, "heartbeat_days", 0) or 0
        if days <= 0 or not self.notifier.enabled or not self.engines:
            return
        last = max((engine.last_notify_at() for _, engine in self.engines), default=0.0)
        now = time.time()
        # First run with a fresh store has no history to measure silence against;
        # start the clock rather than firing a heartbeat immediately.
        if last <= 0:
            self._record_notify(now)
            return
        if now - last < days * 86_400:
            return

        snap = self.portfolio_guard.snapshot()
        quiet_days = (now - last) / 86_400
        lines = []
        for acct, engine in self.engines:
            open_positions = [
                pid for pid, p in engine.portfolio.positions.items() if p.quantity != 0
            ]
            # market_value() silently values unpriced products at zero, so a
            # failed candle fetch would otherwise report a fictitious equity
            # crash in the one alert meant to reassure.
            unpriced = [pid for pid in open_positions if pid not in engine.last_prices]
            if unpriced:
                lines.append(f"{acct.name}: equity unavailable (no fresh price for {', '.join(unpriced)})")
                continue
            equity = engine.portfolio.total_equity(engine.last_prices)
            ret = (equity / acct.starting_cash - 1) * 100 if acct.starting_cash else 0.0
            lines.append(
                f"{acct.name}: ${equity:,.0f} ({ret:+.1f}%, {len(open_positions)} open)"
            )

        ok = self.notifier.send(
            title=f"CryptoBot alive — quiet for {quiet_days:.0f} days",
            message=(
                f"Total equity ${snap['equity']:,.2f}.\n"
                + "\n".join(lines)
                + f"\nNo trades to report; last alert {quiet_days:.0f} days ago."
            ),
            tags="heartbeat",
        )
        if ok:
            self._record_notify(now)
        else:
            # A failed heartbeat is the single most important thing to surface:
            # it is the only alert that fires when nothing else would.
            log.error("Heartbeat push failed: %s", self.notifier.last_error)
            self.engines[0][1].storage.set_meta("last_push_error", self.notifier.last_error)

    def _record_config_warnings(self) -> None:
        """Persist settings that are enabled but inert, so the overseer reports
        them. A feature switched on in config with its secret missing degrades
        silently — the bot keeps running and nothing anywhere says it is off."""
        if not self.engines:
            return
        warnings = []
        if self.config.sentiment_enabled and not self.config.anthropic_api_key:
            warnings.append(
                "sentiment_enabled is true but ANTHROPIC_API_KEY is missing "
                "(scores pinned neutral; explanations templated)"
            )
        if self.config.push_subscription and not self.config.vapid_private_key:
            warnings.append("PUSH_SUBSCRIPTION is set but VAPID_PRIVATE_KEY is missing")
        if self.config.vapid_private_key and not self.config.push_subscription:
            warnings.append("VAPID_PRIVATE_KEY is set but PUSH_SUBSCRIPTION is missing")
        text = "\n".join(warnings)
        # Only write on change: this store is pushed to the state branch every
        # tick, so rewriting an identical value is pure churn.
        store = self.engines[0][1].storage
        if (store.get_meta("config_warnings") or "") != text:
            store.set_meta("config_warnings", text)

    def _record_notify(self, when: float) -> None:
        """Persist the heartbeat clock on the first engine's store (the DBs are
        synced to the state branch, so this survives ephemeral CI runners)."""
        self.engines[0][1].storage.set_meta("last_notify_at", str(when))
        self.engines[0][1].storage.set_meta("last_push_error", "")

    def _export_combined(self) -> None:
        prices: dict = {}
        price_history: dict = {}
        blocks: list[dict] = []
        for acct, engine in self.engines:
            prices.update(engine.last_prices)
            price_history.update(engine.last_price_history)
            blocks.append(
                engine.storage.account_state(
                    engine.portfolio,
                    engine.last_prices,
                    engine.latest_signals,
                    name=acct.name,
                    strategy=acct.strategy_type,
                    products=acct.products,
                )
            )
        export_combined_state(
            self.config.dashboard_state_path, blocks, prices, price_history,
            portfolio_risk=self.portfolio_guard.snapshot(),
            granularity=self.config.candle_granularity,
            vapid_public_key=derive_public_key(self.config.vapid_private_key),
        )

    def status(self) -> list[dict]:
        """Per-account status dicts (each tagged with name/strategy)."""
        out = []
        for acct, engine in self.engines:
            s = engine.status()
            s["name"] = acct.name
            s["strategy"] = acct.strategy_type
            out.append(s)
        return out

    def close(self) -> None:
        for _, engine in self.engines:
            engine.close()
