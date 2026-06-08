"""Tests for the multi-account Runner orchestrator."""

import json
import math

from bot.config import Config
from bot.runner import Runner


def _candles(n=300, base=100.0, slope=0.5):
    out = []
    for i in range(n):
        c = base + slope * i + 5 * math.sin(i / 5)
        out.append(
            {"time": 1700000000 + i * 3600, "open": c - 1, "high": c + 2, "low": c - 2, "close": c, "volume": 10}
        )
    return out


class CountingMarketData:
    def __init__(self):
        self.calls = {}

    def get_candles(self, pid, granularity=None, count=None):
        self.calls[pid] = self.calls.get(pid, 0) + 1
        return _candles()

    def get_prices(self, ids):
        return {p: _candles()[-1]["close"] for p in ids}

    def get_price(self, pid):
        return _candles()[-1]["close"]


class FakeCoordinator:
    def __init__(self, enabled=True, laptop=False):
        self.enabled = enabled
        self._laptop = laptop
        self.claimed = False
        self.pulled = []
        self.pushed = []

    def laptop_active(self):
        return self._laptop

    def claim_lease(self):
        self.claimed = True

    def pull_db_for(self, name, path):
        self.pulled.append(name)

    def push_db_for(self, name, path):
        self.pushed.append(name)


def _cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        """
sentiment_enabled: false
explanations_enabled: false
accounts:
  - {name: trend, strategy_type: ema_crossover, products: [BTC-USD, ETH-USD], starting_cash: 10000}
  - {name: meanrev, strategy_type: rsi_mean_reversion, products: [BTC-USD, SOL-USD], starting_cash: 10000}
  - {name: breakout, strategy_type: donchian_breakout, products: [ETH-USD, SOL-USD], starting_cash: 10000}
"""
    )
    cfg = Config.load(str(p))
    cfg.dashboard_state_path = str(tmp_path / "state.json")
    return cfg


def test_runner_ticks_all_and_dedupes_candles(tmp_path):
    cfg = _cfg(tmp_path)
    md = CountingMarketData()
    runner = Runner(cfg, market_data=md)
    runner.tick()
    # Three accounts trade overlapping products; each fetched once per tick.
    assert md.calls == {"BTC-USD": 1, "ETH-USD": 1, "SOL-USD": 1}
    runner.close()


def test_runner_writes_combined_state(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner(cfg, market_data=CountingMarketData())
    runner.tick()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["schema"] == "multi-account-v1"
    assert [a["name"] for a in state["accounts"]] == ["trend", "meanrev", "breakout"]
    assert [a["strategy"] for a in state["accounts"]] == [
        "ema_crossover",
        "rsi_mean_reversion",
        "donchian_breakout",
    ]
    assert set(state["products"]) == {"BTC-USD", "ETH-USD", "SOL-USD"}
    total = state["portfolio_total"]
    assert total["starting_cash"] == 30000
    assert "equity" in total and "total_return_pct" in total
    runner.close()


def test_runner_status_per_account(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner(cfg, market_data=CountingMarketData())
    statuses = runner.status()
    assert [s["name"] for s in statuses] == ["trend", "meanrev", "breakout"]
    assert all("equity" in s for s in statuses)
    runner.close()


def test_runner_tags_engines_with_account_name(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner(cfg, market_data=CountingMarketData())
    by_name = {acct.name: eng for acct, eng in runner.engines}
    # Each engine knows its account name and tags notifications with it.
    assert by_name["trend"].config.account_name == "trend"
    assert by_name["breakout"]._notif_prefix() == "[breakout] "
    runner.close()


def test_default_account_has_no_notification_prefix(tmp_path):
    # Back-compat: the synthesized single "default" account keeps the original
    # un-prefixed notification titles.
    p = tmp_path / "c.yaml"
    p.write_text("products: [BTC-USD]\nsentiment_enabled: false\n")
    cfg = Config.load(str(p))
    cfg.dashboard_state_path = str(tmp_path / "state.json")
    runner = Runner(cfg, market_data=CountingMarketData())
    eng = runner.engines[0][1]
    assert eng._notif_prefix() == ""
    runner.close()


def test_runner_cloud_stands_down_when_laptop_active(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.driver_role = "cloud"
    md = CountingMarketData()
    coord = FakeCoordinator(enabled=True, laptop=True)
    runner = Runner(cfg, market_data=md, coordinator=coord)
    trades = runner.tick()
    assert trades == []
    assert md.calls == {}  # no engine ticked
    assert not coord.claimed
    runner.close()
