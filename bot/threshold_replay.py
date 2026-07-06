"""What-if threshold replay over the logged signal decisions.

The activity log (``signal_log``, see bot/storage.py) records every evaluated
signal — including the ones that never traded — with a ``features`` snapshot:
the raw indicator values (RSI, ADX, the MAs, Donchian bands, ATR, sentiment)
and the signed distance to each decision threshold at that instant. That is
exactly enough to answer "what if the threshold had been X?": re-evaluate each
logged tick's entry gates against a candidate value, simulate the hypothetical
entries/exits over the logged price path, and compare the resulting P&L to
buying and holding the same capital from the same moments.

This is a decision-support tool, not an auto-tuner: it reports the sweep and
recommends nothing on its own, and it never touches the live config.

Honest limits, so the output is read for what it is:

* It replays only ticks the bot actually evaluated and logged with a features
  snapshot (rows before that column shipped are skipped) — it cannot invent
  signals between ticks.
* The price path is the logged per-tick price (the forming bar's close), so
  intra-tick highs/lows are invisible: protective stops and trailing stops
  fire on the next logged tick after the level is breached, slightly later
  than the live engine would.
* Indicator values are frozen as logged; a different threshold would not have
  changed RSI/ADX/the MAs themselves, only the decisions — which is exactly
  the counterfactual being measured. What it can NOT capture: a different
  position history feeding back into sizing or cooldowns.
* Entries use a fixed notional per trade so P&L is comparable across
  threshold values, rather than compounding a path-dependent equity.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .risk import protective_exit_reason

# Fallback notional per hypothetical entry (USD). Fixed sizing keeps the sweep
# rows comparable: twice the entries means twice the deployed capital, not a
# compounding artifact.
DEFAULT_NOTIONAL = 1_000.0

# Default sweep grids per strategy. Each axis is ONE parameter swept while the
# others stay at the account's configured value. ``trend_buffer_pct`` /
# ``entry_buffer_pct`` are *virtual* relaxation knobs (0 = the current strict
# gate): they widen a price gate by a percentage instead of moving a config
# scalar, because the gate level (trend MA / channel high) moves with price.
DEFAULT_SWEEPS: dict[str, dict[str, list[float]]] = {
    "ema_crossover": {
        "adx_min": [0, 5, 10, 15, 20, 25],
        "rsi_overbought": [70, 75, 80, 85, 90, 100],
        "trend_buffer_pct": [0, 5, 10, 15, 20, 25, 30],
    },
    "rsi_mean_reversion": {
        "rsi_mr_oversold": [25, 30, 35, 40, 45, 50],
        "rsi_mr_overbought": [45, 50, 55, 60, 65],
    },
    "donchian_breakout": {
        "entry_buffer_pct": [0, 1, 2, 3, 5, 8, 10, 13],
    },
    "regime": {
        "trend_buffer_pct": [0, 5, 10, 15, 20, 25, 30],
    },
}

# Sentiment gating mirrored from strategy.apply_sentiment: strategies that fold
# sentiment in have BUYs vetoed at/below the veto score. The regime filter is
# price-only by design and skips it.
_SENTIMENT_GATED = {"ema_crossover", "rsi_mean_reversion", "donchian_breakout"}


@dataclass
class LoggedTick:
    """One evaluated signal from ``signal_log`` (feature-tagged rows only)."""

    timestamp: float
    product_id: str
    action: str
    price: float
    outcome: str
    reject_code: str
    indicators: dict
    thresholds: dict


def load_ticks(db_path: str) -> list[LoggedTick]:
    """Read the feature-tagged activity log, oldest first.

    Rows without a features snapshot (written before the column shipped, or
    the strategy's "not enough data yet" HOLDs) carry no indicator state to
    re-evaluate, so they are skipped.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT timestamp, product_id, action, price, outcome, reject_code, "
            "features FROM signal_log WHERE features NOT IN ('{}', '') ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        return []  # pre-features store: nothing replayable
    finally:
        conn.close()
    ticks = []
    for r in rows:
        try:
            feats = json.loads(r["features"])
        except (ValueError, TypeError):
            continue
        ticks.append(
            LoggedTick(
                timestamp=r["timestamp"],
                product_id=r["product_id"],
                action=r["action"],
                price=r["price"],
                outcome=r["outcome"] or "",
                reject_code=r["reject_code"] or "",
                indicators=feats.get("indicators") or {},
                thresholds=feats.get("thresholds") or {},
            )
        )
    return ticks


# -- per-strategy entry/exit rules -------------------------------------------
#
# Each rule evaluates one logged tick's indicator snapshot against a full
# parameter dict (the account's configured values with the swept one
# overridden). ``entry`` answers "would this tick have fired a long entry?",
# ``exit`` answers "would it have closed one?". Missing optional indicators
# (a disabled filter never logs its value) mean that gate passes, matching the
# live strategies; missing *required* ones mean no decision.


def _sentiment_blocks_buy(ind: dict, p: dict) -> bool:
    score = ind.get("sentiment_score")
    return score is not None and score <= p.get("sentiment_buy_veto", -0.4)


def _ema_entry(ind: dict, price: float, p: dict) -> bool:
    fast, slow, rsi = ind.get("fast_sma"), ind.get("slow_sma"), ind.get("rsi")
    if fast is None or slow is None or rsi is None:
        return False
    if not (fast > slow and rsi < p["rsi_overbought"]):
        return False
    trend_ma = ind.get("trend_ma")
    if trend_ma is not None and price <= trend_ma * (1 - p["trend_buffer_pct"] / 100):
        return False
    adx = ind.get("adx")
    if adx is not None and adx < p["adx_min"]:
        return False
    return not _sentiment_blocks_buy(ind, p)


def _ema_exit(ind: dict, price: float, p: dict) -> bool:
    fast, slow, rsi = ind.get("fast_sma"), ind.get("slow_sma"), ind.get("rsi")
    if fast is None or slow is None or rsi is None:
        return False
    return fast < slow or rsi >= p["rsi_overbought"]


def _mr_entry(ind: dict, price: float, p: dict) -> bool:
    rsi = ind.get("rsi")
    if rsi is None or rsi > p["rsi_mr_oversold"]:
        return False
    return not _sentiment_blocks_buy(ind, p)


def _mr_exit(ind: dict, price: float, p: dict) -> bool:
    rsi = ind.get("rsi")
    return rsi is not None and rsi >= p["rsi_mr_overbought"]


def _donchian_entry(ind: dict, price: float, p: dict) -> bool:
    upper = ind.get("donchian_upper")
    if upper is None or price <= upper * (1 - p["entry_buffer_pct"] / 100):
        return False
    return not _sentiment_blocks_buy(ind, p)


def _donchian_exit(ind: dict, price: float, p: dict) -> bool:
    lower = ind.get("donchian_lower")
    return lower is not None and price < lower


def _regime_entry(ind: dict, price: float, p: dict) -> bool:
    trend_ma = ind.get("trend_ma")
    # Entry and exit share one buffered gate so a relaxed entry doesn't flap
    # against the strict exit on the very next tick.
    return trend_ma is not None and price > trend_ma * (1 - p["trend_buffer_pct"] / 100)


def _regime_exit(ind: dict, price: float, p: dict) -> bool:
    trend_ma = ind.get("trend_ma")
    return trend_ma is not None and price < trend_ma * (1 - p["trend_buffer_pct"] / 100)


_RULES: dict[str, tuple] = {
    "ema_crossover": (_ema_entry, _ema_exit),
    "rsi_mean_reversion": (_mr_entry, _mr_exit),
    "donchian_breakout": (_donchian_entry, _donchian_exit),
    "regime": (_regime_entry, _regime_exit),
}
# Virtual relaxation knobs default to 0 (= the current strict gate).
_VIRTUAL_PARAM_DEFAULTS = {"trend_buffer_pct": 0.0, "entry_buffer_pct": 0.0}


def supported_strategies() -> list[str]:
    return sorted(_RULES)


def build_params(strategy_cfg, overrides: dict | None = None) -> dict:
    """Full rule-parameter dict: the account's StrategyConfig values plus the
    virtual buffer knobs, with ``overrides`` (the swept value) applied last."""
    params = {
        "rsi_overbought": strategy_cfg.rsi_overbought,
        "adx_min": strategy_cfg.adx_min,
        "rsi_mr_oversold": strategy_cfg.rsi_mr_oversold,
        "rsi_mr_overbought": strategy_cfg.rsi_mr_overbought,
        "sentiment_buy_veto": strategy_cfg.sentiment_buy_veto,
        **_VIRTUAL_PARAM_DEFAULTS,
    }
    params.update(overrides or {})
    return params


# -- the replay itself --------------------------------------------------------


@dataclass
class HypotheticalTrade:
    """One simulated round trip (or a still-open position marked at the end)."""

    product_id: str
    entry_ts: float
    entry_price: float
    exit_ts: float | None
    exit_price: float
    quantity: float
    pnl: float  # net of both legs' fees
    exit_reason: str  # "strategy_exit" / a risk.* stop reason / "end_of_data"


@dataclass
class ReplayOutcome:
    """One sweep row: what one candidate threshold value would have produced."""

    param: str
    value: float
    ticks: int  # feature-tagged ticks replayed
    triggers: int  # ticks where the entry rule fired (regardless of position)
    entries: int  # hypothetical entries taken (one open position per product)
    closed: int  # completed round trips
    wins: int  # round trips with positive net P&L
    open_at_end: int
    deployed: float  # sum of entry notionals
    fees: float
    strategy_pnl: float  # net incl. marking open positions at the last price
    buy_hold_pnl: float  # same notional bought at the same entries, held to the end
    trades: list[HypotheticalTrade] = field(default_factory=list)

    @property
    def strategy_return_pct(self) -> float:
        return self.strategy_pnl / self.deployed * 100 if self.deployed else 0.0

    @property
    def buy_hold_return_pct(self) -> float:
        return self.buy_hold_pnl / self.deployed * 100 if self.deployed else 0.0

    @property
    def alpha_pct(self) -> float:
        """Strategy-vs-buy-hold spread on the same deployed notional — the same
        convention as write_status.py's benchmark block."""
        return (
            (self.strategy_pnl - self.buy_hold_pnl) / self.deployed * 100
            if self.deployed
            else 0.0
        )

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.closed if self.closed else None


def replay(
    ticks: list[LoggedTick],
    strategy_type: str,
    strategy_cfg,
    risk_cfg,
    param: str,
    value: float,
    notional: float = DEFAULT_NOTIONAL,
) -> ReplayOutcome:
    """Re-run the logged decisions with ``param`` set to ``value``.

    Simulates one hypothetical long-only book per product, starting flat:
    enter when the (re-thresholded) entry rule fires, exit on the engine's
    protective stops (reusing risk.protective_exit_reason with the ATR logged
    at entry) or the strategy's exit rule, and mark any still-open position at
    the final logged price. The buy-hold benchmark buys the same notional at
    the same entry ticks and holds to the end, fees on both legs either way.
    """
    if strategy_type not in _RULES:
        raise ValueError(
            f"no replay rules for strategy_type {strategy_type!r}; "
            f"supported: {supported_strategies()}"
        )
    entry_rule, exit_rule = _RULES[strategy_type]
    params = build_params(strategy_cfg, {param: value})
    fee_rate = risk_cfg.fee_rate

    by_product: dict[str, list[LoggedTick]] = {}
    for t in ticks:
        by_product.setdefault(t.product_id, []).append(t)

    outcome = ReplayOutcome(
        param=param, value=value, ticks=len(ticks), triggers=0, entries=0,
        closed=0, wins=0, open_at_end=0, deployed=0.0, fees=0.0,
        strategy_pnl=0.0, buy_hold_pnl=0.0,
    )

    for product_id, series in by_product.items():
        pos = None  # (entry_ts, entry_price, qty, atr_at_entry, highs_since_entry)
        bh_entries: list[tuple[float, float]] = []  # (entry_price, qty) for the benchmark
        last_price = series[-1].price

        def _close(tick_ts, price, reason):
            nonlocal pos
            entry_ts, entry_price, qty, _atr, _highs = pos
            exit_fee = qty * price * fee_rate
            pnl = qty * (price - entry_price) - notional * fee_rate - exit_fee
            outcome.fees += notional * fee_rate + exit_fee
            outcome.strategy_pnl += pnl
            outcome.trades.append(
                HypotheticalTrade(
                    product_id=product_id, entry_ts=entry_ts, entry_price=entry_price,
                    exit_ts=tick_ts, exit_price=price, quantity=qty, pnl=pnl,
                    exit_reason=reason,
                )
            )
            pos = None
            return pnl

        for tick in series:
            price = tick.price
            ind = tick.indicators

            if entry_rule(ind, price, params):
                outcome.triggers += 1

            if pos is not None:
                pos[4].append(price)  # highs-since-entry (tick closes; see module doc)
                stop = protective_exit_reason(
                    risk_cfg, pos[1], price, pos[3], highs_since_entry=pos[4]
                )
                if stop is not None:
                    pnl = _close(tick.timestamp, price, stop.split(":")[0])
                    outcome.closed += 1
                    if pnl > 0:
                        outcome.wins += 1
                    continue
                if exit_rule(ind, price, params):
                    pnl = _close(tick.timestamp, price, "strategy_exit")
                    outcome.closed += 1
                    if pnl > 0:
                        outcome.wins += 1
                continue

            if price > 0 and entry_rule(ind, price, params):
                qty = notional / price
                pos = [tick.timestamp, price, qty, ind.get("atr"), [price]]
                outcome.entries += 1
                outcome.deployed += notional
                bh_entries.append((price, qty))

        if pos is not None:
            outcome.open_at_end += 1
            _close(series[-1].timestamp, last_price, "end_of_data")

        # Benchmark: the same capital, bought at the same moments, just held.
        for entry_price, qty in bh_entries:
            bh_exit_fee = qty * last_price * fee_rate
            outcome.buy_hold_pnl += (
                qty * (last_price - entry_price) - notional * fee_rate - bh_exit_fee
            )

    return outcome


def sweep_param(
    ticks: list[LoggedTick],
    strategy_type: str,
    strategy_cfg,
    risk_cfg,
    param: str,
    values: list[float],
    notional: float = DEFAULT_NOTIONAL,
) -> list[ReplayOutcome]:
    """Replay every candidate value of one parameter, in the given order."""
    return [
        replay(ticks, strategy_type, strategy_cfg, risk_cfg, param, v, notional)
        for v in values
    ]
