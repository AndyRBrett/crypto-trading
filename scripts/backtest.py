#!/usr/bin/env python3
"""Backtest every configured account/strategy on historical candles.

Usage:
    python -m scripts.backtest                 # uses config.yaml (or defaults)
    python -m scripts.backtest --config my.yaml --count 1000 --granularity ONE_HOUR

Fetches candles from the configured exchange and replays each account's strategy
through the same risk layer the live engine uses, reporting return, drawdown,
win-rate and fees so you can compare changes *before* shipping them to live
paper trading.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from bot.backtest import run_backtest
from bot.config import Config
from bot.market_data import MarketData
from bot.strategies import make_strategy


def _resolve(base: Config, acct, fee_override=None) -> Config:
    """Per-account Config with None risk overrides inherited from the base.

    ``fee_override`` (when not None) forces the fee rate for every account, so a
    single run can model a different cost assumption (maker fees, a higher volume
    tier) without editing config.
    """

    def pick(override, fallback):
        return fallback if override is None else override

    fee = fee_override if fee_override is not None else pick(acct.fee_rate, base.fee_rate)
    return dataclasses.replace(
        base,
        products=acct.products,
        starting_cash=acct.starting_cash,
        strategy=acct.strategy,
        strategy_type=acct.strategy_type,
        account_name=acct.name,
        fee_rate=fee,
        risk_per_trade_pct=pick(acct.risk_per_trade_pct, base.risk_per_trade_pct),
        max_position_pct=pick(acct.max_position_pct, base.max_position_pct),
        max_open_positions=pick(acct.max_open_positions, base.max_open_positions),
        stop_loss_atr_mult=pick(acct.stop_loss_atr_mult, base.stop_loss_atr_mult),
        take_profit_atr_mult=pick(acct.take_profit_atr_mult, base.take_profit_atr_mult),
        trailing_stop=pick(acct.trailing_stop, base.trailing_stop),
        fallback_stop_pct=pick(acct.fallback_stop_pct, base.fallback_stop_pct),
        allow_short=pick(acct.allow_short, base.allow_short),
        accounts=[],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backtest configured strategies.")
    parser.add_argument("--config", default="config.yaml", help="config YAML path")
    parser.add_argument("--count", type=int, default=None, help="candles to fetch")
    parser.add_argument("--granularity", default=None, help="override candle granularity")
    parser.add_argument("--fee", type=float, default=None,
                        help="override fee rate for all accounts (e.g. 0.0025 for maker)")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    granularity = args.granularity or config.candle_granularity
    count = args.count or max(config.candle_count, 2000)
    market = MarketData(config)

    eff_fee = args.fee if args.fee is not None else config.fee_rate
    print(
        f"Backtest — exchange={config.exchange} granularity={granularity} "
        f"candles≈{count} fee={eff_fee:.4f}\n"
        + "-" * 100
    )

    any_run = False
    for acct in config.accounts:
        cfg = _resolve(config, acct, fee_override=args.fee)
        strategy = make_strategy(cfg.strategy_type, cfg.strategy)
        for product in cfg.products:
            try:
                candles = market.get_history(product, granularity=granularity, count=count)
            except Exception as exc:  # pragma: no cover - network/exchange errors
                print(f"  [{acct.name}] {product}: fetch failed: {exc}", file=sys.stderr)
                continue
            if len(candles) <= strategy.min_candles():
                print(
                    f"  [{acct.name}] {product}: not enough candles "
                    f"({len(candles)} ≤ {strategy.min_candles()} needed)",
                    file=sys.stderr,
                )
                continue
            result = run_backtest(strategy, candles, cfg, product_id=product)
            print(f"  [{acct.name:<14}] {result.summary()}")
            any_run = True

    if not any_run:
        print("No backtests ran — check config, network, and candle availability.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
