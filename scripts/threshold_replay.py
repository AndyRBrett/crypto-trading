#!/usr/bin/env python3
"""What-if threshold replay: sweep entry/exit thresholds over the logged
signal decisions and report the hypothetical P&L / alpha vs. buy-hold.

Reads each account's ``signal_log`` (the per-tick decision log, including the
signals that were generated but rejected), re-evaluates every logged tick
against a range of candidate threshold values, and prints one table per swept
parameter: how many logged signals would have triggered, the hypothetical
entries/exits, and the resulting P&L vs. buying and holding the same capital.

Decision support only — it changes nothing. Usage:

    # all supported accounts, default sweep grids, DBs in the CWD:
    python -m scripts.threshold_replay --config config.ci.yaml

    # one account, one custom axis, DBs pulled from the bot-state branch:
    python -m scripts.threshold_replay --config config.ci.yaml \
        --state-dir /tmp/state --account mean_reversion \
        --param rsi_mr_oversold=30,35,40,45

The bot's DBs live on the ``bot-state`` branch; fetch them first, e.g.:
    git fetch origin bot-state
    mkdir -p /tmp/state && for f in $(git ls-tree --name-only origin/bot-state | grep '\\.db$'); do
        git show origin/bot-state:$f > /tmp/state/$f; done
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bot.config import Config
from bot.threshold_replay import (
    DEFAULT_NOTIONAL,
    DEFAULT_SWEEPS,
    build_params,
    load_ticks,
    supported_strategies,
    sweep_param,
)


def _resolve_risk(config: Config, account):
    """Account risk fields override the top-level Config (None = inherit)."""
    import dataclasses

    overrides = {
        f: getattr(account, f)
        for f in (
            "fee_rate", "risk_per_trade_pct", "max_position_pct",
            "stop_loss_atr_mult", "take_profit_atr_mult", "trailing_stop",
            "fallback_stop_pct",
        )
        if getattr(account, f, None) is not None
    }
    return dataclasses.replace(config, **overrides)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _print_table(outcomes, current_value, header: str) -> None:
    print(f"\n-- {header}")
    print(
        f"   {'value':>7}  {'trig':>4} {'entries':>7} {'closed':>6} {'win%':>5} "
        f"{'strat P&L':>10} {'strat%':>7} {'B&H P&L':>10} {'B&H%':>7} {'alpha%':>7}"
    )
    for o in outcomes:
        marker = "*" if o.value == current_value else " "
        win = f"{o.win_rate * 100:.0f}" if o.win_rate is not None else "-"
        print(
            f" {marker} {o.value:>7g}  {o.triggers:>4} {o.entries:>7} {o.closed:>6} "
            f"{win:>5} {o.strategy_pnl:>+10.2f} {o.strategy_return_pct:>+7.2f} "
            f"{o.buy_hold_pnl:>+10.2f} {o.buy_hold_return_pct:>+7.2f} {o.alpha_pct:>+7.2f}"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Sweep thresholds over the logged reject-coded signals."
    )
    parser.add_argument("--config", default="config.ci.yaml")
    parser.add_argument("--state-dir", default=".",
                        help="directory holding the trading.<account>.db files")
    parser.add_argument("--account", action="append", default=[],
                        help="account name(s) to replay (default: all supported)")
    parser.add_argument("--param", action="append", default=[],
                        help="override one sweep axis, e.g. --param rsi_mr_oversold=30,35,40")
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL,
                        help="USD notional per hypothetical entry")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    overrides: dict[str, list[float]] = {}
    for spec in args.param:
        if "=" not in spec:
            parser.error(f"--param must be name=v1,v2,...  (got {spec!r})")
        name, _, values = spec.partition("=")
        overrides[name.strip()] = [float(v) for v in values.split(",")]

    accounts = [
        a for a in config.accounts
        if (not args.account or a.name in args.account)
        and a.strategy_type in supported_strategies()
    ]
    if not accounts:
        print(
            f"no matching account with a supported strategy "
            f"({', '.join(supported_strategies())})",
            file=sys.stderr,
        )
        return 1

    report = []
    for account in accounts:
        db = Path(args.state_dir) / account.resolved_db_path()
        if not db.exists():
            print(f"skipping {account.name}: {db} not found", file=sys.stderr)
            continue
        ticks = load_ticks(str(db))
        if not ticks:
            print(f"skipping {account.name}: no feature-tagged signal_log rows", file=sys.stderr)
            continue

        risk_cfg = _resolve_risk(config, account)
        grid = dict(DEFAULT_SWEEPS.get(account.strategy_type, {}))
        for name, values in overrides.items():
            if name in build_params(account.strategy):
                grid[name] = values

        span = f"{_fmt_ts(ticks[0].timestamp)}..{_fmt_ts(ticks[-1].timestamp)}"
        products = sorted({t.product_id for t in ticks})
        rejected = sum(1 for t in ticks if t.outcome != "acted")
        block = {
            "account": account.name,
            "strategy": account.strategy_type,
            "ticks": len(ticks),
            "rejected_or_held": rejected,
            "window": span,
            "products": products,
            "sweeps": {},
        }
        if not args.json:
            print(
                f"\n== {account.name} ({account.strategy_type}) — {len(ticks)} logged "
                f"decisions ({rejected} not acted on), {span}, {', '.join(products)}"
            )

        current = build_params(account.strategy)
        for param, values in grid.items():
            outcomes = sweep_param(
                ticks, account.strategy_type, account.strategy, risk_cfg,
                param, values, notional=args.notional,
            )
            if args.json:
                block["sweeps"][param] = {
                    "current": current[param],
                    "rows": [
                        {
                            "value": o.value, "triggers": o.triggers,
                            "entries": o.entries, "closed": o.closed,
                            "win_rate": o.win_rate, "deployed": round(o.deployed, 2),
                            "strategy_pnl": round(o.strategy_pnl, 2),
                            "buy_hold_pnl": round(o.buy_hold_pnl, 2),
                            "strategy_return_pct": round(o.strategy_return_pct, 3),
                            "buy_hold_return_pct": round(o.buy_hold_return_pct, 3),
                            "alpha_pct": round(o.alpha_pct, 3),
                        }
                        for o in outcomes
                    ],
                }
            else:
                _print_table(
                    outcomes, current[param],
                    f"sweep {param} (current {current[param]:g}; "
                    f"notional ${args.notional:g}/entry, fee {risk_cfg.fee_rate:.2%}/leg)",
                )
        report.append(block)

    if args.json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
