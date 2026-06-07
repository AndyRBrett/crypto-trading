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

    # Strategy.
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

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

        strategy_data = data.pop("strategy", {}) or {}
        valid_strategy = {f.name for f in fields(StrategyConfig)}
        strategy = StrategyConfig(
            **{k: v for k, v in strategy_data.items() if k in valid_strategy}
        )

        valid = {f.name for f in fields(cls)} - {"strategy"}
        kwargs = {k: v for k, v in data.items() if k in valid}
        cfg = cls(strategy=strategy, **kwargs)

        # Secrets always come from the environment.
        cfg.coinbase_api_key = os.environ.get("COINBASE_API_KEY", "")
        cfg.coinbase_api_secret = os.environ.get("COINBASE_API_SECRET", "")
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        cfg.github_token = os.environ.get("GITHUB_TOKEN", "")
        return cfg
