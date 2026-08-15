"""Tests for what the bot notifies about: closes (win or loss) and the heartbeat.

Alerting only on wins and new highs meant a losing streak and a crashed bot
produced identical silence. These pin the behavior that makes the two
distinguishable from the phone.
"""

import json
import math
import time

from bot.config import Config
from bot.engine import Engine
from bot.runner import Runner
from bot.strategy import BUY, SELL


class RecordingNotifier:
    """Stands in for the real Notifier; records what would have been pushed."""

    def __init__(self, enabled=True, ok=True):
        self.enabled = enabled
        self.sent: list[dict] = []
        self.last_error = "" if ok else "HTTP 400: VapidPkHashMismatch"
        self._ok = ok

    def send(self, title, message, tags="", priority="default"):
        self.sent.append({"title": title, "message": message, "priority": priority})
        return self._ok


class FakeStorage:
    def __init__(self):
        self._meta: dict = {}
        self.trades: list = []

    def load_trades(self):
        return []

    def save_trade(self, trade):
        self.trades.append(trade)

    def save_equity(self, *a, **k):
        pass

    def export_state(self, *a, **k):
        pass

    def save_signal(self, *a, **k):
        pass

    def get_meta(self, key):
        return self._meta.get(key)

    def set_meta(self, key, value):
        self._meta[key] = value

    def close(self):
        pass


class FakeExplainer:
    def explain(self, *a, **k):
        return "test explanation"


def _engine(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    eng = Engine(
        cfg, market_data=object(), storage=FakeStorage(), explainer=FakeExplainer()
    )
    eng.notifier = RecordingNotifier()
    return eng


def _round_trip(eng, entry, exit_price, qty=1.0, product="BTC-USD"):
    """Open a long and close it, returning the closing trade."""
    eng.portfolio.execute(BUY, product, entry, qty, timestamp=time.time() - 100)
    trade = eng.portfolio.execute(SELL, product, exit_price, qty)
    return eng._finalize(trade, exit_price)


# --- per-trade alerts -----------------------------------------------------

def test_winning_close_notifies():
    eng = _engine(starting_cash=100_000, fee_rate=0.0)
    _round_trip(eng, entry=1000.0, exit_price=1100.0)
    assert len(eng.notifier.sent) == 1
    assert "Profit" in eng.notifier.sent[0]["title"]
    assert "+100.00" in eng.notifier.sent[0]["title"]


def test_losing_close_notifies_too():
    # The regression that mattered: this used to send nothing at all.
    eng = _engine(starting_cash=100_000, fee_rate=0.0)
    _round_trip(eng, entry=1000.0, exit_price=900.0)
    assert len(eng.notifier.sent) == 1
    assert "Loss" in eng.notifier.sent[0]["title"]
    assert "-100.00" in eng.notifier.sent[0]["title"]


def test_loss_alerts_can_be_switched_off():
    eng = _engine(starting_cash=100_000, fee_rate=0.0, notify_on_loss=False)
    _round_trip(eng, entry=1000.0, exit_price=900.0)
    assert eng.notifier.sent == []


def test_opening_leg_does_not_notify():
    eng = _engine(starting_cash=100_000, fee_rate=0.0)
    trade = eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0)
    eng._finalize(trade, 1000.0)
    assert eng.notifier.sent == []


def test_successful_push_records_the_notify_clock():
    eng = _engine(starting_cash=100_000, fee_rate=0.0)
    _round_trip(eng, entry=1000.0, exit_price=1100.0)
    assert float(eng.storage.get_meta("last_notify_at")) > 0
    assert eng.storage.get_meta("last_push_error") == ""


def test_rejected_push_records_the_error_and_not_the_clock():
    eng = _engine(starting_cash=100_000, fee_rate=0.0)
    eng.notifier = RecordingNotifier(ok=False)
    _round_trip(eng, entry=1000.0, exit_price=1100.0)
    # A rejected push must not look like a delivered one to the heartbeat.
    assert eng.storage.get_meta("last_notify_at") is None
    assert "VapidPkHashMismatch" in eng.storage.get_meta("last_push_error")


# --- heartbeat ------------------------------------------------------------

def _candles(n=300, base=100.0, slope=0.5):
    return [
        {
            "time": 1700000000 + i * 3600,
            "open": (c := base + slope * i + 5 * math.sin(i / 5)) - 1,
            "high": c + 2,
            "low": c - 2,
            "close": c,
            "volume": 10,
        }
        for i in range(n)
    ]


class StubMarketData:
    def get_candles(self, pid, granularity=None, count=None):
        return _candles()

    def get_prices(self, ids):
        return {p: _candles()[-1]["close"] for p in ids}

    def get_price(self, pid):
        return _candles()[-1]["close"]


def _runner(tmp_path, **extra):
    p = tmp_path / "c.yaml"
    p.write_text(
        """
sentiment_enabled: false
explanations_enabled: false
accounts:
  - {name: trend, strategy_type: ema_crossover, products: [BTC-USD], starting_cash: 10000}
  - {name: breakout, strategy_type: donchian_breakout, products: [ETH-USD], starting_cash: 10000}
"""
    )
    cfg = Config.load(str(p))
    cfg.dashboard_state_path = str(tmp_path / "state.json")
    # Keep each account's SQLite store inside tmp_path; the default resolves
    # relative to the CWD, so Runners would resume state left by earlier tests.
    for acct in cfg.accounts:
        acct.db_path = str(tmp_path / f"trading.{acct.name}.db")
    for k, v in extra.items():
        setattr(cfg, k, v)
    runner = Runner(cfg, market_data=StubMarketData())
    runner.notifier = RecordingNotifier()
    return runner


def test_heartbeat_starts_the_clock_instead_of_firing_on_a_fresh_store(tmp_path):
    runner = _runner(tmp_path, heartbeat_days=7)
    runner.tick()
    assert runner.notifier.sent == []
    assert float(runner.engines[0][1].storage.get_meta("last_notify_at")) > 0
    runner.close()


def test_heartbeat_stays_quiet_before_the_interval(tmp_path):
    runner = _runner(tmp_path, heartbeat_days=7)
    runner.engines[0][1].storage.set_meta("last_notify_at", str(time.time() - 2 * 86_400))
    runner.tick()
    assert runner.notifier.sent == []
    runner.close()


def test_heartbeat_fires_after_a_long_silence(tmp_path):
    runner = _runner(tmp_path, heartbeat_days=7)
    runner.engines[0][1].storage.set_meta("last_notify_at", str(time.time() - 9 * 86_400))
    runner.tick()

    assert len(runner.notifier.sent) == 1
    msg = runner.notifier.sent[0]
    assert "alive" in msg["title"].lower()
    # It must carry enough state to be worth the interruption.
    assert "trend:" in msg["message"] and "breakout:" in msg["message"]
    runner.close()


def _quiet_runner_with_open_position(tmp_path):
    """Put one account into a known state directly and re-arm the heartbeat clock.

    Deliberately does NOT tick: an earlier version relied on the strategy leaving
    a position open after one tick against synthetic candles, which made these
    tests hostage to unrelated strategy changes (they broke when main retuned the
    RSI exit). What's under test is how the heartbeat *renders* a book, so the
    book is constructed rather than traded into.
    """
    runner = _runner(tmp_path, heartbeat_days=7)
    eng = runner.engines[0][1]
    eng.last_prices = {"BTC-USD": 110.0}
    eng.portfolio.execute(BUY, "BTC-USD", 100.0, 1.0)
    eng.storage.set_meta("last_notify_at", str(time.time() - 9 * 86_400))
    return runner, eng


def test_heartbeat_reports_open_positions_and_equity(tmp_path):
    runner, _ = _quiet_runner_with_open_position(tmp_path)
    runner._maybe_heartbeat()

    msg = runner.notifier.sent[0]["message"]
    assert "1 open" in msg
    assert "equity unavailable" not in msg
    runner.close()


def test_heartbeat_flags_stale_prices_instead_of_reporting_a_false_crash(tmp_path):
    # An unpriced open position makes market_value() read as zero; the heartbeat
    # must say "unavailable" rather than announce an equity collapse.
    runner, eng = _quiet_runner_with_open_position(tmp_path)
    eng.last_prices.pop("BTC-USD", None)
    runner._maybe_heartbeat()

    msg = runner.notifier.sent[0]["message"]
    assert "equity unavailable" in msg
    assert "BTC-USD" in msg
    runner.close()


def test_heartbeat_can_be_disabled(tmp_path):
    runner = _runner(tmp_path, heartbeat_days=0)
    runner.engines[0][1].storage.set_meta("last_notify_at", str(time.time() - 90 * 86_400))
    runner.tick()
    assert runner.notifier.sent == []
    runner.close()


def test_failed_heartbeat_leaves_the_clock_alone_so_it_retries(tmp_path):
    runner = _runner(tmp_path, heartbeat_days=7)
    runner.notifier = RecordingNotifier(ok=False)
    stale = time.time() - 9 * 86_400
    runner.engines[0][1].storage.set_meta("last_notify_at", str(stale))
    runner.tick()

    meta = runner.engines[0][1].storage
    assert abs(float(meta.get_meta("last_notify_at")) - stale) < 1
    assert "VapidPkHashMismatch" in meta.get_meta("last_push_error")
    runner.close()


# --- key publication ------------------------------------------------------

def test_state_json_publishes_the_matching_vapid_public_key(tmp_path):
    from bot.notifier import derive_public_key
    from tests.test_notifier import _keypair

    private_b64, public_b64 = _keypair()
    runner = _runner(tmp_path, vapid_private_key=private_b64)
    runner.tick()

    state = json.loads((tmp_path / "state.json").read_text())
    # The dashboard subscribes with this; it must match what the bot signs with.
    assert state["vapid_public_key"] == public_b64 == derive_public_key(private_b64)
    runner.close()


def test_state_json_omits_the_key_when_push_is_not_configured(tmp_path):
    runner = _runner(tmp_path)
    runner.tick()
    state = json.loads((tmp_path / "state.json").read_text())
    assert "vapid_public_key" not in state
    runner.close()


def test_config_warning_recorded_when_sentiment_key_is_missing(tmp_path):
    runner = _runner(tmp_path, sentiment_enabled=True, anthropic_api_key="")
    runner.tick()
    warnings = runner.engines[0][1].storage.get_meta("config_warnings")
    assert "ANTHROPIC_API_KEY" in warnings
    runner.close()
