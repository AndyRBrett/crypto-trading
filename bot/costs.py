"""Transaction-cost economics: is a signal's projected move worth the round trip?

Issue #44. Seven- and ninety-day P&L were both negative on a handful of trades,
which is what a strategy whose edge never cleared its own costs looks like: the
signals were fine, the round trip wasn't. Every entry pays the fee twice (in and
out) plus whatever slippage the fill actually realized, so a projected move
smaller than that is a losing trade the moment it fills, however good the setup.

These are pure functions over a config-like object and plain numbers — the same
shape as ``bot/risk.py`` — so the live engine and the backtester share one
definition of "does this clear costs?" instead of drifting apart.

Costs are measured in basis points of notional (1 bp = 0.01%):

  * fees      — ``fee_rate`` charged on entry *and* exit, so 2 x fee_rate.
  * slippage  — the realized gap between signal price and fill price, logged per
                fill since #28. The median of the recent samples is used (not the
                mean) so one abnormal fill can't move the floor, and it is counted
                twice for the same round-trip reason.

The projected move comes from the risk layer's own take-profit target
(``take_profit_atr_mult * ATR``): the distance the trade is actually managed
toward. ``cost_floor_margin`` is how many times the round-trip cost that move
must cover before an entry is allowed.
"""

from __future__ import annotations

from typing import Sequence

# Multiplier turning a one-way cost into a round trip (enter + exit).
ROUND_TRIP = 2.0


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def median_slippage_bps(samples: Sequence[float] | None) -> float:
    """Median *magnitude* of the recent per-fill slippage samples, in bps.

    Magnitude, because slippage is signed relative to the signal price: a fill
    above the signal price costs a buyer and pays a seller, and the floor is
    about how far fills land from the signal either way. Empty/None -> 0.0, i.e.
    a fresh store's floor is fees-only until fills accumulate.
    """
    usable = [abs(float(s)) for s in (samples or []) if s is not None]
    return _median(usable)


def round_trip_cost_bps(cfg, samples: Sequence[float] | None = None) -> float:
    """Estimated cost of a full round trip in bps: both fees plus both slippages."""
    fee_bps = float(getattr(cfg, "fee_rate", 0.0) or 0.0) * 1e4
    return ROUND_TRIP * (fee_bps + median_slippage_bps(samples))


def expected_edge_bps(cfg, price: float, atr: float | None) -> float:
    """The move this entry is managed toward, in bps of the entry price.

    That is the risk layer's take-profit distance (``take_profit_atr_mult * ATR``),
    the only forward-looking number the engine commits to. Without an ATR there
    is no target at all (``risk.protective_exit_reason`` returns None for the
    take-profit leg), so the fallback applies the configured reward:risk ratio to
    the fallback stop distance — the same trade geometry, expressed in percent.
    """
    if price <= 0:
        return 0.0
    if atr and atr > 0:
        target_dist = float(getattr(cfg, "take_profit_atr_mult", 0.0) or 0.0) * float(atr)
    else:
        stop_mult = float(getattr(cfg, "stop_loss_atr_mult", 0.0) or 0.0)
        tp_mult = float(getattr(cfg, "take_profit_atr_mult", 0.0) or 0.0)
        reward_risk = (tp_mult / stop_mult) if stop_mult > 0 else 0.0
        stop_dist = float(getattr(cfg, "fallback_stop_pct", 0.0) or 0.0) * price
        target_dist = stop_dist * reward_risk
    if target_dist <= 0:
        return 0.0
    return target_dist / price * 1e4


def cost_floor_verdict(
    cfg, price: float, atr: float | None, samples: Sequence[float] | None = None
) -> dict:
    """Decide whether an entry's projected move clears its round-trip cost.

    Returns a dict that is both the decision and the observability record:

        {"blocked": bool, "enabled": bool, "edge_bps": float,
         "cost_bps": float, "required_bps": float, "samples": int}

    ``blocked`` is only ever True when ``cost_floor_enabled`` is set, so the gate
    is inert (but still measured, so its effect can be read off the signal log
    before it is switched on) until an operator turns it on.
    """
    enabled = bool(getattr(cfg, "cost_floor_enabled", False))
    margin = float(getattr(cfg, "cost_floor_margin", 1.0) or 0.0)
    usable = [float(s) for s in (samples or []) if s is not None]
    edge_bps = expected_edge_bps(cfg, price, atr)
    cost_bps = round_trip_cost_bps(cfg, usable)
    required_bps = cost_bps * margin
    return {
        "enabled": enabled,
        "blocked": enabled and edge_bps < required_bps,
        "edge_bps": round(edge_bps, 2),
        "cost_bps": round(cost_bps, 2),
        "required_bps": round(required_bps, 2),
        "samples": len(usable),
    }


def blocks_entry(cfg, price: float, atr: float | None, samples=None) -> bool:
    """Convenience wrapper for callers that only need the yes/no (backtester)."""
    return cost_floor_verdict(cfg, price, atr, samples)["blocked"]
