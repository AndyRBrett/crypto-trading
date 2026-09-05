"""Rolling-risk circuit breaker: de-risk automatically when the book keeps bleeding.

Issue #45. Telemetry showed 30-day Sharpe -2.54 and Sortino -3.43 against a flat
equity curve with no automatic response: the book was losing slowly and sizing
the next trade exactly as if it were winning. The risk layer sizes off *price*
volatility (ATR) and knows nothing about whether the strategy is actually
working; this module adds the missing feedback loop, scaling size down — or
pausing new entries entirely — while trailing risk-adjusted performance stays
below a floor, and restoring it automatically when performance recovers.

Design notes:

* **Stateless.** The breaker is recomputed each tick from the persisted equity
  curve rather than latched in a flag somewhere, so it survives restarts and the
  fresh-VM-per-tick cloud runs with nothing to persist and nothing to get stuck
  on. It is the same reasoning as the post-stop re-entry cooldown deriving
  itself from the trade log.
* **Consecutive days, not one bad reading.** A single day below the floor is
  noise; the breaker trips only after ``risk_breaker_days`` consecutive daily
  readings breach it. Each reading is the full trailing window (30 days by
  default) evaluated as of that day, so it's the same metric the overseer
  reports, just walked back a day at a time.
* **Both ratios, not either.** A day counts as a breach only when *every*
  computable ratio (Sharpe and Sortino) is at or below its floor. They disagree
  precisely when the losses are all in one tail, which is the case where
  throttling the whole book is the wrong call.
* **Unmeasurable is not a breach.** A day with too little curve to compute a
  ratio breaks the streak rather than extending it: the breaker fires on
  evidence of bleeding, never on the absence of evidence.
* **Entries only.** The multiplier scales new positions. Exits, covers, and
  protective stops never consult it — a throttled book must still be able to
  reduce risk.
"""

from __future__ import annotations

from .metrics import RISK_WINDOW_DAYS, risk_metrics

DAY_SECONDS = 86_400

# What the sizing multiplier is when nothing is throttling: full size.
FULL_SIZE = 1.0


def _floors(cfg) -> dict[str, float]:
    return {
        "sharpe": float(getattr(cfg, "risk_breaker_sharpe_floor", -1.5)),
        "sortino": float(getattr(cfg, "risk_breaker_sortino_floor", -1.5)),
    }


def day_breaches(metrics: dict, floors: dict[str, float]) -> bool:
    """True when every computable ratio in ``metrics`` sits at or below its floor.

    An empty/unmeasurable reading is False — see the module docstring: the
    breaker needs evidence of bleeding, not a gap in the data.
    """
    checked = [
        metrics[name] <= floors[name]
        for name in ("sharpe", "sortino")
        if metrics.get(name) is not None
    ]
    return bool(checked) and all(checked)


def breaker_state(cfg, curve: list[tuple[float, float]], now: float) -> dict:
    """Evaluate the breaker against an equity curve as of ``now``.

    ``curve`` is ``[(timestamp, equity), ...]`` in any order (the same shape
    ``bot.metrics`` takes). Returns the decision *and* the evidence behind it:

        {"enabled": bool, "tripped": bool, "size_multiplier": float,
         "days_breached": int, "days_required": int, "window_days": int,
         "sharpe": float|None, "sortino": float|None, "floors": {...}}

    ``tripped`` is only ever True when ``risk_breaker_enabled`` is set, so the
    breaker is measured (and readable in overseer status) before it is armed.
    """
    enabled = bool(getattr(cfg, "risk_breaker_enabled", False))
    required = max(1, int(getattr(cfg, "risk_breaker_days", 3) or 1))
    window = int(getattr(cfg, "risk_breaker_window_days", RISK_WINDOW_DAYS) or RISK_WINDOW_DAYS)
    floors = _floors(cfg)

    # Walk back one day at a time, newest first, counting the unbroken run of
    # breaching days. Each reading is the whole trailing window as of that day —
    # so the curve is truncated at that day's end as well as clipped at its
    # start, otherwise every "earlier" reading would still see the days that
    # came after it and the streak would be one reading repeated N times.
    ordered = sorted(curve)
    breached = 0
    latest: dict = {}
    for i in range(required):
        as_of = now - i * DAY_SECONDS
        as_of_curve = [(ts, eq) for ts, eq in ordered if ts <= as_of]
        metrics = risk_metrics(as_of_curve, window_days=window, now=as_of)
        if i == 0:
            latest = metrics
        if not day_breaches(metrics, floors):
            break
        breached += 1

    tripped = enabled and breached >= required
    mult = float(getattr(cfg, "risk_breaker_size_mult", 0.5) or 0.0) if tripped else FULL_SIZE
    return {
        "enabled": enabled,
        "tripped": tripped,
        "size_multiplier": max(0.0, mult),
        "days_breached": breached,
        "days_required": required,
        "window_days": window,
        "sharpe": latest.get("sharpe"),
        "sortino": latest.get("sortino"),
        "floors": floors,
    }


def size_multiplier(cfg, curve: list[tuple[float, float]], now: float) -> float:
    """Convenience wrapper for callers that only need the sizing scale.

    Short-circuits when the breaker is disabled: a caller that only wants the
    multiplier gains nothing from measuring a breaker that cannot fire, and the
    backtester asks on every entry bar.
    """
    if not getattr(cfg, "risk_breaker_enabled", False):
        return FULL_SIZE
    return breaker_state(cfg, curve, now)["size_multiplier"]


def describe(state: dict) -> str:
    """One-line human summary, for logs and push notifications."""
    ratios = []
    for name in ("sharpe", "sortino"):
        if state.get(name) is not None:
            ratios.append(f"{name} {state[name]:+.2f}")
    detail = ", ".join(ratios) or "no measurable ratios"
    if not state["tripped"]:
        return (
            f"risk breaker clear ({detail}; {state['days_breached']}/"
            f"{state['days_required']} breaching days)"
        )
    pct = state["size_multiplier"] * 100
    action = "new entries paused" if state["size_multiplier"] <= 0 else f"sizing at {pct:.0f}%"
    return (
        f"risk breaker TRIPPED — {detail} below floor for "
        f"{state['days_breached']} consecutive day(s) over a "
        f"{state['window_days']}-day window; {action}"
    )
