"""Tests for bot/market_data.py (CCXT-backed)."""

from unittest.mock import MagicMock, patch

import pytest

from bot.config import Config
from bot.market_data import MarketData, MarketDataError, _symbol, closed_candles


# ---------------------------------------------------------------------------
# Symbol conversion
# ---------------------------------------------------------------------------

def test_symbol_conversion():
    assert _symbol("BTC-USD") == "BTC/USD"
    assert _symbol("ETH-USD") == "ETH/USD"
    assert _symbol("SOL-USDT") == "SOL/USDT"


# ---------------------------------------------------------------------------
# get_candles
# ---------------------------------------------------------------------------

def _make_md(exchange_id="coinbase") -> tuple[MarketData, MagicMock]:
    cfg = Config()
    cfg.exchange = exchange_id
    cfg.candle_granularity = "ONE_HOUR"
    cfg.candle_count = 5
    md = MarketData(cfg)
    mock_exchange = MagicMock()
    md._exchange = lambda: mock_exchange
    return md, mock_exchange


def test_get_candles_returns_correct_format():
    md, mock_ex = _make_md()
    # CCXT returns [[timestamp_ms, open, high, low, close, volume], ...]
    mock_ex.fetch_ohlcv.return_value = [
        [1_700_000_000_000, 45000.0, 45500.0, 44800.0, 45200.0, 10.5],
        [1_700_003_600_000, 45200.0, 45600.0, 45100.0, 45400.0, 8.2],
    ]
    candles = md.get_candles("BTC-USD")
    assert len(candles) == 2
    assert candles[0] == {
        "time": 1_700_000_000,
        "open": 45000.0,
        "high": 45500.0,
        "low": 44800.0,
        "close": 45200.0,
        "volume": 10.5,
    }
    mock_ex.fetch_ohlcv.assert_called_once_with("BTC/USD", timeframe="1h", limit=5)


def test_get_candles_sorted_oldest_first():
    md, mock_ex = _make_md()
    mock_ex.fetch_ohlcv.return_value = [
        [1_700_003_600_000, 45200.0, 45600.0, 45100.0, 45400.0, 8.0],
        [1_700_000_000_000, 45000.0, 45500.0, 44800.0, 45200.0, 10.0],
    ]
    candles = md.get_candles("BTC-USD")
    assert candles[0]["time"] < candles[1]["time"]


def test_get_candles_raises_on_ccxt_error():
    import ccxt

    md, mock_ex = _make_md()
    mock_ex.fetch_ohlcv.side_effect = ccxt.NetworkError("timeout")
    with pytest.raises(MarketDataError):
        md.get_candles("BTC-USD")


# ---------------------------------------------------------------------------
# get_price
# ---------------------------------------------------------------------------

def test_get_price_uses_ticker():
    md, mock_ex = _make_md()
    mock_ex.fetch_ticker.return_value = {"last": 46000.0}
    price = md.get_price("BTC-USD")
    assert price == 46000.0
    mock_ex.fetch_ticker.assert_called_once_with("BTC/USD")


def test_get_price_falls_back_to_candle_on_ticker_failure():
    md, mock_ex = _make_md()
    mock_ex.fetch_ticker.side_effect = Exception("no ticker")
    mock_ex.fetch_ohlcv.return_value = [
        [1_700_000_000_000, 45000.0, 45500.0, 44800.0, 45200.0, 10.0],
    ]
    price = md.get_price("BTC-USD")
    assert price == 45200.0


def test_get_prices_returns_dict():
    md, mock_ex = _make_md()
    mock_ex.fetch_ticker.side_effect = [
        {"last": 46000.0},
        {"last": 3200.0},
    ]
    prices = md.get_prices(["BTC-USD", "ETH-USD"])
    assert prices == {"BTC-USD": 46000.0, "ETH-USD": 3200.0}


# ---------------------------------------------------------------------------
# closed_candles — drop the still-forming final bar
# ---------------------------------------------------------------------------

def _hourly(n: int, start: int = 1_700_000_000) -> list[dict]:
    return [
        {"time": start + i * 3600, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
        for i in range(n)
    ]


def test_closed_candles_drops_forming_bar():
    candles = _hourly(5)
    last_open = candles[-1]["time"]
    # "now" is 10 min into the last hourly candle -> it is still forming.
    result = closed_candles(candles, "ONE_HOUR", now=last_open + 600)
    assert len(result) == 4
    assert result[-1] is candles[-2]


def test_closed_candles_keeps_settled_bar():
    candles = _hourly(5)
    last_open = candles[-1]["time"]
    # "now" is past the end of the last candle's hour -> it has closed.
    result = closed_candles(candles, "ONE_HOUR", now=last_open + 3601)
    assert len(result) == 5


def test_closed_candles_unknown_granularity_is_noop():
    candles = _hourly(3)
    assert len(closed_candles(candles, "NOPE", now=candles[-1]["time"])) == 3


def test_closed_candles_short_series_unchanged():
    candles = _hourly(1)
    assert closed_candles(candles, "ONE_HOUR", now=candles[-1]["time"]) == candles
