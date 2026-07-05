"""Post-stop-out re-entry cooldown (reentry_cooldown_bars).

The headline test reproduces, with the real fills from the live long_short
account's trade log, the whipsaw observed on 2026-07-02: the ETH-USD short was
stopped out at $1,698.69 (15:03 UTC) and the level-triggered re-entry re-shorted
at $1,702.08 just 2h21m later — a worse price than the stop that had just fired.
With a one-bar (daily) cooldown that re-entry is blocked; the *next* short the
account actually took (BTC-USD, 31.8h after its own stop-out) stays allowed.

The cooldown is derived from the persisted trade log (no in-memory state), so
it also holds across the fresh-VM-per-tick cloud runs. Off by default:
``reentry_cooldown_bars: 0`` must leave behavior exactly as before.
"""

import time

from bot.engine import ACTED, REENTRY_COOLDOWN
from bot.strategy import BUY, SELL, Signal

from tests.test_engine import FakeStorage, make_engine

# Real fills from trading.long_short.db (bot-state branch, 2026-07-04):
#   open short   2026-06-23 13:34:38  SELL 0.640542 ETH @ 1657.69
#   stop cover   2026-07-02 15:03:31  BUY  0.640542 ETH @ 1698.69  "Stop-loss (short): ..."
#   re-short     2026-07-02 17:24:18  SELL 0.614833 ETH @ 1702.08  <- the whipsaw
OPEN_TS = 1782221678.8523548
STOP_TS = 1783004611.6234436
RESHORT_TS = 1783013058.610126
STOP_REASON = (
    "Stop-loss (short): price $1,698.69 hit stop $1,672.04 (entry $1,657.69) "
    "— cutting the loss / locking in gains."
)


def _engine(cooldown_bars):
    eng = make_engine(
        starting_cash=10_000,
        products=["ETH-USD", "BTC-USD"],
        allow_short=True,
        candle_granularity="ONE_DAY",
        reentry_cooldown_bars=cooldown_bars,
    )
    eng.storage = FakeStorage()
    return eng


def _replay_stopped_short(eng, now):
    """Replay the real ETH short + stop-cover, time-shifted so the stop-cover
    sits exactly as far in the past as it was when the live re-short fired."""
    shift = now - RESHORT_TS  # keeps every relative gap identical to the live log
    eng.portfolio.execute(
        SELL, "ETH-USD", 1657.69, 0.6405423716713418,
        timestamp=OPEN_TS + shift,
        reasons=["Fast EMA(20) below slow EMA(50) (1761.95 < 1909.91) — short the downtrend."],
    )
    eng.portfolio.execute(
        BUY, "ETH-USD", 1698.69, 0.6405423716713418,
        timestamp=STOP_TS + shift,
        reasons=[STOP_REASON],
    )


def _reshort_signal():
    # The exact signal the strategy emitted at 17:24 (indicators abridged; the
    # engine only reads atr for sizing).
    return Signal(
        product_id="ETH-USD",
        action=SELL,
        price=1702.08,
        indicators={"atr": 40.0},
        reasons=["Fast EMA(20) below slow EMA(50) (1661.27 < 1814.33) — short the downtrend."],
    )


def test_one_bar_cooldown_blocks_the_july_2_reshort():
    eng = _engine(cooldown_bars=1)
    now = time.time()
    _replay_stopped_short(eng, now)
    trade, code = eng._manage(
        _reshort_signal(), 1702.08, [], {"ETH-USD": 1702.08}
    )
    # 2h21m after the stop-out is inside the 1-day cooldown: blocked.
    assert trade is None
    assert code == REENTRY_COOLDOWN
    assert eng.portfolio.position("ETH-USD").quantity == 0


def test_cooldown_off_by_default_reproduces_live_behavior():
    # reentry_cooldown_bars defaults to 0: the same re-short goes through,
    # exactly as it did live on Jul 2 — proving the flag changes nothing
    # until it is explicitly enabled.
    eng = _engine(cooldown_bars=0)
    now = time.time()
    _replay_stopped_short(eng, now)
    trade, code = eng._manage(
        _reshort_signal(), 1702.08, [], {"ETH-USD": 1702.08}
    )
    assert code == ACTED
    assert trade is not None and trade.side == SELL
    assert eng.portfolio.position("ETH-USD").quantity < 0


def test_entry_allowed_once_cooldown_expires():
    # The account's OTHER live re-entry (BTC, 31.8h after its own stop-out)
    # must survive a 1-bar daily cooldown: shift the ETH scenario so the
    # stop-cover is 31.8h old — outside the 24h window — and re-enter.
    eng = _engine(cooldown_bars=1)
    now = time.time()
    # _replay_stopped_short places the stop-cover (RESHORT_TS - STOP_TS)
    # seconds before the reference time; walk the reference back so the
    # cover lands 31.8h before now.
    _replay_stopped_short(eng, now - (31.8 * 3600 - (RESHORT_TS - STOP_TS)))
    trade, code = eng._manage(
        _reshort_signal(), 1702.08, [], {"ETH-USD": 1702.08}
    )
    assert code == ACTED
    assert trade is not None


def test_cooldown_applies_to_long_reentries_too():
    eng = make_engine(
        starting_cash=10_000,
        products=["BTC-USD"],
        candle_granularity="ONE_DAY",
        reentry_cooldown_bars=2,
    )
    eng.storage = FakeStorage()
    now = time.time()
    eng.portfolio.execute(BUY, "BTC-USD", 100.0, 1.0, timestamp=now - 90_000)
    eng.portfolio.execute(
        SELL, "BTC-USD", 90.0, 1.0, timestamp=now - 86_400,
        reasons=["Stop-loss: price $90.00 hit stop $90.10 (entry $100.00) — cutting the loss."],
    )
    sig = Signal(product_id="BTC-USD", action=BUY, price=95.0, indicators={"atr": 2.0})
    trade, code = eng._manage(sig, 95.0, [], {"BTC-USD": 95.0})
    # 1 day since the stop, 2-bar (2-day) cooldown: still blocked.
    assert trade is None and code == REENTRY_COOLDOWN


def test_normal_exit_starts_no_cooldown():
    # Only stop exits start a cooldown; a strategy SELL (e.g. RSI reverted to
    # the mean) must not block an immediate re-entry even with the flag on.
    eng = make_engine(
        starting_cash=10_000,
        products=["BTC-USD"],
        candle_granularity="ONE_DAY",
        reentry_cooldown_bars=5,
    )
    eng.storage = FakeStorage()
    now = time.time()
    eng.portfolio.execute(BUY, "BTC-USD", 100.0, 1.0, timestamp=now - 7200)
    eng.portfolio.execute(
        SELL, "BTC-USD", 110.0, 1.0, timestamp=now - 3600,
        reasons=["RSI 60.0 ≥ 55 — reverted to the mean, taking profit."],
    )
    sig = Signal(product_id="BTC-USD", action=BUY, price=108.0, indicators={"atr": 2.0})
    trade, code = eng._manage(sig, 108.0, [], {"BTC-USD": 108.0})
    assert code == ACTED and trade is not None
