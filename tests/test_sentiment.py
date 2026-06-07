from types import SimpleNamespace

from bot.config import Config
from bot.sentiment import NewsFeed, Sentiment, SentimentAnalyzer, keywords_for
from bot.strategy import BUY, HOLD, SELL, Strategy, StrategyConfig

SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test</title>
  <item><title>Bitcoin surges to new highs</title>
        <description>&lt;p&gt;BTC rallies hard&lt;/p&gt;</description></item>
  <item><title>Ethereum upgrade ships</title>
        <description>ETH network improves</description></item>
  <item><title></title><description>empty title skipped</description></item>
</channel></rss>"""


def test_keywords_for():
    assert "bitcoin" in keywords_for("BTC-USD")
    assert "btc" in keywords_for("BTC-USD")
    assert "ethereum" in keywords_for("ETH-USD")


def test_news_parse_extracts_items_and_strips_html():
    items = NewsFeed.parse(SAMPLE_RSS)
    assert len(items) == 2  # empty-title item skipped
    assert items[0]["title"] == "Bitcoin surges to new highs"
    assert "<p>" not in items[0]["summary"]
    assert items[0]["summary"] == "BTC rallies hard"


def test_news_parse_respects_limit():
    assert len(NewsFeed.parse(SAMPLE_RSS, limit=1)) == 1


def test_analyzer_neutral_without_api_key():
    cfg = Config()  # no ANTHROPIC_API_KEY
    analyzer = SentimentAnalyzer(cfg)
    s = analyzer.analyze("BTC-USD")
    assert s.score == 0.0 and s.label == "neutral"


def test_analyzer_caches_results():
    cfg = Config()
    analyzer = SentimentAnalyzer(cfg)
    calls = {"n": 0}

    def fake(pid):
        calls["n"] += 1
        return Sentiment(pid, 0.5, "bullish", "ok")

    analyzer._analyze_uncached = fake
    analyzer.analyze("BTC-USD")
    analyzer.analyze("BTC-USD")
    assert calls["n"] == 1  # second call served from cache


def _strategy():
    return Strategy(
        StrategyConfig(
            fast_period=2, slow_period=4, ma_type="sma", rsi_period=2,
            rsi_overbought=95.0, rsi_oversold=5.0,
            trend_filter=False, adx_filter=False,
            sentiment_buy_veto=-0.4, sentiment_sell_trigger=-0.6,
        )
    )


def candles(closes):
    return [{"close": c} for c in closes]


def test_bearish_sentiment_vetoes_buy():
    s = _strategy()
    bearish = Sentiment("BTC-USD", -0.8, "bearish", "Regulatory crackdown.")
    sig = s.generate_signal("BTC-USD", candles([10, 10, 10, 10, 8, 13]), sentiment=bearish)
    assert sig.action == HOLD
    assert sig.indicators["sentiment_score"] == -0.8


def test_positive_sentiment_keeps_buy():
    s = _strategy()
    bullish = Sentiment("BTC-USD", 0.6, "bullish", "ETF inflows.")
    sig = s.generate_signal("BTC-USD", candles([10, 10, 10, 10, 8, 13]), sentiment=bullish)
    assert sig.action == BUY
    assert sig.indicators["sentiment_label"] == "bullish"


def test_strongly_bearish_sentiment_triggers_sell():
    s = _strategy()
    bearish = Sentiment("BTC-USD", -0.9, "bearish", "Exchange hack.")
    # Choppy market -> would HOLD on price alone; sentiment forces risk-off SELL.
    sig = s.generate_signal("BTC-USD", candles([10, 11, 10, 11, 10, 11]), sentiment=bearish)
    assert sig.action == SELL


def test_no_sentiment_is_backwards_compatible():
    s = _strategy()
    sig = s.generate_signal("BTC-USD", candles([10, 11, 10, 11, 10, 11]))
    assert sig.action == HOLD
    assert "sentiment_score" not in sig.indicators
