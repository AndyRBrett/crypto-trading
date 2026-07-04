#!/usr/bin/env python3
"""Write ``overseer-status.json`` for the external Project Overseer monitor.

The overseer reads this file from the repo root via the GitHub API in its weekly
review to confirm the bot isn't a blind spot (issue #16). It summarizes paper
trading straight from the bot's own SQLite trade stores — the ``trading*.db``
files the bot writes (``trades`` table; see bot/storage.py).

Alongside the headline 7-day window it reports 30- and 90-day P&L and trade
counts (issue #20) so a quiet week doesn't hide longer-term performance. Because
a 1-2 trade week reads as a flawless ``win_rate`` of 1.0, ``win_rate`` carries a
``win_rate_low_sample`` flag when fewer than ten trades back it, so the
overseer/dashboard can grey it out instead of trusting small-sample noise.

Metrics that can't be computed are omitted rather than invented. ``errors`` is
empty when healthy: a week with zero fills is reported as data (``trades: 0``),
not an error — only an unreadable / missing trade store is flagged.

It also emits a heartbeat (issue #18): ``last_run_at`` (every run) and
``signals_evaluated`` (signals scored this run, counted from the ``signal_log``
table) so a healthy-but-idle bot is distinguishable from a silently dead one.
``signals_acted`` is how many of those scored signals actually became a trade.

Two further enrichments turn raw numbers into evaluable signal:

* A buy-and-hold ``benchmark`` (issue #22): raw P&L doesn't say whether the
  strategy beats passively holding the same coins. Using per-symbol mark prices
  at the window's start and end (from the trade + signal logs), weighted by the
  notional the strategy actually deployed, it reports ``strategy_return_pct`` vs.
  ``buy_hold_return_pct`` and the ``alpha_pct`` between them, plus a small rolling
  ``equity_curve`` for a dashboard chart.
* A per-signal decision log (issue #23): ``rejection_reasons`` (a count of why
  evaluated signals didn't trade) and ``avg_slippage_bps`` (realized signal-to-
  fill slippage on the ones that did), so tuning is data-driven instead of guesswork.
  Each ``hold``/``rejected`` decision also carries the signed ``thresholds`` it
  logged — how close that signal came to firing — so "6/6 no_signal" is no longer
  an opaque gap.
* ``risk_metrics``: Sharpe, Sortino, max drawdown and annualized volatility over a
  30-day lookback, computed from the persisted equity curve (see ``bot/metrics.py``
  for the conventions). They scale return against the risk taken to earn it, so a
  raw P&L number becomes interpretable — is a down month normal variance or a real
  regression? Omitted when there isn't enough equity history to measure.
"""

from __future__ import annotations

import bisect
import glob
import json
import sqlite3
import time
from datetime import datetime, timezone

from bot.metrics import RISK_WINDOW_DAYS, risk_metrics
from bot.portfolio import Portfolio, Trade, closing_legs

WINDOW_DAYS = 7
# Longer windows reported alongside the headline 7-day metrics (issue #20).
EXTRA_WINDOW_DAYS = (30, 90)
# Below this many closed trades, a window's win_rate is small-sample noise.
LOW_SAMPLE_TRADES = 10
STATUS_PATH = "overseer-status.json"
DB_GLOB = "trading*.db"  # per-account (trading.<name>.db) + legacy trading.db
# A tick logs one signal_log row per product (including HOLDs). run-bot ticks at
# most hourly and write_status runs right after the tick in the same job, so
# signals written within this window belong to the run that just executed.
SIGNAL_RUN_WINDOW = 900  # seconds (15 min)
# Cap the rolling equity curve so the status file stays small; the series is
# downsampled to at most this many points across the headline window.
MAX_EQUITY_POINTS = 48


def _iso(ts: float) -> str:
    """Epoch seconds -> ISO-8601 UTC with a Z suffix (2026-06-19T18:24:30Z)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge_equity(
    series: list[list[tuple[float, float]]], clip_to_common_start: bool = False
) -> list[tuple[float, float]]:
    """Sum per-store equity snapshots into one full-resolution portfolio curve.

    ``series`` is one sorted ``[(timestamp, equity), ...]`` list per store. The
    stores tick independently, so at each observed timestamp we forward-fill each
    store's most recent equity (its last snapshot at-or-before that instant) and
    sum across stores. For the common single-store case this is just that store's
    own curve.

    When ``clip_to_common_start`` is set, the curve starts only once *every* store
    has reported at least once. Before that point the sum understates the book (a
    store that hasn't ticked yet contributes nothing), which would read as a huge
    spurious return the first time it comes online — fine for a rough dashboard
    line, but it would wreck Sharpe/Sortino, so the risk window clips it off.
    """
    series = [s for s in series if s]
    if not series:
        return []
    timestamps = sorted({ts for s in series for ts, _ in s})
    if clip_to_common_start:
        common_start = max(s[0][0] for s in series)  # latest first-snapshot
        timestamps = [ts for ts in timestamps if ts >= common_start]
    ts_lists = [[ts for ts, _ in s] for s in series]
    eq_lists = [[eq for _, eq in s] for s in series]
    curve: list[tuple[float, float]] = []
    for ts in timestamps:
        total = 0.0
        for tl, el in zip(ts_lists, eq_lists):
            i = bisect.bisect_right(tl, ts) - 1
            if i >= 0:  # store had started by this instant
                total += el[i]
        curve.append((ts, total))
    return curve


def _aggregate_equity(series: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Portfolio-wide equity curve downsampled for the status file's dashboard
    chart: at most ``MAX_EQUITY_POINTS`` points, first and last always kept."""
    curve = _merge_equity(series)
    if len(curve) > MAX_EQUITY_POINTS:
        step = (len(curve) - 1) / (MAX_EQUITY_POINTS - 1)
        idxs = sorted({round(k * step) for k in range(MAX_EQUITY_POINTS)})
        curve = [curve[i] for i in idxs]
    return curve


def collect_metrics(now: float | None = None) -> dict:
    """Build the status payload from the trade store(s).

    A completed round trip is any *closing leg* — a SELL closing a long or,
    since shorting landed, a BUY covering a short (realized P&L rides on the
    closing fill either way). Win rate is the share of closing legs that
    realized a profit; window P&L sums their realized P&L. Each store's log is
    replayed through ``Portfolio.from_trades`` first so pre-fee-fix rows are
    normalized to the current P&L formula instead of mixing two conventions.
    """
    now = time.time() if now is None else now
    windows = (WINDOW_DAYS, *EXTRA_WINDOW_DAYS)
    window_starts = {d: now - d * 86_400 for d in windows}
    head_start = window_starts[WINDOW_DAYS]
    # The risk window (Sharpe/Sortino/drawdown) reaches further back than the
    # headline 7-day equity curve, so load equity over the longer of the two.
    risk_start = now - RISK_WINDOW_DAYS * 86_400
    equity_load_start = min(head_start, risk_start)
    errors: list[str] = []

    db_paths = sorted(glob.glob(DB_GLOB))
    if not db_paths:
        errors.append(f"no trade store found (expected {DB_GLOB})")

    read_any = False
    # Per-window accumulators keyed by window length in days.
    fills = {d: 0 for d in windows}    # all fills (BUY+SELL) in the window
    pnl = {d: 0.0 for d in windows}    # summed realized P&L over the window
    closed = {d: 0 for d in windows}   # closing legs (long exit / short cover)
    wins = {d: 0 for d in windows}     # closing legs that realized a profit
    last_fill: float | None = None  # most recent fill across all history
    signals_evaluated = 0     # signals scored in the run that just executed
    signals_acted = 0         # those that actually became a trade this run
    run_since = now - SIGNAL_RUN_WINDOW

    # Buy-and-hold benchmark accumulators over the headline window (issue #22).
    buy_notional: dict[str, float] = {}            # strategy capital deployed per symbol
    first_mark: dict[str, tuple[float, float]] = {}  # earliest (ts, price) seen per symbol
    last_mark: dict[str, tuple[float, float]] = {}   # latest (ts, price) seen per symbol
    equity_series: list[list[tuple[float, float]]] = []  # 7-day curve, per store
    risk_equity_series: list[list[tuple[float, float]]] = []  # risk window, per store
    # Per-signal decision log for the run that just executed (issue #23).
    decisions: list[dict] = []

    def _mark(product_id: str, ts: float, price: float) -> None:
        """Record a price observation so the window's start/end marks can be found."""
        if product_id not in first_mark or ts < first_mark[product_id][0]:
            first_mark[product_id] = (ts, price)
        if product_id not in last_mark or ts > last_mark[product_id][0]:
            last_mark[product_id] = (ts, price)

    for path in db_paths:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, product_id, side, price, quantity, fee "
                "FROM trades"
            ).fetchall()
            try:
                sig_rows = conn.execute(
                    "SELECT timestamp, product_id, price FROM signal_log "
                    "WHERE timestamp >= ?",
                    (head_start,),
                ).fetchall()
            except sqlite3.Error:
                sig_rows = []  # older store without signal_log
            decisions += _store_decisions(conn, run_since)
            store_equity = [
                (r["timestamp"], r["equity"])
                for r in conn.execute(
                    "SELECT timestamp, equity FROM equity "
                    "WHERE timestamp >= ? ORDER BY timestamp",
                    (equity_load_start,),
                )
            ]
            risk_equity_series.append(store_equity)
            # The headline curve is the 7-day tail of the same load.
            equity_series.append([p for p in store_equity if p[0] >= head_start])
            conn.close()
        except sqlite3.Error as exc:
            errors.append(f"{path}: {exc}")
            continue
        read_any = True
        signals_evaluated += sum(1 for r in sig_rows if r["timestamp"] >= run_since)
        for r in sig_rows:
            _mark(r["product_id"], r["timestamp"], r["price"])
        # Replay the store's log so realized P&L is uniformly on the current
        # formula, then classify each fill as an opening or closing leg —
        # direction-agnostic, so short covers (BUY legs) are counted correctly.
        store_trades = Portfolio.from_trades(
            0.0,
            0.0,
            [
                Trade(
                    timestamp=r["timestamp"],
                    product_id=r["product_id"],
                    side=r["side"],
                    price=r["price"],
                    quantity=r["quantity"],
                    fee=r["fee"],
                    cash_after=0.0,
                )
                for r in rows
            ],
        ).trades
        closers = {id(t) for t in closing_legs(store_trades)}
        for t in store_trades:
            ts = t.timestamp
            if last_fill is None or ts > last_fill:
                last_fill = ts
            # A fill in the run window is a signal that was acted on this run.
            if ts >= run_since:
                signals_acted += 1
            if ts >= head_start:
                # Every fill is also a mark for the benchmark, and opening legs
                # (long entries and short entries alike) are the capital the
                # strategy deployed — its buy-and-hold weighting.
                _mark(t.product_id, ts, t.price)
                if id(t) not in closers:
                    buy_notional[t.product_id] = (
                        buy_notional.get(t.product_id, 0.0) + t.price * t.quantity
                    )
            for d in windows:
                if ts >= window_starts[d]:
                    fills[d] += 1
                    if id(t) in closers:
                        closed[d] += 1
                        pnl[d] += t.realized_pnl
                        if t.realized_pnl > 0:
                            wins[d] += 1

    status: dict = {
        "generated_at": _iso(now),
        # Heartbeat: last_run_at is always written, and signals_evaluated proves
        # the strategy pipeline executed this run — so a healthy-but-idle bot
        # (signals_evaluated > 0, trades 0) is distinguishable from a stalled one
        # (signals_evaluated 0) even when both report trades=0/pnl=0/errors=[].
        "last_run_at": _iso(now),
        "window_days": WINDOW_DAYS,
    }
    # Trade counts / P&L need a readable store; omit them if we couldn't read one.
    if read_any:
        status["trades"] = fills[WINDOW_DAYS]
        status["pnl"] = round(pnl[WINDOW_DAYS], 2)
        # Win rate is undefined with no closed trades in the window — omit it.
        if closed[WINDOW_DAYS]:
            status["win_rate"] = round(wins[WINDOW_DAYS] / closed[WINDOW_DAYS], 3)
            # Flag the small-sample case so a 1-2 trade week's perfect win_rate
            # is greyed out rather than trusted.
            if closed[WINDOW_DAYS] < LOW_SAMPLE_TRADES:
                status["win_rate_low_sample"] = True
        # Longer windows so a quiet week doesn't hide longer-term performance.
        for d in EXTRA_WINDOW_DAYS:
            status[f"pnl_{d}d"] = round(pnl[d], 2)
            status[f"trades_{d}d"] = fills[d]
        # Buy-and-hold benchmark + equity curve (issue #22): turn the bare P&L
        # into alpha-vs-holding. Omitted when nothing was deployed in the window.
        benchmark = _benchmark(pnl[WINDOW_DAYS], buy_notional, first_mark, last_mark)
        if benchmark is not None:
            status["benchmark"] = benchmark
        curve = _aggregate_equity(equity_series)
        if curve:
            status["equity_curve"] = [
                {"t": _iso(ts), "equity": round(eq, 2)} for ts, eq in curve
            ]
        # Risk-adjusted metrics (Sharpe / Sortino / max drawdown) turn the raw
        # P&L into something interpretable — is a down month normal variance or a
        # regression? Computed from the persisted equity curve over a 30-day
        # lookback; clip the cold-start ramp so a store coming online mid-window
        # isn't read as a return. Omitted when there isn't enough curve to measure.
        risk_curve = _merge_equity(risk_equity_series, clip_to_common_start=True)
        risk = risk_metrics(risk_curve, now=now)
        if risk:
            status["risk_metrics"] = risk
    status["signals_evaluated"] = signals_evaluated
    status["signals_acted"] = signals_acted
    # Decision log (issue #23): why each evaluated signal did/didn't trade, and
    # the realized slippage on the ones that did.
    if decisions:
        status["decisions"] = decisions
        rejections: dict[str, int] = {}
        slippages: list[float] = []
        for d in decisions:
            if d["outcome"] == "acted":
                if d.get("slippage_bps") is not None:
                    slippages.append(d["slippage_bps"])
            elif d.get("reject_code"):
                rejections[d["reject_code"]] = rejections.get(d["reject_code"], 0) + 1
        if rejections:
            status["rejection_reasons"] = rejections
        if slippages:
            status["avg_slippage_bps"] = round(sum(slippages) / len(slippages), 2)
    status["last_fill_at"] = _iso(last_fill) if last_fill is not None else None
    status["errors"] = errors
    return status


def _store_decisions(conn: sqlite3.Connection, run_since: float) -> list[dict]:
    """Per-signal decisions logged in the run window, oldest first.

    Reads the decision-log columns (issue #23). Stores written before those
    columns existed simply contribute nothing — guarded so an old store never
    breaks the status write.
    """
    try:
        rows = conn.execute(
            "SELECT product_id, action, outcome, reject_code, slippage_bps, features "
            "FROM signal_log WHERE timestamp >= ? ORDER BY id",
            (run_since,),
        ).fetchall()
    except sqlite3.Error:
        # Older store without the features column — fall back to the rest.
        try:
            rows = conn.execute(
                "SELECT product_id, action, outcome, reject_code, slippage_bps "
                "FROM signal_log WHERE timestamp >= ? ORDER BY id",
                (run_since,),
            ).fetchall()
        except sqlite3.Error:
            return []
    out = []
    for r in rows:
        keys = r.keys()
        decision = {
            "product_id": r["product_id"],
            "action": r["action"],
            "outcome": r["outcome"] or "hold",
            "reject_code": r["reject_code"] or "",
            "slippage_bps": r["slippage_bps"],
        }
        # Surface how close a HOLD came to firing: the signed distance to each
        # decision threshold. This is the whole point of the snapshot — "6/6
        # no_signal" becomes "and here's how near each was". Acted signals already
        # have a full trade record, so the thresholds are only added on non-acts.
        if "features" in keys and decision["outcome"] != "acted":
            try:
                feats = json.loads(r["features"]) if r["features"] else {}
            except (ValueError, TypeError):
                feats = {}
            if feats.get("thresholds"):
                decision["thresholds"] = feats["thresholds"]
        out.append(decision)
    return out


def _benchmark(
    strategy_pnl: float,
    buy_notional: dict[str, float],
    first_mark: dict[str, tuple[float, float]],
    last_mark: dict[str, tuple[float, float]],
) -> dict | None:
    """Buy-and-hold benchmark over the headline window (issue #22).

    For each symbol the strategy deployed capital into, value that capital as if
    it had simply been held from the window's start mark to its end mark, then
    compare against the strategy's realized P&L. Both returns are expressed
    against the same deployed notional so ``alpha_pct`` is apples-to-apples.

    Returns ``None`` when no capital was deployed in the window (nothing to
    benchmark against).
    """
    deployed = sum(buy_notional.values())
    if deployed <= 0:
        return None
    bh_pnl = 0.0
    for product_id, notional in buy_notional.items():
        start = first_mark.get(product_id)
        end = last_mark.get(product_id)
        if not start or not end or start[1] <= 0:
            continue
        bh_pnl += notional * (end[1] / start[1] - 1)
    return {
        "deployed_notional": round(deployed, 2),
        "strategy_pnl": round(strategy_pnl, 2),
        "buy_hold_pnl": round(bh_pnl, 2),
        "strategy_return_pct": round(strategy_pnl / deployed * 100, 3),
        "buy_hold_return_pct": round(bh_pnl / deployed * 100, 3),
        "alpha_pct": round((strategy_pnl - bh_pnl) / deployed * 100, 3),
    }


def main() -> int:
    status = collect_metrics()
    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
