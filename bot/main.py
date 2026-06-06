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
from .engine import Engine


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_status(engine: Engine) -> None:
    s = engine.status()
    print("\n=== Paper Portfolio ===")
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
        if os.path.exists(config.db_path):
            os.remove(config.db_path)
            print(f"Removed {config.db_path}")
        else:
            print("Nothing to reset.")
        return 0

    if args.command == "verify":
        from .market_data import MarketData

        md = MarketData(config)
        ok = md.verify_credentials()
        print("Coinbase Advanced credentials:", "OK" if ok else "FAILED / not set")
        # Also sanity-check public data.
        try:
            price = md._public_price(config.products[0])
            print(f"Public price for {config.products[0]}: ${price:,.2f}")
        except Exception as exc:
            print(f"Public price check failed: {exc}")
        return 0 if ok else 1

    engine = Engine(config)
    try:
        if args.command == "status":
            _print_status(engine)
        elif args.command == "once":
            trades = engine.tick()
            print(f"Tick complete. {len(trades)} trade(s) executed.")
            for t in trades:
                print(f"  {t.side} {t.product_id}: {t.explanation}")
            _print_status(engine)
        elif args.command == "run":
            logging.getLogger(__name__).info(
                "Starting loop: %d product(s), every %ds. Ctrl+C to stop.",
                len(config.products),
                config.poll_interval,
            )
            while True:
                engine.tick()
                time.sleep(config.poll_interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
