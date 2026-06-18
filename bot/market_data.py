"""Market data via CCXT — a unified interface to 100+ exchanges.

The exchange is selected by ``config.exchange`` (any CCXT exchange id, default
``"coinbase"``). Credentials are optional: if ``COINBASE_API_KEY`` /
``COINBASE_API_SECRET`` are present they are forwarded for authenticated access;
otherwise the public REST API is used with no keys required.

Candles are returned oldest → newest as dicts:
    {"time": int, "open": float, "high": float, "low": float,
     "close": float, "volume": float}
"""

from __future__ import annotations

from typing import Sequence

import ccxt

_TIMEFRAMES: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "THIRTY_MINUTE": "30m",
    "ONE_HOUR": "1h",
    "TWO_HOUR": "2h",
    "SIX_HOUR": "6h",
    "ONE_DAY": "1d",
}

# Length of each granularity in seconds, used to tell whether the most recent
# candle has closed yet (see ``closed_candles``).
_GRANULARITY_SECONDS: dict[str, int] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 5 * 60,
    "FIFTEEN_MINUTE": 15 * 60,
    "THIRTY_MINUTE": 30 * 60,
    "ONE_HOUR": 60 * 60,
    "TWO_HOUR": 2 * 60 * 60,
    "SIX_HOUR": 6 * 60 * 60,
    "ONE_DAY": 24 * 60 * 60,
}


def closed_candles(
    candles: Sequence[dict], granularity: str, now: float | None = None
) -> list[dict]:
    """Return ``candles`` with the still-forming final bar dropped.

    Exchanges return the current, in-progress period as the most recent candle.
    When the bot ticks more often than the candle granularity (e.g. every 15 min
    on hourly candles), that last bar is a moving target: signals derived from it
    flip-flop and the same crossover gets re-detected on every tick. Strategy
    entries should only ever look at *settled* candles, so drop the final bar
    while it is still inside its own period. Protective stops still react to the
    live price separately, which is the whole point of ticking intra-candle.
    """
    import time as _time

    candles = list(candles)
    if len(candles) < 2:
        return candles
    span = _GRANULARITY_SECONDS.get(granularity)
    if not span:
        return candles
    now = _time.time() if now is None else now
    last_open = candles[-1].get("time")
    if last_open is None:
        return candles
    # If we're still inside the last candle's period, it hasn't closed yet.
    if now < last_open + span:
        return candles[:-1]
    return candles

# Module-level cache so load_markets() is called once per (exchange, auth) pair.
_exchange_cache: dict[tuple, ccxt.Exchange] = {}


class MarketDataError(Exception):
    pass


def _symbol(product_id: str) -> str:
    """Convert Coinbase-style product id to a CCXT symbol: BTC-USD → BTC/USD."""
    return product_id.replace("-", "/")


def _make_exchange(exchange_id: str, api_key: str = "", api_secret: str = "") -> ccxt.Exchange:
    cache_key = (exchange_id, bool(api_key))
    if cache_key not in _exchange_cache:
        cls = getattr(ccxt, exchange_id, None)
        if cls is None:
            raise MarketDataError(f"Unknown CCXT exchange id: {exchange_id!r}")
        params: dict = {}
        if api_key and api_secret:
            params = {"apiKey": api_key, "secret": api_secret}
        _exchange_cache[cache_key] = cls(params)
    return _exchange_cache[cache_key]


class MarketData:
    def __init__(self, config):
        self.config = config

    def _exchange(self) -> ccxt.Exchange:
        return _make_exchange(
            self.config.exchange,
            self.config.coinbase_api_key,
            self.config.coinbase_api_secret,
        )

    def get_candles(
        self, product_id: str, granularity: str | None = None, count: int | None = None
    ) -> list[dict]:
        granularity = granularity or self.config.candle_granularity
        count = count or self.config.candle_count
        timeframe = _TIMEFRAMES.get(granularity, "1h")
        symbol = _symbol(product_id)
        try:
            raw = self._exchange().fetch_ohlcv(symbol, timeframe=timeframe, limit=count)
        except ccxt.BaseError as exc:
            raise MarketDataError(f"fetch_ohlcv failed for {symbol}: {exc}") from exc
        # CCXT returns [[timestamp_ms, open, high, low, close, volume], ...] oldest → newest.
        candles = [
            {
                "time": int(row[0]) // 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in raw
        ]
        candles.sort(key=lambda c: c["time"])
        return candles[-count:]

    def get_history(
        self, product_id: str, granularity: str | None = None, count: int = 2000
    ) -> list[dict]:
        """Fetch up to ``count`` candles by paginating backward in time.

        Exchanges cap ``fetch_ohlcv`` at a few hundred candles per request
        (Coinbase ≈ 300), so a single call can't supply enough history for a
        meaningful backtest. This walks forward from ``now - count*timeframe`` in
        pages until it has enough (or the exchange runs out), de-duplicating on
        timestamp. Used by the backtester/sweep; live trading uses get_candles.
        """
        granularity = granularity or self.config.candle_granularity
        timeframe = _TIMEFRAMES.get(granularity, "1h")
        symbol = _symbol(product_id)
        ex = self._exchange()
        try:
            tf_ms = ex.parse_timeframe(timeframe) * 1000
        except Exception:
            tf_ms = _GRANULARITY_SECONDS.get(granularity, 3600) * 1000
        now_ms = ex.milliseconds()
        since = now_ms - count * tf_ms

        by_time: dict[int, list] = {}
        max_pages = count // 150 + 25  # safety bound (room to skip pre-listing gaps)
        for _ in range(max_pages):
            try:
                batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=300)
            except ccxt.BaseError as exc:
                raise MarketDataError(f"fetch_ohlcv failed for {symbol}: {exc}") from exc
            if not batch:
                # No data at `since`. If we haven't collected anything yet, the
                # requested start likely predates the asset's listing — jump
                # forward a page and keep looking instead of giving up.
                if not by_time and since < now_ms:
                    since += 300 * tf_ms
                    continue
                break
            for row in batch:
                by_time[int(row[0])] = row
            last_ts = int(batch[-1][0])
            next_since = last_ts + tf_ms
            if next_since <= since or next_since >= now_ms or len(by_time) >= count:
                break
            since = next_since

        candles = [
            {
                "time": ts // 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for ts, row in by_time.items()
        ]
        candles.sort(key=lambda c: c["time"])
        return candles[-count:]

    def get_price(self, product_id: str) -> float:
        """Latest price from the exchange ticker, falling back to the last candle close."""
        symbol = _symbol(product_id)
        try:
            ticker = self._exchange().fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if price:
                return float(price)
        except Exception:
            pass
        candles = self.get_candles(product_id, count=2)
        if not candles:
            raise MarketDataError(f"no price available for {product_id}")
        return candles[-1]["close"]

    def get_prices(self, product_ids: Sequence[str]) -> dict[str, float]:
        return {pid: self.get_price(pid) for pid in product_ids}

    def verify_credentials(self) -> bool:
        """Best-effort check that the configured exchange keys authenticate."""
        try:
            self._exchange().fetch_balance()
            return True
        except Exception:
            return False
