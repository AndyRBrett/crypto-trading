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


def position_size(cfg, equity: float, cash: float, price: float, atr: float | None) -> float:
    """Volatility-based size so the stop distance risks ~``risk_per_trade_pct``.

    Bounded by the per-position equity cap and available cash (fee-aware).
    Returns 0 for non-positive inputs or dust-sized trades.
    """
    if price <= 0 or equity <= 0:
        return 0.0
    stop_dist = cfg.stop_loss_atr_mult * atr if (atr and atr > 0) else price * cfg.fallback_stop_pct
    if stop_dist <= 0:
        return 0.0
    qty_by_risk = (equity * cfg.risk_per_trade_pct) / stop_dist
    qty_by_cap = (equity * cfg.max_position_pct) / price
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
) -> str | None:
    """Return an exit reason if a stop / take-profit / trailing level is breached.

    ``highs_since_entry`` powers the Chandelier trailing stop (the highest high
    since the position opened); pass an empty/None sequence to disable trailing.
    """
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
