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


class FakeStore:
    """Stands in for the Coordinator's shared-state-branch file."""

    def __init__(self, state=None):
        self.state = state
        self.pushes = 0

    def pull_sentiment_cache(self):
        return self.state

    def push_sentiment_cache(self, state):
        self.state = state
        self.pushes += 1
        return True


def _counting_analyzer(store=None, result=None):
    analyzer = SentimentAnalyzer(Config(), store=store)
    calls = {"n": 0}

    def fake(pid):
        calls["n"] += 1
        return result if result is not None else Sentiment(pid, 0.5, "bullish", "ok")

    analyzer._analyze_uncached = fake
    return analyzer, calls


def test_score_is_reused_for_the_whole_bar():
    analyzer, calls = _counting_analyzer()
    for _ in range(24):  # 24 hourly ticks inside one daily bar
        analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    assert calls["n"] == 1


def test_new_bar_triggers_a_rescore():
    analyzer, calls = _counting_analyzer()
    analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    analyzer.analyze("BTC-USD", bar_time=1_757_980_800)  # next daily bar
    assert calls["n"] == 2


def test_degraded_result_is_not_pinned_to_the_bar(monkeypatch):
    """A feed/API failure at the top of a bar must not freeze a placeholder
    neutral for the rest of the bar — the next tick retries."""
    analyzer, calls = _counting_analyzer(result=Sentiment.neutral("BTC-USD"))
    clock = {"t": 1_000.0}
    monkeypatch.setattr("bot.sentiment.time.time", lambda: clock["t"])
    analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    clock["t"] += analyzer.config.sentiment_cache_ttl + 1  # next tick
    analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    assert calls["n"] == 2


def test_degraded_result_is_reused_within_one_tick(monkeypatch):
    """Four of the five accounts hold BTC-USD. A failure must not be re-run
    (and, for a malformed response, re-billed) once per account in one tick —
    the TTL still applies to degraded results even under a bar key."""
    analyzer, calls = _counting_analyzer(result=Sentiment.neutral("BTC-USD"))
    monkeypatch.setattr("bot.sentiment.time.time", lambda: 1_000.0)
    for _ in range(4):  # one engine per account, same tick
        analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    assert calls["n"] == 1


def test_unpinned_caller_refreshes_within_the_bar(monkeypatch):
    """A held product must keep seeing news inside the bar: sentiment alone can
    force a risk-off SELL, so pinning it for a day would stretch the reaction
    window from an hour to a day."""
    analyzer, calls = _counting_analyzer()
    clock = {"t": 1_000.0}
    monkeypatch.setattr("bot.sentiment.time.time", lambda: clock["t"])
    bar = 1_757_894_400
    analyzer.analyze("BTC-USD", bar_time=bar, pin=False)
    clock["t"] += analyzer.config.sentiment_cache_ttl + 1  # an hour later
    analyzer.analyze("BTC-USD", bar_time=bar, pin=False)
    assert calls["n"] == 2  # same bar, but re-scored

    # ...while a flat caller stays pinned to the bar.
    flat, flat_calls = _counting_analyzer()
    flat.analyze("BTC-USD", bar_time=bar)
    clock["t"] += analyzer.config.sentiment_cache_ttl + 1
    flat.analyze("BTC-USD", bar_time=bar)
    assert flat_calls["n"] == 1


def test_refreshed_score_is_still_shared(monkeypatch):
    """An unpinned refresh keeps its bar key, so the other driver reuses the
    fresher score instead of paying to re-derive it."""
    monkeypatch.setattr("bot.sentiment.time.time", lambda: 1_000.0)
    store = FakeStore()
    analyzer, _ = _counting_analyzer(store=store)
    analyzer.analyze("BTC-USD", bar_time=1_757_894_400, pin=False)
    assert analyzer.flush() is True
    assert store.state["products"]["BTC-USD"]["bar"] == 1_757_894_400


def test_cache_survives_a_new_process():
    """The cloud runs `bot.main once` — a fresh process every tick — so the
    cache only saves anything if it round-trips through the shared store."""
    store = FakeStore()
    first, first_calls = _counting_analyzer(store=store)
    first.analyze("BTC-USD", bar_time=1_757_894_400)
    assert first.flush() is True

    second, second_calls = _counting_analyzer(store=store)
    got = second.analyze("BTC-USD", bar_time=1_757_894_400)
    assert second_calls["n"] == 0  # served from the shared cache
    assert got.score == 0.5 and got.label == "bullish"
    assert first_calls["n"] == 1


def test_flush_is_a_noop_when_nothing_was_scored():
    store = FakeStore()
    analyzer, _ = _counting_analyzer(store=store)
    analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    assert analyzer.flush() is True
    assert analyzer.flush() is False  # unchanged -> no second push
    assert store.pushes == 1


def test_degraded_results_are_never_shared():
    store = FakeStore()
    analyzer, _ = _counting_analyzer(store=store, result=Sentiment.neutral("BTC-USD"))
    analyzer.analyze("BTC-USD", bar_time=1_757_894_400)
    assert analyzer.flush() is False
    assert store.pushes == 0


def test_unreadable_shared_cache_is_not_fatal():
    class Broken(FakeStore):
        def pull_sentiment_cache(self):
            raise RuntimeError("network down")

    analyzer, calls = _counting_analyzer(store=Broken())
    assert analyzer.analyze("BTC-USD", bar_time=1).score == 0.5
    assert calls["n"] == 1


def test_sentiment_uses_a_cheap_model_by_default():
    # Scoring headlines is classification; the bill scales with tick frequency.
    assert Config().sentiment_model == "claude-haiku-4-5"


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


def test_one_bad_cache_entry_does_not_drop_the_rest():
    store = FakeStore(
        {
            "version": 1,
            "products": {
                "BTC-USD": {"bar": 1, "sentiment": {"score": "not-a-number"}},
                "ETH-USD": {
                    "bar": 1,
                    "sentiment": {"score": -0.3, "label": "bearish", "summary": "x"},
                },
            },
        }
    )
    analyzer, calls = _counting_analyzer(store=store)
    assert analyzer.analyze("ETH-USD", bar_time=1).label == "bearish"
    assert calls["n"] == 0  # the good entry survived the bad one
    analyzer.analyze("BTC-USD", bar_time=1)
    assert calls["n"] == 1  # the bad entry was dropped and re-scored
