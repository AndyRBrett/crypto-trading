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
* ``exit_reasons`` (issue #52): how the window's round trips actually ended —
  ``stop_loss`` / ``take_profit`` / ``position_aging`` / ``strategy_exit``. The
  counterpart to ``rejection_reasons``: one says why nothing could be entered,
  the other says what freed a slot.
* ``risk_breaker`` (issue #45): the accounts whose rolling-risk circuit breaker is
  currently throttling new entries, and when each tripped. Present only while at
  least one account is throttled, so its absence means the whole book is sizing
  normally.
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
from bot.risk import STOP_REASON_PREFIX

WINDOW_DAYS = 7
# Longer windows reported alongside the headline 7-day metrics (issue #20).
EXTRA_WINDOW_DAYS = (30, 90)
# Below this many closed trades, a window's win_rate is small-sample noise.
LOW_SAMPLE_TRADES = 10
# A signal that came within this % of its trigger was a near-miss, not a quiet
# market (issue #38). At 0.5% the price was effectively at the threshold and the
# strategy still declined — repeated near-misses mean the setting is too tight,
# whereas gaps of several percent mean there was genuinely nothing to trade.
NEAR_TRIGGER_PCT = 0.5
STATUS_PATH = "overseer-status.json"
DB_GLOB = "trading*.db"  # per-account (trading.<name>.db) + legacy trading.db
# A tick logs one signal_log row per product (including HOLDs). run-bot ticks at
# most hourly and write_status runs right after the tick in the same job, so
# signals written within this window belong to the run that just executed.
SIGNAL_RUN_WINDOW = 900  # seconds (15 min)
# Cap the rolling equity curve so the status file stays small; the series is
# downsampled to at most this many points across the headline window.
MAX_EQUITY_POINTS = 48


def _reasons(raw) -> list[str]:
    """The stored ``reasons`` JSON column as a list; [] for anything unreadable."""
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _exit_kind(reasons: list[str]) -> str:
    """Classify a closing leg by its recorded reason.

    The engine writes the exit reason it acted on, and the prefixes are stable
    contracts (``bot/risk.py``): a stop-out, a take-profit, an aged-out rotation
    (issue #52), or the strategy's own SELL/cover. Anything unrecognized is
    reported as ``other`` rather than silently folded into one of the buckets.
    """
    first = (reasons[0] if reasons else "") or ""
    if first.startswith(STOP_REASON_PREFIX):
        return "stop_loss"
    if first.startswith("Take-profit"):
        return "take_profit"
    if first.startswith("Position aging"):
        return "position_aging"
    return "strategy_exit" if first else "other"


def _account_name(db_path: str) -> str:
    """``trading.regime.db`` -> ``regime``; the legacy ``trading.db`` -> ``default``."""
    stem = db_path.rsplit("/", 1)[-1]
    if stem.startswith("trading.") and stem.endswith(".db"):
        middle = stem[len("trading."):-len(".db")]
        if middle:
            return middle
    return "default"


def _iso(ts: float) -> str:
    """Epoch seconds -> ISO-8601 UTC with a Z suffix (2026-06-19T18:24:30Z)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge_equity(
    series: list[list[tuple[float, float]]],
    clip_start: float | None = None,
    clip_to_common_start: bool = False,
) -> list[tuple[float, float]]:
    """Sum per-store equity snapshots into one full-resolution portfolio curve.

    ``series`` is one sorted ``[(timestamp, equity), ...]`` list per store. The
    stores tick independently, so at each observed timestamp we forward-fill each
    store's most recent equity (its last snapshot at-or-before that instant) and
    sum across stores. For the common single-store case this is just that store's
    own curve.

    ``clip_start`` emits only points at-or-after that timestamp while keeping
    earlier snapshots as forward-fill seeds. Pass the reporting window's start
    (with each store's series loaded from *before* it) so the curve's first
    point already sums every live store. Clipping each store's series to the
    window before merging instead leaves no seed: a store contributes $0 until
    its first in-window snapshot lands, so a 5-store book "5x'd" over the first
    few points of every status file (the 2026-06-29 $10k -> $49.5k artifact).

    When ``clip_to_common_start`` is set, the curve instead starts only once
    *every* store has reported at least once. Before that point the sum
    understates the book (a store with no earlier history to seed from
    contributes nothing), which would read as a huge spurious return the first
    time it comes online — it would wreck Sharpe/Sortino, so the risk window
    clips it off.

    With either clip, a store whose *last* snapshot predates the curve start is
    retired (e.g. the legacy single-account trading.db), not merely quiet — it
    is dropped entirely rather than forward-filled forever into a book it is no
    longer part of.
    """
    series = [s for s in series if s]
    if not series:
        return []
    if clip_to_common_start:
        # max-of-firsts can't belong to a store that's dropped below (a store's
        # last >= its first), so computing it up front is stable.
        clip_start = max(s[0][0] for s in series)  # latest first-snapshot
    if clip_start is not None:
        series = [s for s in series if s[-1][0] >= clip_start]
        if not series:
            return []
    timestamps = sorted({ts for s in series for ts, _ in s})
    if clip_start is not None:
        timestamps = [ts for ts in timestamps if ts >= clip_start]
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


def _aggregate_equity(
    series: list[list[tuple[float, float]]], clip_start: float | None = None
) -> list[tuple[float, float]]:
    """Portfolio-wide equity curve downsampled for the status file's dashboard
    chart: at most ``MAX_EQUITY_POINTS`` points, first and last always kept."""
    curve = _merge_equity(series, clip_start=clip_start)
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
    push_errors: set[str] = set()      # distinct push failures across the stores
    config_warnings: set[str] = set()  # settings enabled but inert (missing secret)
    equity_skip_warnings: set[str] = set()  # stores currently stuck on a stale equity snapshot
    breaker_tripped: dict[str, float | None] = {}  # accounts currently throttled (issue #45)
    exit_kinds: dict[str, int] = {}  # how the window's round trips ended (issue #52)
    last_notify: float | None = None   # most recent successful push, any account

    db_paths = sorted(glob.glob(DB_GLOB))
    if not db_paths:
        errors.append(f"no trade store found (expected {DB_GLOB})")

    portfolio_risk = None
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
    # Per-store snapshots over the full load window (risk lookback); the 7-day
    # headline curve is clipped at merge time so pre-window points still seed
    # the forward-fill (see _merge_equity).
    equity_series: list[list[tuple[float, float]]] = []
    # Per-signal decision log for the run that just executed (issue #23).
    decisions: list[dict] = []

    closed_legs: list[tuple[float, str, float]] = []   # (ts, product, realized)

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
                "SELECT timestamp, product_id, side, price, quantity, fee, reasons "
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
            equity_series.append(store_equity)
            # A rejected push is otherwise invisible: the bot keeps trading and
            # every metric here stays healthy while the phone goes quiet. Surface
            # it so a dead notification channel is a reported fault, not a guess.
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            except sqlite3.Error:
                meta = {}
            try:
                candidate = json.loads(meta.get("portfolio_risk", "null"))
                if (isinstance(candidate, dict) and isinstance(candidate.get("as_of"), (int, float))
                        and (portfolio_risk is None or candidate["as_of"] > portfolio_risk["as_of"])):
                    portfolio_risk = candidate
            except (ValueError, TypeError):
                pass
            if meta.get("last_push_error"):
                push_errors.add(meta["last_push_error"].splitlines()[0])
            # last_equity_skip_at is cleared the moment a store snapshots
            # successfully again, so its presence means the *most recent* tick
            # is still stuck — the merged equity curve is flatlining on this
            # store's contribution right now, not just did once in the past
            # (issue #50: a 25h price freeze that read as "quiet market"
            # instead of "can't price the position").
            if meta.get("last_equity_skip_at"):
                products = meta.get("last_equity_skip_products") or "?"
                try:
                    since = _iso(float(meta["last_equity_skip_at"]))
                except (TypeError, ValueError):
                    # An unparseable stamp still means the store is stuck. Going
                    # quiet here would rebuild the very bug this block exists to
                    # catch — a live fault with nothing in `errors` — inside its
                    # own error path.
                    since = f"an unreadable time ({meta['last_equity_skip_at']!r})"
                equity_skip_warnings.add(
                    f"equity snapshot stale since {since}: "
                    f"no fresh price for {products}"
                )
            # Rolling-risk breaker (issue #45). The engine records its own
            # trip/recover transitions against its real config; reporting the
            # recorded state (rather than recomputing it here with guessed
            # floors) is what makes "why did sizing halve?" answerable.
            if meta.get("risk_breaker_tripped"):
                try:
                    changed = float(meta.get("risk_breaker_changed_at") or 0.0) or None
                except ValueError:
                    changed = None
                breaker_tripped[_account_name(path)] = changed
            for warning in (meta.get("config_warnings") or "").splitlines():
                if warning.strip():
                    config_warnings.add(warning.strip())
            if meta.get("last_notify_at"):
                try:
                    last_notify = max(last_notify or 0.0, float(meta["last_notify_at"]))
                except ValueError:
                    pass
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
                    reasons=_reasons(r["reasons"]),
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
            if ts >= head_start and id(t) in closers:
                # How each round trip ended. `in_position` rejections say the
                # book was full; this says what finally emptied a slot — a stop,
                # a target, the strategy, or the aging cap (issue #52).
                kind = _exit_kind(t.reasons)
                exit_kinds[kind] = exit_kinds.get(kind, 0) + 1
            if ts >= head_start:
                # Every fill is also a mark for the benchmark, and opening legs
                # (long entries and short entries alike) are the capital the
                # strategy deployed — its buy-and-hold weighting.
                _mark(t.product_id, ts, t.price)
                if id(t) not in closers:
                    buy_notional[t.product_id] = (
                        buy_notional.get(t.product_id, 0.0) + t.price * t.quantity
                    )
            # Every closing leg inside the widest window, kept once for the
            # per-asset breakdown below (#51). Gathered here rather than in a
            # second pass because the replay that classifies opening vs closing
            # legs is the expensive part and it is already running.
            if id(t) in closers and ts >= window_starts[max(windows)]:
                closed_legs.append((ts, t.product_id, t.realized_pnl))
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
        # Which asset is doing this (#51). Omitted entirely when nothing closed,
        # so an empty book reads as no data rather than a table of zeroes.
        by_asset = attribution(closed_legs, window_starts, windows)
        if by_asset:
            status["attribution"] = by_asset
        # Buy-and-hold benchmark + equity curve (issue #22): turn the bare P&L
        # into alpha-vs-holding. Omitted when nothing was deployed in the window.
        benchmark = _benchmark(pnl[WINDOW_DAYS], buy_notional, first_mark, last_mark)
        if benchmark is not None:
            status["benchmark"] = benchmark
        curve = _aggregate_equity(equity_series, clip_start=head_start)
        if curve:
            status["equity_curve"] = [
                {"t": _iso(ts), "equity": round(eq, 2)} for ts, eq in curve
            ]
        # Risk-adjusted metrics (Sharpe / Sortino / max drawdown) turn the raw
        # P&L into something interpretable — is a down month normal variance or a
        # regression? Computed from the persisted equity curve over a 30-day
        # lookback; clip the cold-start ramp so a store coming online mid-window
        # isn't read as a return. Omitted when there isn't enough curve to measure.
        risk_curve = _merge_equity(equity_series, clip_to_common_start=True)
        risk = risk_metrics(risk_curve, now=now)
        if risk:
            status["risk_metrics"] = risk
    if exit_kinds:
        status["exit_reasons"] = dict(sorted(exit_kinds.items()))
    if breaker_tripped:
        status["risk_breaker"] = {
            "tripped_accounts": sorted(breaker_tripped),
            "since": {
                name: _iso(ts) for name, ts in sorted(breaker_tripped.items()) if ts
            },
        }
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
        # How near the non-acting signals came to firing (issue #38) — turns
        # "0 of 10 acted" from a bare fact into a diagnosis.
        proximity = signal_proximity(decisions)
        if proximity:
            status["signal_proximity"] = proximity
    status["last_fill_at"] = _iso(last_fill) if last_fill is not None else None
    status["last_notify_at"] = _iso(last_notify) if last_notify else None
    for msg in sorted(push_errors):
        errors.append(f"push notification failing: {msg}")
    for msg in sorted(config_warnings):
        errors.append(f"config: {msg}")
    for msg in sorted(equity_skip_warnings):
        errors.append(msg)
    status["errors"] = errors
    if portfolio_risk is not None:
        status["portfolio_risk"] = portfolio_risk
        status["portfolio_risk"]["stale"] = now - portfolio_risk["as_of"] > SIGNAL_RUN_WINDOW

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


def signal_proximity(decisions, near_pct=NEAR_TRIGGER_PCT):
    """Aggregate how CLOSE non-acting signals came to firing (issue #38).

    ``signals_acted: 0 / signals_evaluated: 10`` states that nothing fired but
    not why, and the two explanations call for opposite responses:

      quiet market    price sat far from its trigger all window. The thresholds
                      are fine and there was simply nothing to trade — doing
                      nothing was correct, so leave the settings alone.
      miscalibrated   price repeatedly came within a hair of the trigger and
                      never crossed. The strategy is watching the right move and
                      declining to take it; the threshold wants widening.

    Telling them apart needs the *distribution* of the distance-to-trigger, not
    merely its presence. ``closest_pct`` is the single most useful number: the
    smallest gap between price and its trigger across the window. A window whose
    closest approach was 5% never had a trade in it; one that repeatedly grazed
    0.2% is a threshold problem.

    Returns None when no decision carried thresholds, so the key is absent from
    the status file rather than present and meaningless.
    """
    # Grouped BY METRIC, never pooled. Each strategy publishes its own threshold
    # keys and they are not in the same units: donchian reports breakout_dist_pct
    # and exit_dist_pct in percent, while the trend/RSI strategies report
    # rsi_to_overbought and adx_to_min in index POINTS. Averaging 0.4% against
    # 39.5 RSI points would produce a confident, meaningless number — and since
    # only 2 of 10 live decisions carry the donchian keys, reading just those
    # would silently describe a fifth of the evidence as though it were all of it.
    per_metric: dict[str, list[float]] = {}
    for d in decisions:
        for key, raw in (d.get("thresholds") or {}).items():
            if raw is None:
                continue
            try:
                gap = abs(float(raw))
            except (TypeError, ValueError):
                continue  # convert BEFORE creating the key, or a metric whose
                          # every value is unparseable leaves an empty list here
                          # and the summary below indexes off the end of it
            per_metric.setdefault(key, []).append(gap)
    if not per_metric:
        return None

    metrics, near_total, pct_samples = {}, 0, 0
    for key, gaps in sorted(per_metric.items()):
        gaps.sort()
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
        entry = {
            "samples": len(gaps),
            "closest": round(gaps[0], 3),
            "median": round(median, 3),
            "widest": round(gaps[-1], 3),
        }
        # The near-miss test only applies to percentage-denominated gaps, where
        # NEAR_TRIGGER_PCT means something. Point-denominated ones (RSI, ADX) are
        # still reported for the chart, but they do not vote on the verdict.
        # Historical raw channel gaps are comparison data, not active triggers.
        if key.endswith("_pct") and not key.startswith("raw_"):
            entry["near_trigger"] = sum(1 for g in gaps if g <= near_pct)
            near_total += entry["near_trigger"]
            pct_samples += len(gaps)
        metrics[key] = entry

    return {
        "metrics": metrics,
        "near_threshold_pct": near_pct,
        "pct_samples": pct_samples,
        "near_trigger": near_total,
        # Precomputed so the dashboard and the overseer's agents reach the same
        # conclusion from the same rule instead of each inventing a cutoff.
        # Unknown, not "quiet", when no percentage-denominated gap was recorded:
        # absence of evidence isn't evidence of a quiet market.
        "verdict": ("thresholds-may-be-tight" if near_total
                    else "quiet-market" if pct_samples else "unknown"),
    }


# --- per-asset attribution (issue #51) -------------------------------------
#
# Portfolio P&L answers "how did we do"; it cannot answer "which asset is
# costing us", which is the question you act on. A book that is flat overall can
# be one asset quietly bleeding into another one carrying it, and the status
# file showed only the sum.
#
# NOTE ON DRAWDOWN. bot.metrics.max_drawdown is deliberately NOT reused here. It
# returns a FRACTION (equity / peak - 1), which needs a positive equity base; a
# cumulative realized-P&L curve starts at zero and can sit negative, where that
# formula divides by a peak of zero or flips sign against a negative one. The
# honest per-asset measure is the absolute peak-to-trough decline in dollars, so
# that is what this computes and what the field is named after — never a percent
# that would look comparable to risk_metrics.max_drawdown_pct and mean something
# entirely different.


def realized_drawdown(legs):
    """Worst peak-to-trough fall of cumulative realized P&L, in dollars (<= 0).

    `legs` is [(timestamp, realized_pnl), ...] in any order; it is sorted here so
    a caller cannot get a subtly wrong answer by passing store-ordered rows.
    """
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for _ts, realized in sorted(legs, key=lambda leg: leg[0]):
        cumulative += realized
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


def attribution(closed_legs, window_starts, windows):
    """Per-asset P&L, closed-trade count and realized drawdown, per window.

    Shaped asset-first ({"BTC-USD": {"7d": {...}}}) because that is the question
    being asked — which asset is doing this — and it is what a panel iterates.
    Assets with no closed trade in any window are omitted rather than reported as
    zeroes: a symbol the strategy never closed is not a flat performer.
    """
    products = sorted({product for _ts, product, _pnl in closed_legs})
    out = {}
    for product in products:
        per_window = {}
        for d in windows:
            legs = [(ts, pnl) for ts, prod, pnl in closed_legs
                    if prod == product and ts >= window_starts[d]]
            if not legs:
                continue
            per_window[f"{d}d"] = {
                "pnl": round(sum(pnl for _ts, pnl in legs), 2),
                "closed": len(legs),
                "realized_drawdown": round(realized_drawdown(legs), 2),
            }
        if per_window:
            out[product] = per_window
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
