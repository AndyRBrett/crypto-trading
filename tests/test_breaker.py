"""Tests for the rolling-risk circuit breaker (issue #45).

Covers the pure evaluation (bot/breaker.py), the sizing multiplier in
bot/risk.py, the engine wiring (throttle, pause, exits untouched, transition
recorded), and the overseer status surface.
"""

import time

from bot import breaker, risk
from bot.config import Config
from bot.engine import ACTED, RISK_BREAKER, Engine
from bot.metrics import risk_metrics
from bot.strategy import BUY, SELL, Signal

from tests.test_engine import FakeExplainer, FakeStorage

DAY = 86_400


def _cfg(**overrides):
    cfg = Config()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _curve(daily_returns, end: float, start_equity: float = 10_000.0):
    """An equity curve with one snapshot per UTC day ending at ``end``."""
    out = []
    equity = start_equity
    n = len(daily_returns)
    for i, r in enumerate(daily_returns):
        equity *= 1 + r
        out.append((end - (n - 1 - i) * DAY, equity))
    return out


# A steady bleed: 40 days of small losses. Sharpe/Sortino are deeply negative
# and stay that way as the window is walked back day by day.
BLEED = [-0.004] * 40
# The same book recovering: steady gains put both ratios above any negative floor.
RECOVERY = [0.004] * 40


def test_day_breaches_requires_every_computable_ratio():
    floors = {"sharpe": -1.5, "sortino": -1.5}
    assert breaker.day_breaches({"sharpe": -2.0, "sortino": -3.0}, floors) is True
    # Sortino above its floor -> the losses aren't uniformly bad; not a breach.
    assert breaker.day_breaches({"sharpe": -2.0, "sortino": -0.5}, floors) is False
    # Exactly at the floor counts as breaching (floors are inclusive).
    assert breaker.day_breaches({"sharpe": -1.5, "sortino": -1.5}, floors) is True
    # Nothing computable -> no evidence -> not a breach.
    assert breaker.day_breaches({}, floors) is False
    assert breaker.day_breaches({"max_drawdown_pct": 12.0}, floors) is False


def test_trips_after_consecutive_breaching_days():
    now = time.time()
    curve = _curve(BLEED, end=now)
    cfg = _cfg(risk_breaker_enabled=True, risk_breaker_days=3, risk_breaker_size_mult=0.5)
    state = breaker.breaker_state(cfg, curve, now)
    # Sanity: the fixture really is a deeply negative book.
    assert risk_metrics(curve, now=now)["sharpe"] < -1.5
    assert state["tripped"] is True
    assert state["days_breached"] == 3 and state["days_required"] == 3
    assert state["size_multiplier"] == 0.5


def test_recovery_clears_the_breaker_without_any_stored_state():
    now = time.time()
    cfg = _cfg(risk_breaker_enabled=True, risk_breaker_days=3)
    assert breaker.breaker_state(cfg, _curve(RECOVERY, end=now), now)["tripped"] is False
    # Nothing is latched: the same config on a bleeding curve trips, and on a
    # recovering one it doesn't, purely from the curve.
    assert breaker.breaker_state(cfg, _curve(BLEED, end=now), now)["tripped"] is True


def test_measured_but_inert_until_enabled():
    now = time.time()
    state = breaker.breaker_state(_cfg(risk_breaker_days=3), _curve(BLEED, end=now), now)
    assert state["enabled"] is False and state["tripped"] is False
    assert state["size_multiplier"] == 1.0
    # The evidence is still reported, which is what makes it readable before arming.
    assert state["days_breached"] == 3 and state["sharpe"] < -1.5


def test_flat_curve_never_trips():
    """No dispersion -> no Sharpe -> no evidence. Absence is not a breach."""
    now = time.time()
    flat = [(now - i * DAY, 10_000.0) for i in range(40)]
    cfg = _cfg(risk_breaker_enabled=True, risk_breaker_days=3)
    state = breaker.breaker_state(cfg, flat, now)
    assert state["tripped"] is False and state["days_breached"] == 0


def test_a_higher_day_requirement_needs_a_longer_streak():
    now = time.time()
    # Two bad days at the end of an otherwise fine book: a 2-day breaker trips,
    # a 5-day one does not.
    mixed = _curve([0.004] * 38 + [-0.30, -0.30], end=now)
    trips = breaker.breaker_state(
        _cfg(risk_breaker_enabled=True, risk_breaker_days=2), mixed, now
    )
    holds = breaker.breaker_state(
        _cfg(risk_breaker_enabled=True, risk_breaker_days=5), mixed, now
    )
    assert trips["tripped"] is True
    assert holds["tripped"] is False and holds["days_breached"] < 5


def test_size_multiplier_short_circuits_when_disabled():
    now = time.time()
    assert breaker.size_multiplier(_cfg(), _curve(BLEED, end=now), now) == 1.0
    assert breaker.size_multiplier(
        _cfg(risk_breaker_enabled=True, risk_breaker_size_mult=0.25),
        _curve(BLEED, end=now), now,
    ) == 0.25


# -- sizing -----------------------------------------------------------------


def test_position_size_scales_and_clamps():
    cfg = _cfg(starting_cash=10_000)
    full = risk.position_size(cfg, 10_000, 10_000, price=1000.0, atr=50.0)
    half = risk.position_size(cfg, 10_000, 10_000, price=1000.0, atr=50.0, size_mult=0.5)
    assert abs(half - full / 2) < 1e-9
    # A multiplier can only ever reduce size.
    assert risk.position_size(cfg, 10_000, 10_000, 1000.0, 50.0, size_mult=3.0) == full
    # Throttled into dust -> skipped rather than filled as a token trade.
    assert risk.position_size(cfg, 10_000, 10_000, 1000.0, 50.0, size_mult=0.001) == 0.0


# -- engine wiring ----------------------------------------------------------


class CurveStorage(FakeStorage):
    """FakeStorage backed by a canned equity curve."""

    def __init__(self, curve=()):
        super().__init__()
        self.curve = list(curve)

    def load_equity_curve(self, limit=500):
        return [{"timestamp": ts, "equity": eq} for ts, eq in self.curve][-limit:]

    def recent_slippage_bps(self, product_id, limit=20):
        return []


def _engine(storage=None, **cfg_overrides):
    return Engine(
        _cfg(**cfg_overrides),
        market_data=object(),
        storage=storage or CurveStorage(),
        explainer=FakeExplainer(),
    )


def _signal(action, price=1000.0, atr=50.0, product="BTC-USD"):
    return Signal(
        product_id=product, action=action, price=price,
        indicators={"atr": atr}, reasons=["test reason"],
    )


def test_engine_halves_size_while_tripped():
    now = time.time()
    storage = CurveStorage(_curve(BLEED, end=now))
    eng = _engine(storage, starting_cash=10_000, risk_breaker_enabled=True,
                  risk_breaker_days=3, risk_breaker_size_mult=0.5)
    baseline = _engine(CurveStorage(_curve(BLEED, end=now)), starting_cash=10_000)
    baseline._refresh_breaker()
    full = baseline._position_size(price=1000.0, atr=50.0, prices={})

    eng._refresh_breaker()
    assert eng._breaker["tripped"] is True
    assert abs(eng._position_size(price=1000.0, atr=50.0, prices={}) - full / 2) < 1e-9
    # The signal still trades — throttled, not blocked.
    trade, code = eng._manage(_signal(BUY), 1000.0, [], prices={})
    assert trade is not None and code == ACTED
    assert abs(trade.quantity - full / 2) < 1e-9


def test_engine_pauses_entries_at_zero_multiplier():
    now = time.time()
    eng = _engine(CurveStorage(_curve(BLEED, end=now)), starting_cash=10_000,
                  risk_breaker_enabled=True, risk_breaker_days=3,
                  risk_breaker_size_mult=0.0)
    eng._refresh_breaker()
    trade, code = eng._manage(_signal(BUY), 1000.0, [], prices={})
    assert trade is None and code == RISK_BREAKER
    # Shorts are paused on the same terms.
    eng.config.allow_short = True
    trade, code = eng._manage(_signal(SELL), 1000.0, [], prices={})
    assert trade is None and code == RISK_BREAKER


def test_engine_never_throttles_an_exit():
    """A throttled book must still be able to reduce risk."""
    now = time.time()
    eng = _engine(CurveStorage(_curve(BLEED, end=now)), starting_cash=10_000,
                  risk_breaker_enabled=True, risk_breaker_days=3,
                  risk_breaker_size_mult=0.0, trailing_stop=False)
    eng.portfolio.execute(BUY, "BTC-USD", 1000.0, 1.0, timestamp=now - 100)
    eng._refresh_breaker()
    trade, code = eng._manage(_signal(SELL), 1000.0, [], prices={})
    assert trade is not None and code == ACTED
    assert trade.quantity == 1.0  # the whole position, unscaled


def test_engine_records_the_state_transition():
    now = time.time()
    storage = CurveStorage(_curve(BLEED, end=now))
    eng = _engine(storage, starting_cash=10_000, risk_breaker_enabled=True,
                  risk_breaker_days=3)
    eng._refresh_breaker()
    assert storage.get_meta("risk_breaker_tripped") == "1"
    assert float(storage.get_meta("risk_breaker_changed_at")) > 0
    # Recovery clears it back out.
    storage.curve = _curve(RECOVERY, end=now)
    eng._refresh_breaker()
    assert storage.get_meta("risk_breaker_tripped") == ""


def test_engine_survives_an_unreadable_equity_curve():
    """A broken curve read must never be the reason a tick dies."""
    class Broken(CurveStorage):
        def load_equity_curve(self, limit=500):
            raise RuntimeError("db is locked")

    eng = _engine(Broken(), starting_cash=10_000, risk_breaker_enabled=True)
    eng._refresh_breaker()  # no exception
    assert eng._breaker["size_multiplier"] == 1.0
    trade, code = eng._manage(_signal(BUY), 1000.0, [], prices={})
    assert trade is not None and code == ACTED


def test_backtest_shares_the_breaker():
    """The backtester throttles on the same rule, so a sweep measures it."""
    from bot.backtest import run_backtest
    from bot.strategies import make_strategy

    # A trend the regime strategy rides, then a saw-tooth around its trend MA so
    # it exits and re-enters repeatedly — the equity curve moves, which is what
    # gives the breaker something to measure.
    closes = [1000.0 + i for i in range(240)]
    for _ in range(6):
        for _ in range(25):  # slide below the MA -> exit
            closes.append(closes[-1] * 0.995)
        for _ in range(25):  # climb back above it -> re-enter
            closes.append(closes[-1] * 1.007)
    candles = [
        {"time": i * DAY, "open": c, "high": c * 1.01, "low": c * 0.99,
         "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]

    def _run(cfg):
        return run_backtest(make_strategy("regime", cfg.strategy), candles, cfg,
                            product_id="BTC-USD")

    base = _run(_cfg())
    assert base.trades, "baseline should trade this tape"
    # Floors set above any achievable ratio make every measurable day a breach,
    # so the breaker pauses entries as soon as there is a curve to judge.
    throttled = _run(_cfg(
        risk_breaker_enabled=True, risk_breaker_days=1, risk_breaker_size_mult=0.0,
        risk_breaker_sharpe_floor=1e9, risk_breaker_sortino_floor=1e9,
    ))
    # Paused entries can only remove trades, never add them.
    assert len(throttled.trades) < len(base.trades)


def test_overseer_reports_tripped_accounts(tmp_path, monkeypatch):
    import write_status
    from bot.storage import Storage

    monkeypatch.chdir(tmp_path)
    st = Storage("trading.regime.db")
    st.save_equity(10_000.0, 0.0, 10_000.0)
    st.set_meta("risk_breaker_tripped", "1")
    st.set_meta("risk_breaker_changed_at", str(time.time()))
    st.close()
    quiet = Storage("trading.momo.db")
    quiet.save_equity(10_000.0, 0.0, 10_000.0)
    quiet.close()

    status = write_status.collect_metrics()
    assert status["risk_breaker"]["tripped_accounts"] == ["regime"]
    assert status["risk_breaker"]["since"]["regime"].endswith("Z")


def test_overseer_omits_the_block_when_nothing_is_throttled(tmp_path, monkeypatch):
    import write_status
    from bot.storage import Storage

    monkeypatch.chdir(tmp_path)
    st = Storage("trading.regime.db")
    st.save_equity(10_000.0, 0.0, 10_000.0)
    st.close()
    assert "risk_breaker" not in write_status.collect_metrics()


def test_breaker_overrides_resolve_per_account(tmp_path):
    from bot.runner import Runner

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
risk_breaker_enabled: true
risk_breaker_size_mult: 0.5
accounts:
  - name: cautious
    products: [BTC-USD]
    risk_breaker_size_mult: 0.0
  - name: inherits
    products: [ETH-USD]
"""
    )
    cfg = Config.load(str(cfg_file))
    cautious, inherits = cfg.accounts
    resolve = Runner.__dict__["_account_config"]
    stub = type("S", (), {"config": cfg})()
    assert resolve(stub, cautious).risk_breaker_size_mult == 0.0
    assert resolve(stub, inherits).risk_breaker_size_mult == 0.5
    assert resolve(stub, inherits).risk_breaker_enabled is True
