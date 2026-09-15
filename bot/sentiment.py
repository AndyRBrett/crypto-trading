"""News sentiment analysis.

Pulls recent crypto headlines from keyless RSS feeds, has Claude score the
near-term sentiment for a given asset, and returns a structured score the
strategy folds into its signals.

Fully optional and safe to fail: disabled by default, results are cached with
a TTL to avoid hammering the feeds/LLM, and every failure path (no API key, no
network, no relevant headlines, bad response) degrades to a neutral score so
the bot keeps trading on price action alone.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree

import requests

log = logging.getLogger(__name__)

# Keyless RSS feeds — no API key required.
DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# Map a product's base symbol to the names likely to appear in headlines.
SYMBOL_NAMES = {
    "BTC": ["bitcoin"],
    "ETH": ["ethereum", "ether"],
    "SOL": ["solana"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin"],
    "ADA": ["cardano"],
    "LTC": ["litecoin"],
    "AVAX": ["avalanche"],
    "LINK": ["chainlink"],
    "MATIC": ["polygon"],
    "DOT": ["polkadot"],
}

SENTIMENT_SYSTEM = (
    "You are a crypto market analyst. Given recent news headlines about an "
    "asset, score the overall NEAR-TERM market sentiment for that asset on a "
    "scale from -1.0 (very bearish) through 0.0 (neutral) to +1.0 (very "
    "bullish). Base your judgment only on the supplied headlines. Return a "
    "score, a label (bearish/neutral/bullish), and a one-sentence summary of "
    "the dominant narrative. Do not give financial advice."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "label": {"type": "string", "enum": ["bearish", "neutral", "bullish"]},
        "summary": {"type": "string"},
    },
    "required": ["score", "label", "summary"],
    "additionalProperties": False,
}


@dataclass
class Sentiment:
    product_id: str
    score: float  # -1 (very bearish) .. +1 (very bullish)
    label: str  # bearish / neutral / bullish
    summary: str
    headline_count: int = 0
    headlines: list[str] = field(default_factory=list)
    # True only for a score Claude actually produced. Degraded results (no key,
    # feed failure, no relevant headlines, unparseable response) are neutral
    # placeholders and must NOT be pinned to a bar — see SentimentAnalyzer.
    # Internal only: deliberately absent from to_dict(), which feeds the
    # dashboard's stable state.json shape.
    ok: bool = True

    @classmethod
    def neutral(cls, product_id: str, summary: str = "No sentiment available.") -> "Sentiment":
        return cls(product_id, 0.0, "neutral", summary, 0, [], ok=False)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "label": self.label,
            "summary": self.summary,
            "headline_count": self.headline_count,
            "headlines": self.headlines,
        }

    @classmethod
    def from_dict(cls, product_id: str, data: dict) -> "Sentiment":
        """Rebuild a scored result from its to_dict() form (cache reload)."""
        return cls(
            product_id=product_id,
            score=float(data["score"]),
            label=str(data.get("label", "neutral")),
            summary=str(data.get("summary", "")),
            headline_count=int(data.get("headline_count", 0)),
            headlines=list(data.get("headlines", [])),
        )


def keywords_for(product_id: str) -> set[str]:
    base = product_id.split("-")[0].upper()
    kws = {base.lower()}
    kws.update(SYMBOL_NAMES.get(base, []))
    return kws


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


# Cap feed responses before parsing — an oversized or maliciously crafted RSS
# body (e.g. a compromised feed doing entity-expansion) shouldn't be able to
# tie up the parser or the process's memory.
_MAX_FEED_BYTES = 5 * 1024 * 1024


class NewsFeed:
    def __init__(self, feeds: list[str]):
        self.feeds = feeds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-paper-bot/0.1"})

    @staticmethod
    def parse(content: bytes | str, limit: int = 40) -> list[dict]:
        """Parse RSS bytes into a list of {title, summary} dicts."""
        if len(content) > _MAX_FEED_BYTES:
            raise ValueError(f"feed body too large ({len(content)} bytes)")
        root = ElementTree.fromstring(content)
        items: list[dict] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            summary = _strip_html(item.findtext("description") or "")
            items.append({"title": title, "summary": summary[:300]})
            if len(items) >= limit:
                break
        return items

    def fetch(self, limit_per_feed: int = 40) -> list[dict]:
        out: list[dict] = []
        for url in self.feeds:
            try:
                resp = self.session.get(url, timeout=15, stream=True)
                resp.raise_for_status()
                content = resp.raw.read(_MAX_FEED_BYTES + 1, decode_content=True)
                if len(content) > _MAX_FEED_BYTES:
                    raise ValueError(f"feed body too large (>{_MAX_FEED_BYTES} bytes)")
                out.extend(self.parse(content, limit_per_feed))
            except Exception as exc:
                log.warning("news feed failed %s: %s", url, exc)
        return out


@dataclass
class _CacheEntry:
    fetched_at: float
    bar_time: int | None
    sentiment: Sentiment


class SentimentAnalyzer:
    """Scores sentiment per product, reusing a score for the whole candle.

    Scoring costs an API call, so the aim is one call per product per *decision*.
    Sentiment only ever reaches the strategies through ``apply_sentiment`` at
    signal time, and signals are generated from settled candles — so on the
    daily timeframe the same score informs every tick of the day, and re-scoring
    hourly buys nothing but tokens.

    Two caches serve that:

    * **Bar cache** — when ``analyze`` is given the settled bar's timestamp, a
      score is reused until that bar rolls over. Passed a ``store``, the entries
      survive the process, which is what makes it work at all in the cloud:
      ``bot.main once`` is a fresh process every tick, so an in-memory cache is
      always empty on startup.
    * **TTL cache** — the fallback when no bar timestamp is available, and the
      only cache used for degraded results, so a feed blip at the top of a bar
      is retried next tick instead of pinning a placeholder neutral all day.
    """

    def __init__(self, config, store=None):
        self.config = config
        self.feed = NewsFeed(config.news_feeds)
        self._client = None
        self._cache: dict[str, _CacheEntry] = {}
        # Optional persistence for the bar cache: any object exposing
        # pull_sentiment_cache() / push_sentiment_cache(state). The Coordinator
        # implements it against the shared state branch.
        self._store = store
        self._loaded = store is None
        self._dirty = False
        self.enabled = bool(config.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        return self._client

    # -- persistence -------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load the shared bar cache once, on first use. Never fatal."""
        if self._loaded:
            return
        self._loaded = True  # set first: a failed load must not retry per product
        try:
            state = self._store.pull_sentiment_cache() or {}
            for product_id, entry in (state.get("products") or {}).items():
                # Per-entry guard: one malformed product must not cost us the
                # whole cache (and so a full re-score of every other asset).
                try:
                    bar = entry.get("bar")
                    if bar is None:
                        continue
                    self._cache[product_id] = _CacheEntry(
                        fetched_at=float(entry.get("fetched_at", 0.0)),
                        bar_time=int(bar),
                        sentiment=Sentiment.from_dict(product_id, entry["sentiment"]),
                    )
                except Exception as exc:
                    log.warning(
                        "sentiment: skipping unreadable cache entry %s: %s",
                        product_id,
                        exc,
                    )
            log.info("sentiment: loaded %d cached score(s).", len(self._cache))
        except Exception as exc:
            log.warning("sentiment: could not load shared cache: %s", exc)

    def flush(self) -> bool:
        """Persist the bar cache if it changed. Returns True when written."""
        if not self._store or not self._dirty:
            return False
        state = {
            "version": 1,
            "products": {
                product_id: {
                    "bar": entry.bar_time,
                    "fetched_at": round(entry.fetched_at, 3),
                    "sentiment": entry.sentiment.to_dict(),
                }
                for product_id, entry in sorted(self._cache.items())
                # Only scored results are worth sharing; a placeholder neutral
                # would suppress the next tick's retry on every other driver.
                if entry.bar_time is not None and entry.sentiment.ok
            },
        }
        try:
            ok = bool(self._store.push_sentiment_cache(state))
        except Exception as exc:
            log.warning("sentiment: could not persist shared cache: %s", exc)
            return False
        if ok:
            self._dirty = False
        return ok

    # -- scoring -----------------------------------------------------------

    def _is_fresh(self, entry: _CacheEntry, bar_time: int | None, now: float) -> bool:
        if bar_time is not None and entry.bar_time is not None:
            # Same settled bar -> the headlines behind the decision haven't
            # been superseded yet. Only a scored result is pinned this way.
            return entry.bar_time == bar_time and entry.sentiment.ok
        return now - entry.fetched_at < self.config.sentiment_cache_ttl

    def analyze(self, product_id: str, bar_time: int | None = None) -> Sentiment:
        """Score ``product_id``, reusing the score for the settled bar.

        ``bar_time`` is the open timestamp of the last settled candle — the bar
        the strategy will actually decide on. Omit it to fall back to the TTL
        cache (the long-running local loop, and every existing caller).
        """
        self._ensure_loaded()
        now = time.time()
        cached = self._cache.get(product_id)
        if cached and self._is_fresh(cached, bar_time, now):
            return cached.sentiment
        sentiment = self._analyze_uncached(product_id)
        self._cache[product_id] = _CacheEntry(now, bar_time, sentiment)
        if bar_time is not None and sentiment.ok:
            self._dirty = True
        return sentiment

    def _relevant_headlines(self, product_id: str) -> list[dict]:
        kws = keywords_for(product_id)
        items = self.feed.fetch()
        relevant = []
        for it in items:
            hay = (it["title"] + " " + it["summary"]).lower()
            if any(k in hay for k in kws):
                relevant.append(it)
        return relevant

    def _analyze_uncached(self, product_id: str) -> Sentiment:
        if not self.enabled:
            return Sentiment.neutral(
                product_id, "Sentiment disabled (no ANTHROPIC_API_KEY)."
            )
        try:
            headlines = self._relevant_headlines(product_id)
        except Exception as exc:
            log.warning("news fetch failed: %s", exc)
            return Sentiment.neutral(product_id, "News fetch failed.")
        if not headlines:
            return Sentiment.neutral(product_id, "No recent relevant headlines.")
        headlines = headlines[: self.config.sentiment_max_headlines]
        try:
            return self._score_with_claude(product_id, headlines)
        except Exception as exc:
            log.warning("sentiment scoring failed: %s", exc)
            return Sentiment.neutral(product_id, "Sentiment scoring failed.")

    def _score_with_claude(self, product_id: str, headlines: list[dict]) -> Sentiment:
        titles = [h["title"] for h in headlines]
        listing = "\n".join(f"- {t}" for t in titles)
        prompt = (
            f"Asset: {product_id}\n\nRecent headlines:\n{listing}\n\n"
            "Score the overall near-term market sentiment for this asset."
        )
        resp = self._get_client().messages.create(
            model=self.config.sentiment_model,
            max_tokens=500,
            system=SENTIMENT_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        score = max(-1.0, min(1.0, float(data["score"])))
        return Sentiment(
            product_id=product_id,
            score=score,
            label=data.get("label", "neutral"),
            summary=data.get("summary", ""),
            headline_count=len(titles),
            headlines=titles[:5],
        )
