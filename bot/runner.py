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
import os
import tempfile

from .config import Config
from .coordinate import Coordinator
from .engine import Engine
from .explain import Explainer
from .market_data import MarketData
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
        self.analyzer = sentiment_analyzer
        if self.analyzer is None and config.sentiment_enabled:
            self.analyzer = SentimentAnalyzer(config)

        self.accounts = config.accounts
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
            )
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
        all_trades: list = []
        for acct, engine in self.engines:
            all_trades += engine.tick()

        self._export_combined()
        if self.publisher.enabled:
            self.publisher.publish(self.config.dashboard_state_path)
        if self.coordinator.enabled:
            for acct, engine in self.engines:
                self.coordinator.push_db_for(acct.name, engine.config.db_path)
        return all_trades

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
            granularity=self.config.candle_granularity,
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
