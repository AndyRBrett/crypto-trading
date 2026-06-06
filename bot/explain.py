"""Claude-powered trade explanations.

Turns the structured signal + portfolio context into a short, plain-English
rationale a human can read in the dashboard. This is intentionally decoupled
from the trading logic: the decision is already made by the strategy, and
Claude only *describes* it. If no API key is configured or the call fails,
we fall back to a deterministic template built from the signal's reasons, so
the bot never depends on the LLM to function.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the analyst for a crypto paper-trading bot. Given a trade the bot "
    "just made and the signals behind it, write a concise, plain-English "
    "explanation of WHY the bot made this trade. 2-3 sentences. Be concrete: "
    "reference the indicators and what they imply. Do not give financial advice, "
    "hedge excessively, or add disclaimers. Write for a curious non-expert."
)


class Explainer:
    def __init__(self, config):
        self.config = config
        self._client = None
        self.enabled = config.explanations_enabled and bool(config.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        return self._client

    def explain(self, trade, portfolio, prices: dict[str, float]) -> str:
        if not self.enabled:
            return self._fallback(trade)
        try:
            return self._explain_with_claude(trade, portfolio, prices)
        except Exception as exc:  # network, auth, rate limit, etc.
            log.warning("Claude explanation failed (%s); using fallback.", exc)
            return self._fallback(trade)

    def _explain_with_claude(self, trade, portfolio, prices: dict[str, float]) -> str:
        equity = portfolio.total_equity(prices)
        pos = portfolio.position(trade.product_id)
        ind = trade.indicators or {}
        prompt = f"""Trade just executed:
- Action: {trade.side} {trade.quantity:.6f} {trade.product_id} @ ${trade.price:,.2f}
- Notional: ${trade.notional():,.2f}  (fee ${trade.fee:,.2f})
{f"- Realized P&L on this sell: ${trade.realized_pnl:,.2f}" if trade.side == "SELL" else ""}

Signals behind it:
{chr(10).join("- " + r for r in trade.reasons) or "- (none recorded)"}

Indicator snapshot:
- Fast SMA({ind.get('fast_period')}): {ind.get('fast_sma')}
- Slow SMA({ind.get('slow_period')}): {ind.get('slow_sma')}
- RSI({ind.get('rsi_period')}): {ind.get('rsi')}

Portfolio after the trade:
- Cash: ${portfolio.cash:,.2f}
- Position in {trade.product_id}: {pos.quantity:.6f} (avg ${pos.avg_price:,.2f})
- Total equity: ${equity:,.2f}

Explain why the bot made this trade."""

        client = self._get_client()
        resp = client.messages.create(
            model=self.config.explain_model,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in resp.content if b.type == "text"]
        return " ".join(parts).strip() or self._fallback(trade)

    @staticmethod
    def _fallback(trade) -> str:
        head = (
            f"{trade.side} {trade.quantity:.6f} {trade.product_id} "
            f"@ ${trade.price:,.2f}."
        )
        why = " ".join(trade.reasons) if trade.reasons else "Triggered by the strategy."
        return f"{head} {why}"
