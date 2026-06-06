"""Market data access.

Two backends, selected by ``config.data_source``:

  * "public" (default) — Coinbase's public Exchange API. No auth required,
    works immediately, and returns the same prices used for paper fills.
  * "coinbase_advanced" — the official ``coinbase-advanced-py`` SDK using your
    CDP API key + secret. Useful to verify your keys work and to use the same
    data feed you'd trade against live.

Both return candles as a list of dicts ordered oldest -> newest:
    {"time": int, "open": float, "high": float, "low": float,
     "close": float, "volume": float}
"""

from __future__ import annotations

import time
from typing import Sequence

import requests

PUBLIC_BASE = "https://api.exchange.coinbase.com"

# Map Advanced-Trade granularity names to Exchange API seconds.
_GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "ONE_HOUR": 3600,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


class MarketDataError(Exception):
    pass


class MarketData:
    def __init__(self, config):
        self.config = config
        self._rest_client = None  # lazy Advanced-Trade client
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "crypto-paper-bot/0.1"})

    # -- public Exchange API ----------------------------------------------

    def _public_candles(self, product_id: str, granularity: str, count: int):
        seconds = _GRANULARITY_SECONDS.get(granularity, 3600)
        end = int(time.time())
        start = end - seconds * count
        url = f"{PUBLIC_BASE}/products/{product_id}/candles"
        resp = self._session.get(
            url,
            params={"granularity": seconds, "start": start, "end": end},
            timeout=15,
        )
        resp.raise_for_status()
        # Exchange returns [[time, low, high, open, close, volume], ...] newest first.
        rows = resp.json()
        candles = [
            {
                "time": int(r[0]),
                "low": float(r[1]),
                "high": float(r[2]),
                "open": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in rows
        ]
        candles.sort(key=lambda c: c["time"])
        return candles[-count:]

    def _public_price(self, product_id: str) -> float:
        url = f"{PUBLIC_BASE}/products/{product_id}/ticker"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        return float(resp.json()["price"])

    # -- Advanced Trade SDK ------------------------------------------------

    def _client(self):
        if self._rest_client is None:
            try:
                from coinbase.rest import RESTClient
            except ImportError as exc:  # pragma: no cover - import guard
                raise MarketDataError(
                    "coinbase-advanced-py not installed. Run "
                    "`pip install coinbase-advanced-py` or set data_source: public."
                ) from exc
            if not (self.config.coinbase_api_key and self.config.coinbase_api_secret):
                raise MarketDataError(
                    "COINBASE_API_KEY / COINBASE_API_SECRET not set; "
                    "cannot use data_source: coinbase_advanced."
                )
            self._rest_client = RESTClient(
                api_key=self.config.coinbase_api_key,
                api_secret=self.config.coinbase_api_secret,
            )
        return self._rest_client

    def _advanced_candles(self, product_id: str, granularity: str, count: int):
        seconds = _GRANULARITY_SECONDS.get(granularity, 3600)
        end = int(time.time())
        start = end - seconds * count
        resp = self._client().get_candles(
            product_id=product_id,
            start=str(start),
            end=str(end),
            granularity=granularity,
        )
        raw = getattr(resp, "candles", None)
        if raw is None and isinstance(resp, dict):
            raw = resp.get("candles", [])
        candles = []
        for c in raw or []:
            get = c.get if isinstance(c, dict) else lambda k, _c=c: getattr(_c, k)
            candles.append(
                {
                    "time": int(get("start", 0)),
                    "low": float(get("low", 0)),
                    "high": float(get("high", 0)),
                    "open": float(get("open", 0)),
                    "close": float(get("close", 0)),
                    "volume": float(get("volume", 0)),
                }
            )
        candles.sort(key=lambda c: c["time"])
        return candles[-count:]

    # -- public interface --------------------------------------------------

    def get_candles(
        self, product_id: str, granularity: str | None = None, count: int | None = None
    ):
        granularity = granularity or self.config.candle_granularity
        count = count or self.config.candle_count
        if self.config.data_source == "coinbase_advanced":
            return self._advanced_candles(product_id, granularity, count)
        return self._public_candles(product_id, granularity, count)

    def get_price(self, product_id: str) -> float:
        """Latest price. Falls back to the most recent candle close."""
        try:
            if self.config.data_source == "public":
                return self._public_price(product_id)
        except Exception:
            pass
        candles = self.get_candles(product_id, count=2)
        if not candles:
            raise MarketDataError(f"no price available for {product_id}")
        return candles[-1]["close"]

    def get_prices(self, product_ids: Sequence[str]) -> dict[str, float]:
        return {pid: self.get_price(pid) for pid in product_ids}

    def verify_credentials(self) -> bool:
        """Best-effort check that Coinbase Advanced keys authenticate."""
        try:
            self._client().get_accounts()
            return True
        except Exception:
            return False
