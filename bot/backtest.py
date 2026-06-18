"""Historical backtester.

Replays a candle series through a strategy and the *exact* risk layer the live
engine uses (``bot/risk.py`` for sizing and stops, ``Portfolio`` for fills and
fees), so results net of fees reflect what the bot would actually have done.

This is the measurement tool: change a strategy or a parameter, run a backtest,
and compare return / drawdown / win-rate before committing the change to live
paper trading — instead of waiting weeks for live signal to accrue.

Single-instrument by design: each run evaluates one strategy on one product, the
standard way to judge a strategy in isolation. Portfolio-level heat across
products (``max_open_positions``) is a live concern, not a per-strategy one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import risk
from .portfolio import Portfolio
from .strategy import BUY, SELL


@dataclass
class BacktestResult:
    strategy_type: str
    product_id: str
    bars: int
    starting_cash: float
    final_equity: float
    total_return_pct: float
    realized_pnl: float
    fees_paid: float
    num_trades: int          # round-trip exits
    wins: int
    losses: int
    win_rate: float          # fraction of exits that were profitable
    profit_factor: float     # gross wins / gross losses (inf if no losses)
    max_drawdown_pct: float
    trades: list = field(default_factory=list)

    def summary(self) -> str:
        pf = "inf" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}"
        return (
            f"{self.strategy_type:>18} {self.product_id:<10} "
            f"ret {self.total_return_pct:+7.2f}%  "
            f"maxDD {self.max_drawdown_pct:5.2f}%  "
            f"trades {self.num_trades:>3}  win {self.win_rate*100:4.0f}%  "
            f"PF {pf:>5}  fees ${self.fees_paid:,.2f}"
        )


def run_backtest(
    strategy,
    candles: Sequence[dict],
    config,
    product_id: str = "BTC-USD",
) -> BacktestResult:
    """Replay ``candles`` (oldest→newest, each a dict with close/high/low/time)."""
    portfolio = Portfolio(config.starting_cash, config.fee_rate)
    strategy_type = type(strategy).__name__
    min_c = strategy.min_candles()

    peak_equity = config.starting_cash
    max_dd = 0.0

    candles = list(candles)
    for i in range(min_c, len(candles)):
        window = candles[: i + 1]            # settled history through bar i
        bar = candles[i]
        price = float(bar["close"])
        ts = bar.get("time", i)
        prices = {product_id: price}

        signal = strategy.generate_signal(product_id, window, sentiment=None)
        atr = signal.indicators.get("atr")
        pos = portfolio.position(product_id)

        if pos.quantity > 0:
            opened = portfolio.opened_at(product_id)
            highs = [
                c["high"] for c in window
                if "high" in c and (opened is None or c.get("time", 0) >= opened)
            ]
            reason = risk.protective_exit_reason(config, pos.avg_price, price, atr, highs)
            if reason is None and signal.action == SELL:
                reason = "; ".join(signal.reasons)
            if reason:
                portfolio.execute(
                    SELL, product_id, price, pos.quantity, timestamp=ts, reasons=[reason]
                )
        elif signal.action == BUY:
            equity = portfolio.total_equity(prices)
            qty = risk.position_size(config, equity, portfolio.cash, price, atr)
            if qty > 0:
                portfolio.execute(
                    BUY, product_id, price, qty, timestamp=ts, reasons=signal.reasons
                )

        equity = portfolio.total_equity(prices)
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_dd = max(max_dd, (peak_equity - equity) / peak_equity)

    # Mark to the final price for reporting.
    last_price = float(candles[-1]["close"]) if candles else 0.0
    final_equity = portfolio.total_equity({product_id: last_price})

    exits = [t for t in portfolio.trades if t.side == SELL]
    wins = sum(1 for t in exits if t.realized_pnl > 0)
    losses = sum(1 for t in exits if t.realized_pnl < 0)
    gross_win = sum(t.realized_pnl for t in exits if t.realized_pnl > 0)
    gross_loss = -sum(t.realized_pnl for t in exits if t.realized_pnl < 0)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    return BacktestResult(
        strategy_type=strategy_type,
        product_id=product_id,
        bars=len(candles),
        starting_cash=config.starting_cash,
        final_equity=final_equity,
        total_return_pct=(final_equity / config.starting_cash - 1) * 100
        if config.starting_cash
        else 0.0,
        realized_pnl=portfolio.realized_pnl(),
        fees_paid=sum(t.fee for t in portfolio.trades),
        num_trades=len(exits),
        wins=wins,
        losses=losses,
        win_rate=(wins / len(exits)) if exits else 0.0,
        profit_factor=profit_factor,
        max_drawdown_pct=max_dd * 100,
        trades=portfolio.trades,
    )
