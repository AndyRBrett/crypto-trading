"""Pure risk-management formulas: position sizing and protective exits.

Extracted from the engine so the live trader and the backtester share one
source of truth — a backtest that sized or stopped differently from production
would measure the wrong thing. These functions take a config-like object (any
object exposing the risk fields) plus plain numbers, so they're trivial to test
and to call from both the engine and ``bot/backtest.py``.
"""

from __future__ import annotations

from typing import Sequence

# Notional below which a trade is ignored as dust (USD).
DUST_NOTIONAL = 10.0

# Every stop exit reason starts with this (long: "Stop-loss:", short:
# "Stop-loss (short):"), including trailing-stop exits — it's the contract the
# re-entry cooldown keys off, so keep protective_exit_reason in sync.
STOP_REASON_PREFIX = "Stop-loss"


def reentry_cooldown_active(
    cfg, trades, product_id: str, now: float, bar_seconds: float
) -> bool:
    """True while a product is inside the post-stop-out re-entry cooldown.

    After a protective stop closes a position, level-triggered re-entry
    (``allow_trend_reentry``) re-arms on the very next tick — observed live as
    a stop-out followed by a re-entry at a *worse* price two hours later. With
    ``cfg.reentry_cooldown_bars > 0``, new entries in that product are blocked
    until that many bars have passed since the stop exit.

    Derived purely from the persisted trade log (the most recent fill for the
    product being a stop exit inside the window), so it survives restarts and
    the fresh-VM-per-tick cloud runs — no extra state to persist. A normal
    exit (take-profit / strategy SELL) resets nothing: only stops start a
    cooldown. Disabled by default (``reentry_cooldown_bars = 0``).
    """
    bars = getattr(cfg, "reentry_cooldown_bars", 0) or 0
    if bars <= 0 or not bar_seconds:
        return False
    last = None
    for t in trades:
        if t.product_id == product_id and (last is None or t.timestamp > last.timestamp):
            last = t
    if last is None or not last.reasons:
        return False
    if not str(last.reasons[0]).startswith(STOP_REASON_PREFIX):
        return False
    return (now - last.timestamp) < bars * bar_seconds


def position_size(
    cfg,
    equity: float,
    cash: float,
    price: float,
    atr: float | None,
    direction: str = "long",
) -> float:
    """Volatility-based size so the stop distance risks ~``risk_per_trade_pct``.

    Bounded by the per-position equity cap and (for longs) available cash.
    Returns 0 for non-positive inputs or dust-sized trades. ``direction`` selects
    long vs short: a short is opened with a SELL that *credits* cash, so the
    cash bound doesn't apply — only the risk and equity-cap bounds do.
    """
    if price <= 0 or equity <= 0:
        return 0.0
    stop_dist = cfg.stop_loss_atr_mult * atr if (atr and atr > 0) else price * cfg.fallback_stop_pct
    if stop_dist <= 0:
        return 0.0
    qty_by_risk = (equity * cfg.risk_per_trade_pct) / stop_dist
    qty_by_cap = (equity * cfg.max_position_pct) / price
    if direction == "short":
        qty = min(qty_by_risk, qty_by_cap)
    else:
        qty_by_cash = (cash * 0.999) / (price * (1 + cfg.fee_rate))
        qty = min(qty_by_risk, qty_by_cap, qty_by_cash)
    if qty * price < DUST_NOTIONAL:
        return 0.0
    return qty


def protective_exit_reason(
    cfg,
    entry: float,
    price: float,
    atr: float | None,
    highs_since_entry: Sequence[float] | None = None,
    lows_since_entry: Sequence[float] | None = None,
    direction: str = "long",
) -> str | None:
    """Return an exit reason if a stop / take-profit / trailing level is breached.

    For a long the stop sits *below* entry and the target *above*; for a short
    they invert. The Chandelier trailing stop rides the highest high since entry
    (long) or the lowest low since entry (short) — pass an empty/None sequence to
    disable trailing. ``direction`` is ``"long"`` or ``"short"``.
    """
    if direction == "short":
        if atr and atr > 0:
            stop = entry + cfg.stop_loss_atr_mult * atr
            target = entry - cfg.take_profit_atr_mult * atr
            if cfg.trailing_stop and lows_since_entry:
                # Ratchet the stop down as price makes new lows.
                stop = min(stop, min(lows_since_entry) + cfg.stop_loss_atr_mult * atr)
        else:
            stop = entry * (1 + cfg.fallback_stop_pct)
            target = None

        if price >= stop:
            return (
                f"Stop-loss (short): price ${price:,.2f} hit stop ${stop:,.2f} "
                f"(entry ${entry:,.2f}) — cutting the loss / locking in gains."
            )
        if target is not None and price <= target:
            return (
                f"Take-profit (short): price ${price:,.2f} reached target ${target:,.2f} "
                f"(entry ${entry:,.2f})."
            )
        return None

    if atr and atr > 0:
        stop = entry - cfg.stop_loss_atr_mult * atr
        target = entry + cfg.take_profit_atr_mult * atr
        if cfg.trailing_stop and highs_since_entry:
            stop = max(stop, max(highs_since_entry) - cfg.stop_loss_atr_mult * atr)
    else:
        stop = entry * (1 - cfg.fallback_stop_pct)
        target = None

    if price <= stop:
        return (
            f"Stop-loss: price ${price:,.2f} hit stop ${stop:,.2f} "
            f"(entry ${entry:,.2f}) — cutting the loss / locking in gains."
        )
    if target is not None and price >= target:
        return (
            f"Take-profit: price ${price:,.2f} reached target ${target:,.2f} "
            f"(entry ${entry:,.2f})."
        )
    return None
