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
"""

from __future__ import annotations

import glob
import json
import sqlite3
import time
from datetime import datetime, timezone

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


def _iso(ts: float) -> str:
    """Epoch seconds -> ISO-8601 UTC with a Z suffix (2026-06-19T18:24:30Z)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_metrics(now: float | None = None) -> dict:
    """Build the status payload from the trade store(s).

    A SELL closes a position in this bot, so each SELL is one completed round
    trip; win rate is the share of those that realized a profit. Realized P&L is
    recorded on SELLs, so the window P&L is the sum of ``realized_pnl`` for
    SELLs whose timestamp falls in the window.
    """
    now = time.time() if now is None else now
    windows = (WINDOW_DAYS, *EXTRA_WINDOW_DAYS)
    window_starts = {d: now - d * 86_400 for d in windows}
    errors: list[str] = []

    db_paths = sorted(glob.glob(DB_GLOB))
    if not db_paths:
        errors.append(f"no trade store found (expected {DB_GLOB})")

    read_any = False
    # Per-window accumulators keyed by window length in days.
    fills = {d: 0 for d in windows}    # all fills (BUY+SELL) in the window
    pnl = {d: 0.0 for d in windows}    # summed realized P&L over the window
    closed = {d: 0 for d in windows}   # closed (SELL) trades in the window
    wins = {d: 0 for d in windows}     # closed trades that realized a profit
    last_fill: float | None = None  # most recent fill across all history
    signals_evaluated = 0     # signals scored in the run that just executed
    signals_acted = 0         # those that actually became a trade this run
    run_since = now - SIGNAL_RUN_WINDOW

    for path in db_paths:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, side, realized_pnl FROM trades"
            ).fetchall()
            try:
                (n,) = conn.execute(
                    "SELECT COUNT(*) FROM signal_log WHERE timestamp >= ?",
                    (run_since,),
                ).fetchone()
                signals_evaluated += n
            except sqlite3.Error:
                pass  # older store without signal_log; heartbeat stays 0 for it
            conn.close()
        except sqlite3.Error as exc:
            errors.append(f"{path}: {exc}")
            continue
        read_any = True
        for r in rows:
            ts = r["timestamp"]
            if last_fill is None or ts > last_fill:
                last_fill = ts
            # A fill in the run window is a signal that was acted on this run.
            if ts >= run_since:
                signals_acted += 1
            for d in windows:
                if ts >= window_starts[d]:
                    fills[d] += 1
                    if r["side"] == "SELL":
                        closed[d] += 1
                        pnl[d] += r["realized_pnl"]
                        if r["realized_pnl"] > 0:
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
    status["signals_evaluated"] = signals_evaluated
    status["signals_acted"] = signals_acted
    status["last_fill_at"] = _iso(last_fill) if last_fill is not None else None
    status["errors"] = errors
    return status


def main() -> int:
    status = collect_metrics()
    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)
        f.write("\n")
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
