"""Tests for multi-account config loading + backward-compat synthesis."""

from bot.config import AccountConfig, Config


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return str(p)


def test_accounts_parsed(tmp_path):
    path = _write(
        tmp_path,
        """
strategy:
  fast_period: 20
accounts:
  - name: trend
    strategy_type: ema_crossover
    products: [BTC-USD, ETH-USD]
    starting_cash: 10000
  - name: meanrev
    strategy_type: rsi_mean_reversion
    products: [SOL-USD]
    starting_cash: 5000
    strategy:
      rsi_mr_oversold: 25
""",
    )
    cfg = Config.load(path)
    assert [a.name for a in cfg.accounts] == ["trend", "meanrev"]
    trend, meanrev = cfg.accounts
    assert trend.strategy_type == "ema_crossover"
    assert trend.products == ["BTC-USD", "ETH-USD"]
    assert trend.resolved_db_path() == "trading.trend.db"
    # Per-account override merges over the top-level strategy defaults.
    assert meanrev.strategy.rsi_mr_oversold == 25
    assert meanrev.strategy.fast_period == 20  # inherited from top-level
    assert meanrev.starting_cash == 5000


def test_no_accounts_synthesizes_default(tmp_path):
    path = _write(
        tmp_path,
        """
products: [BTC-USD]
starting_cash: 12345
db_path: trading.db
""",
    )
    cfg = Config.load(path)
    assert len(cfg.accounts) == 1
    acct = cfg.accounts[0]
    assert acct.name == "default"
    assert acct.strategy_type == "ema_crossover"
    assert acct.products == ["BTC-USD"]
    assert acct.starting_cash == 12345
    assert acct.resolved_db_path() == "trading.db"
    # Top-level fields remain authoritative for the legacy single-account path.
    assert cfg.products == ["BTC-USD"]


def test_default_config_has_one_default_account():
    # A Config() built directly (as tests do) has no accounts; load() is what
    # synthesizes them. Verify load with a missing file still yields one.
    cfg = Config.load("does-not-exist-xyz.yaml")
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].name == "default"


def test_risk_override_resolution():
    acct = AccountConfig(name="x", max_open_positions=5)
    assert acct.max_open_positions == 5
    assert acct.risk_per_trade_pct is None  # inherits from Config at runtime
