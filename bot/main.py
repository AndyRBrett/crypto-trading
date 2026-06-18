"""CLI entry point.

    python -m bot.main once      # run a single decision cycle
    python -m bot.main run       # loop forever, sleeping between ticks
    python -m bot.main status    # print portfolio summary
    python -m bot.main verify    # check Coinbase Advanced credentials
    python -m bot.main reset     # wipe the database (paper history)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

from .config import Config
from .runner import Runner


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_status(s: dict) -> None:
    label = s.get("name", "")
    strat = s.get("strategy", "")
    header = f"=== {label} ({strat}) ===" if label else "=== Paper Portfolio ==="
    print(f"\n{header}")
    print(f"  Cash:           ${s['cash']:,.2f}")
    print(f"  Equity:         ${s['equity']:,.2f}")
    print(f"  Total return:   {s['total_return_pct']:+.2f}%")
    print(f"  Realized P&L:   ${s['realized_pnl']:,.2f}")
    print(f"  Unrealized P&L: ${s['unrealized_pnl']:,.2f}")
    print(f"  Trades:         {s['num_trades']}")
    if s["positions"]:
        print("  Positions:")
        for pid, p in s["positions"].items():
            price = p["price"] or p["avg_price"]
            pnl = (price - p["avg_price"]) * p["quantity"]
            print(
                f"    {pid}: {p['quantity']:.6f} @ avg ${p['avg_price']:,.2f} "
                f"(now ${price:,.2f}, P&L ${pnl:+,.2f})"
            )
    else:
        print("  Positions:      (none)")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crypto paper trading bot")
    parser.add_argument(
        "command", choices=["once", "run", "status", "verify", "reset"]
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = Config.load(args.config)

    if args.command == "reset":
        removed = []
        for acct in config.accounts:
            path = acct.resolved_db_path()
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
        if removed:
            for path in removed:
                print(f"Removed {path}")
        else:
            print("Nothing to reset.")
        return 0

    if args.command == "verify":
        from .market_data import MarketData

        md = MarketData(config)
        ok = md.verify_credentials()
        print("Coinbase Advanced credentials:", "OK" if ok else "FAILED / not set")
        # Also sanity-check public data.
        product = config.accounts[0].products[0] if config.accounts else config.products[0]
        try:
            price = md.get_price(product)
            print(f"Public price for {product}: ${price:,.2f}")
        except Exception as exc:
            print(f"Public price check failed: {exc}")
        return 0 if ok else 1

    runner = Runner(config)
    try:
        if args.command == "status":
            for s in runner.status():
                _print_status(s)
        elif args.command == "once":
            trades = runner.tick()
            print(f"Tick complete. {len(trades)} trade(s) executed.")
            for t in trades:
                print(f"  {t.side} {t.product_id}: {t.explanation}")
            for s in runner.status():
                _print_status(s)
        elif args.command == "run":
            logging.getLogger(__name__).info(
                "Starting loop: %d account(s), every %ds. Ctrl+C to stop.",
                len(runner.engines),
                config.poll_interval,
            )
            while True:
                runner.tick()
                time.sleep(config.poll_interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
