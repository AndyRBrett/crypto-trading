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

    @classmethod
    def neutral(cls, product_id: str, summary: str = "No sentiment available.") -> "Sentiment":
        return cls(product_id, 0.0, "neutral", summary, 0, [])

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "label": self.label,
            "summary": self.summary,
            "headline_count": self.headline_count,
            "headlines": self.headlines,
        }


def keywords_for(product_id: str) -> set[str]:
    base = product_id.split("-")[0].upper()
    kws = {base.lower()}
    kws.update(SYMBOL_NAMES.get(base, []))
    return kws


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


class NewsFeed:
    def __init__(self, feeds: list[str]):
        self.feeds = feeds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-paper-bot/0.1"})

    @staticmethod
    def parse(content: bytes | str, limit: int = 40) -> list[dict]:
        """Parse RSS bytes into a list of {title, summary} dicts."""
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
                resp = self.session.get(url, timeout=15)
                resp.raise_for_status()
                out.extend(self.parse(resp.content, limit_per_feed))
            except Exception as exc:
                log.warning("news feed failed %s: %s", url, exc)
        return out


class SentimentAnalyzer:
    def __init__(self, config):
        self.config = config
        self.feed = NewsFeed(config.news_feeds)
        self._client = None
        self._cache: dict[str, tuple[float, Sentiment]] = {}
        self.enabled = bool(config.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        return self._client

    def analyze(self, product_id: str) -> Sentiment:
        now = time.time()
        cached = self._cache.get(product_id)
        if cached and now - cached[0] < self.config.sentiment_cache_ttl:
            return cached[1]
        sentiment = self._analyze_uncached(product_id)
        self._cache[product_id] = (now, sentiment)
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
