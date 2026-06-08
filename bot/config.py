"""Configuration loading.

Config comes from two places:
  * A YAML file (defaults to ``config.yaml``) for strategy/bot settings.
  * Environment variables / a ``.env`` file for secrets (API keys).

Everything has a sensible default, so the bot runs out of the box with no
config file and no keys (using public market data and a templated, non-LLM
trade rationale).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from .sentiment import DEFAULT_FEEDS
from .strategy import StrategyConfig


def _sanitize_name(name: str) -> str:
    """Filesystem-safe slug for deriving per-account db paths."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)


@dataclass
class AccountConfig:
    """One paper-trading account: its own strategy, products, cash, and DB.

    Risk fields default to ``None`` meaning "inherit the top-level ``Config``
    value"; set one to override it for just this account.
    """

    name: str
    strategy_type: str = "ema_crossover"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    products: list[str] = field(default_factory=lambda: ["BTC-USD"])
    starting_cash: float = 10_000.0
    db_path: str = ""  # defaults to trading.<name>.db when empty

    # Optional per-account risk overrides (None -> inherit from Config).
    fee_rate: float | None = None
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_open_positions: int | None = None
    stop_loss_atr_mult: float | None = None
    take_profit_atr_mult: float | None = None
    trailing_stop: bool | None = None
    fallback_stop_pct: float | None = None

    def resolved_db_path(self) -> str:
        return self.db_path or f"trading.{_sanitize_name(self.name)}.db"


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    # What to trade and how.
    products: list[str] = field(default_factory=lambda: ["BTC-USD"])
    starting_cash: float = 10_000.0
    fee_rate: float = 0.006  # 0.6% taker fee, Coinbase-ish
    buy_fraction: float = 0.25  # fallback sizing when ATR is unavailable
    poll_interval: int = 3600  # seconds between ticks (matches 1h candles)

    # Market data. 300 candles so the 200-period trend filter has history.
    candle_granularity: str = "ONE_HOUR"  # ONE_HOUR / ONE_DAY / etc.
    candle_count: int = 300
    data_source: str = "public"  # kept for backward compat; CCXT is now used for all sources
    exchange: str = "coinbase"   # any CCXT exchange id (coinbase, kraken, binance, ...)

    # Strategy. ``strategy_type`` selects the algorithm from the registry in
    # bot/strategies.py ("ema_crossover" default; "rsi_mean_reversion",
    # "donchian_breakout", ...). ``strategy`` holds the shared tunables.
    strategy_type: str = "ema_crossover"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    # Multiple paper accounts, each with its own strategy/products/cash/DB. When
    # empty, Config.load synthesizes a single "default" account from the
    # top-level products/strategy/starting_cash/db_path (backward compatible).
    accounts: list = field(default_factory=list)

    # Name of the account this Config drives (set by the Runner per account).
    # Surfaced in push-notification titles so multi-account alerts are
    # distinguishable. Empty / "default" means the single-account path.
    account_name: str = ""

    # Risk management (engine-level sizing + protective exits).
    risk_per_trade_pct: float = 0.01  # risk ~1% of equity per trade on the stop
    max_position_pct: float = 0.30  # cap any single position at 30% of equity
    max_open_positions: int = 3  # portfolio heat cap
    stop_loss_atr_mult: float = 2.0  # initial stop = entry - mult * ATR
    take_profit_atr_mult: float = 4.0  # target = entry + mult * ATR (2:1 reward:risk)
    trailing_stop: bool = True  # ride winners with a Chandelier trailing stop
    fallback_stop_pct: float = 0.08  # stop distance when ATR isn't available

    # Claude trade explanations.
    explanations_enabled: bool = True
    explain_model: str = "claude-opus-4-8"

    # News sentiment (optional; needs ANTHROPIC_API_KEY + network).
    sentiment_enabled: bool = False
    sentiment_model: str = "claude-opus-4-8"
    sentiment_cache_ttl: int = 1800  # seconds to reuse a sentiment score
    sentiment_max_headlines: int = 15
    news_feeds: list[str] = field(default_factory=lambda: list(DEFAULT_FEEDS))

    # Persistence / output.
    db_path: str = "trading.db"
    dashboard_state_path: str = "dashboard/state.json"

    # Publish state to GitHub Pages so a phone can view the dashboard remotely.
    publish_enabled: bool = False
    publish_repo: str = ""  # "owner/repo", e.g. "AndyRBrett/crypto-trading"
    publish_branch: str = "gh-pages"
    publish_path: str = "state.json"

    # Driver coordination: let a running laptop take priority over the cloud and
    # share one continuous portfolio between them (see bot/coordinate.py).
    coordinate_enabled: bool = False
    driver_role: str = "local"  # "local" (laptop) or "cloud" (Actions)
    lease_ttl_seconds: int = 1800  # cloud stands down while a local lease is this fresh
    state_branch: str = "bot-state"  # shared branch holding trading.db + driver.json
    state_db_path: str = "trading.db"  # path of the shared DB on state_branch
    lease_path: str = "driver.json"  # path of the lease on state_branch

    # Web Push notifications through the dashboard PWA.
    # Enable from the dashboard's "Enable Notifications" button, then copy
    # the subscription JSON into the PUSH_SUBSCRIPTION GitHub Actions secret.
    push_subscription: str = ""   # JSON from pushManager.subscribe() — via PUSH_SUBSCRIPTION env
    vapid_private_key: str = ""   # base64url raw P-256 scalar   — via VAPID_PRIVATE_KEY env
    vapid_claims_email: str = "mailto:bot@example.com"  # VAPID contact (can stay default)

    # Secrets (populated from env, never written to disk by us).
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    anthropic_api_key: str = ""
    github_token: str = ""

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        _load_dotenv()
        data: dict = {}
        p = Path(path)
        if p.exists():
            import yaml  # imported lazily so the dep is optional

            data = yaml.safe_load(p.read_text()) or {}

        valid_strategy = {f.name for f in fields(StrategyConfig)}
        strategy_data = data.pop("strategy", {}) or {}

        def _build_strategy(overrides: dict | None) -> StrategyConfig:
            merged = {**strategy_data, **(overrides or {})}
            return StrategyConfig(
                **{k: v for k, v in merged.items() if k in valid_strategy}
            )

        strategy = _build_strategy(None)

        accounts_data = data.pop("accounts", None) or []

        valid = {f.name for f in fields(cls)} - {"strategy", "accounts"}
        kwargs = {k: v for k, v in data.items() if k in valid}
        cfg = cls(strategy=strategy, **kwargs)

        # Build per-account configs. Each account's `strategy:` block is merged
        # over the top-level strategy defaults; unknown keys are ignored.
        valid_account = {f.name for f in fields(AccountConfig)} - {"strategy"}
        accounts: list[AccountConfig] = []
        for entry in accounts_data:
            entry = dict(entry or {})
            acct_strategy = _build_strategy(entry.pop("strategy", None))
            kw = {k: v for k, v in entry.items() if k in valid_account}
            accounts.append(AccountConfig(strategy=acct_strategy, **kw))

        # Backward compat: no `accounts:` -> one "default" account mirroring the
        # top-level fields, so the legacy single-account path is unchanged.
        if not accounts:
            accounts.append(
                AccountConfig(
                    name="default",
                    strategy_type=cfg.strategy_type,
                    strategy=cfg.strategy,
                    products=cfg.products,
                    starting_cash=cfg.starting_cash,
                    db_path=cfg.db_path,
                )
            )
        cfg.accounts = accounts

        # Secrets always come from the environment.
        cfg.coinbase_api_key = os.environ.get("COINBASE_API_KEY", "")
        cfg.coinbase_api_secret = os.environ.get("COINBASE_API_SECRET", "")
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        cfg.github_token = os.environ.get("GITHUB_TOKEN", "")
        # Push notification secrets always come from the environment.
        cfg.push_subscription = os.environ.get("PUSH_SUBSCRIPTION", "")
        cfg.vapid_private_key = os.environ.get("VAPID_PRIVATE_KEY", "")
        return cfg
