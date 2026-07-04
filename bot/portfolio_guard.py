"""Cross-account portfolio guard: one view of the combined book, optional cap.

The paper accounts are deliberately independent — separate DBs, separate cash,
separate risk settings — which means nothing constrains their *combined*
footprint: all five sleeves can lean the same way on highly correlated assets
at once. This module adds that missing portfolio-level view in two phases:

* **Read-only (always on when wired):** :meth:`PortfolioGuard.snapshot` sums
  gross long / gross short / net exposure and combined equity across every
  registered engine, priced with the freshest prices available. The Runner
  logs it each tick; the dashboard already shows the same aggregation from
  the combined export.

* **Entry veto (opt-in, ``portfolio_guard_enabled: true``):** before an engine
  opens a NEW position (long entry or short entry), it asks
  :meth:`allows_entry`; the guard vetoes when combined gross exposure plus the
  new notional would exceed ``max_gross_exposure_pct`` × combined equity.
  Vetoes are logged to signal_log as ``portfolio_exposure``.

The guard NEVER touches exits: closes, covers, and protective stops are not
consulted — an over-cap book can always reduce risk. Disabled by default, so
wiring it in changes nothing until the flag is flipped.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class PortfolioGuard:
    def __init__(self, config):
        self.enabled = bool(getattr(config, "portfolio_guard_enabled", False))
        self.max_gross_exposure_pct = float(
            getattr(config, "max_gross_exposure_pct", 1.5)
        )
        self._engines: list = []

    def register(self, engine) -> None:
        """Add an account engine (anything with .portfolio and .last_prices)."""
        self._engines.append(engine)

    def _merged_prices(self, extra_prices: dict | None) -> dict:
        """Freshest known price per product: every engine's last tick snapshot,
        overlaid with the calling engine's current-tick prices."""
        merged: dict = {}
        for e in self._engines:
            merged.update(getattr(e, "last_prices", None) or {})
        if extra_prices:
            merged.update(extra_prices)
        return merged

    def snapshot(self, extra_prices: dict | None = None) -> dict:
        """Combined book across all registered engines.

        Positions with no known price are marked at their entry price — a
        stale mark beats silently valuing an open position at zero.
        """
        prices = self._merged_prices(extra_prices)
        gross_long = gross_short = equity = 0.0
        by_asset: dict[str, float] = {}
        for e in self._engines:
            p = e.portfolio
            equity += p.cash
            for pid, pos in p.positions.items():
                if pos.quantity == 0:
                    continue
                value = pos.quantity * prices.get(pid, pos.avg_price)
                equity += value
                by_asset[pid] = by_asset.get(pid, 0.0) + value
                if value >= 0:
                    gross_long += value
                else:
                    gross_short += -value
        return {
            "equity": equity,
            "gross_long": gross_long,
            "gross_short": gross_short,
            "gross": gross_long + gross_short,
            "net_exposure": gross_long - gross_short,
            "by_asset": by_asset,
        }

    def allows_entry(
        self, notional: float, extra_prices: dict | None = None
    ) -> tuple[bool, str]:
        """May a NEW position of ``notional`` USD be opened right now?

        Only consulted for entries; exits never come through here. When the
        guard is disabled this is always a yes, so the wiring itself changes
        no behavior.
        """
        if not self.enabled:
            return True, ""
        snap = self.snapshot(extra_prices)
        cap = self.max_gross_exposure_pct * snap["equity"]
        if snap["equity"] <= 0:
            return False, "combined equity is non-positive"
        if snap["gross"] + abs(notional) > cap:
            return False, (
                f"combined gross exposure ${snap['gross']:,.2f} + new "
                f"${abs(notional):,.2f} would exceed the cap ${cap:,.2f} "
                f"({self.max_gross_exposure_pct:.0%} of ${snap['equity']:,.2f} equity)"
            )
        return True, ""
