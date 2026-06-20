#!/usr/bin/env python3
"""Write ``overseer-status.json`` for the external Project Overseer monitor.

The overseer reads this file from the repo root via the GitHub API in its weekly
review to confirm the bot isn't a blind spot (issue #16). It summarizes the last
7 days of paper trading straight from the bot's own SQLite trade stores — the
``trading*.db`` files the bot writes (``trades`` table; see bot/storage.py).

Metrics that can't be computed are omitted rather than invented. ``errors`` is
empty when healthy: a week with zero fills is reported as data (``trades: 0``),
not an error — only an unreadable / missing trade store is flagged.
"""

from __future__ import annotations

import glob
import json
import sqlite3
import time
from datetime import datetime, timezone

WINDOW_DAYS = 7
STATUS_PATH = "overseer-status.json"
DB_GLOB = "trading*.db"  # per-account (trading.<name>.db) + legacy trading.db


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
    window_start = now - WINDOW_DAYS * 86_400
    errors: list[str] = []

    db_paths = sorted(glob.glob(DB_GLOB))
    if not db_paths:
        errors.append(f"no trade store found (expected {DB_GLOB})")

    read_any = False
    window_fills = 0          # all fills (BUY+SELL) in the window
    window_pnl = 0.0          # summed realized P&L over the window
    window_closed = 0         # closed (SELL) trades in the window
    window_wins = 0           # closed trades that realized a profit
    last_fill: float | None = None  # most recent fill across all history

    for path in db_paths:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, side, realized_pnl FROM trades"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            errors.append(f"{path}: {exc}")
            continue
        read_any = True
        for r in rows:
            ts = r["timestamp"]
            if last_fill is None or ts > last_fill:
                last_fill = ts
            if ts >= window_start:
                window_fills += 1
                if r["side"] == "SELL":
                    window_closed += 1
                    window_pnl += r["realized_pnl"]
                    if r["realized_pnl"] > 0:
                        window_wins += 1

    status: dict = {
        "generated_at": _iso(now),
        "window_days": WINDOW_DAYS,
    }
    # Trade counts / P&L need a readable store; omit them if we couldn't read one.
    if read_any:
        status["trades"] = window_fills
        status["pnl"] = round(window_pnl, 2)
        # Win rate is undefined with no closed trades in the window — omit it.
        if window_closed:
            status["win_rate"] = round(window_wins / window_closed, 3)
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
