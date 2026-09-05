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

* **Correlation/concentration veto (opt-in, ``correlation_guard_enabled``):**
  caps per-asset gross notional and the square root of gross-weighted absolute
  correlations, normalized by equity. Missing history assumes correlation 1.
  This is an exposure proxy, not statistical market beta or a VaR estimate.

The guard NEVER touches exits: closes, covers, and protective stops are not
consulted — an over-cap book can always reduce risk. Disabled by default, so
wiring it in changes nothing until the flag is flipped.
"""

from __future__ import annotations

import logging
import math
import time
from itertools import combinations

log = logging.getLogger(__name__)


class PortfolioGuard:
    def __init__(self, config):
        self.enabled = bool(getattr(config, "portfolio_guard_enabled", False))
        self.max_gross_exposure_pct = float(
            getattr(config, "max_gross_exposure_pct", 1.5)
        )
        self.correlation_enabled = config.correlation_guard_enabled
        self.lookback = config.correlation_lookback
        self.min_samples = config.correlation_min_samples
        self.cluster_threshold = config.correlation_cluster_threshold
        self.asset_cap = config.max_asset_exposure_pct
        self.correlated_cap = config.max_correlated_exposure_pct
        for name, value in (("correlation_lookback", self.lookback),
                            ("correlation_min_samples", self.min_samples)):
            if type(value) is not int or value < 2:
                raise ValueError(f"{name} must be an integer >= 2")
        if self.min_samples > self.lookback:
            raise ValueError("correlation_min_samples exceeds lookback")
        for value in (self.asset_cap, self.correlated_cap, self.cluster_threshold):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("correlation limits must be finite and positive")
        if self.cluster_threshold > 1:
            raise ValueError("correlation_cluster_threshold must be <= 1")
        self._returns = {}
        self._prices = {}
        self.as_of = None
        self._engines: list = []

    def prepare(self, candles_by_product, interval_seconds):
        """Replace history every tick; match identical closed-bar intervals.

        Missing/gapped/constant series provide no diversification credit.
        Caller supplies closed candles only. Stale windows are discarded.
        """
        self.as_of = time.time()
        self._returns, self._prices = {}, {}
        for pid, candles in candles_by_product.items():
            points = {}
            for c in candles:
                try:
                    t, price = float(c["time"]), float(c["close"])
                    if math.isfinite(t) and math.isfinite(price) and price > 0:
                        points[t] = price
                except (KeyError, TypeError, ValueError):
                    continue
            rows = sorted(points.items())[-self.lookback - 1:]
            if not rows or not 0 <= self.as_of - rows[-1][0] <= 2 * interval_seconds:
                continue
            self._prices[pid] = rows[-1][1]
            self._returns[pid] = {
                (t0, t1): p1 / p0 - 1
                for (t0, p0), (t1, p1) in zip(rows, rows[1:])
                if t1 - t0 == interval_seconds
            }

    def _correlation(self, a, b):
        ra, rb = self._returns.get(a, {}), self._returns.get(b, {})
        keys = sorted(ra.keys() & rb.keys())[-self.lookback:]
        if len(keys) < self.min_samples:
            return 1.0, len(keys), True
        x, y = [ra[k] for k in keys], [rb[k] for k in keys]
        mx, my = sum(x) / len(x), sum(y) / len(y)
        xx, yy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
        if xx <= 1e-24 or yy <= 1e-24:
            return 1.0, len(keys), True
        rho = sum((u - mx) * (v - my) for u, v in zip(x, y)) / math.sqrt(xx * yy)
        if not math.isfinite(rho):
            return 1.0, len(keys), True
        return max(-1.0, min(1.0, rho)), len(keys), False

    def _risk(self, gross_by_asset, equity):
        assets = sorted(gross_by_asset)
        variance = sum(v * v for v in gross_by_asset.values())
        pairs, edges = [], {a: set() for a in assets}
        for a, b in combinations(assets, 2):
            rho, samples, assumed = self._correlation(a, b)
            # Gross notionals and absolute correlation: never grant hedge credit
            # for offsetting accounts or anti-correlated series.
            variance += 2 * abs(rho) * gross_by_asset[a] * gross_by_asset[b]
            pairs.append({"assets": [a, b], "correlation": rho,
                          "samples": samples, "assumed": assumed})
            if abs(rho) >= self.cluster_threshold:
                edges[a].add(b)
                edges[b].add(a)
        clusters, remaining = [], set(assets)
        while remaining:
            pending, members = [min(remaining)], set()
            while pending:
                a = pending.pop()
                if a in members:
                    continue
                members.add(a)
                pending.extend(edges[a] - members)
            remaining -= members
            gross = sum(gross_by_asset[a] for a in members)
            clusters.append({"assets": sorted(members), "gross": gross,
                             "exposure_pct": gross / equity if equity > 0 else None})
        effective = math.sqrt(max(0, variance))
        return {"effective_open_risk": effective,
                "effective_beta": effective / equity if equity > 0 else None,
                "clusters": clusters, "pairs": pairs}

    def register(self, engine) -> None:
        """Add an account engine (anything with .portfolio and .last_prices)."""
        self._engines.append(engine)

    def _merged_prices(self, extra_prices: dict | None) -> dict:
        """Freshest known price per product: every engine's last tick snapshot,
        overlaid with the calling engine's current-tick prices."""
        merged: dict = {}
        for e in self._engines:
            merged.update(getattr(e, "last_prices", None) or {})
        merged.update(self._prices)
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
        gross_by_asset = {}
        for e in self._engines:
            p = e.portfolio
            equity += p.cash
            for pid, pos in p.positions.items():
                if pos.quantity == 0:
                    continue
                value = pos.quantity * prices.get(pid, pos.avg_price)
                equity += value
                gross_by_asset[pid] = gross_by_asset.get(pid, 0.0) + abs(value)
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
            "gross_by_asset": gross_by_asset,
            "correlation_enabled": self.correlation_enabled,
            "as_of": self.as_of,
            "limits": {"asset_exposure_pct": self.asset_cap,
                       "correlated_exposure_pct": self.correlated_cap,
                       "gross_exposure_pct": self.max_gross_exposure_pct},
            **self._risk(gross_by_asset, equity),
        }

    def allows_entry(
        self, notional: float, extra_prices: dict | None = None, product_id: str | None = None
    ) -> tuple[bool, str]:
        """May a NEW position of ``notional`` USD be opened right now?

        Only consulted for entries; exits never come through here. When the
        guard is disabled this is always a yes, so the wiring itself changes
        no behavior.
        """
        if not self.enabled and not self.correlation_enabled:
            return True, ""
        snap = self.snapshot(extra_prices)
        if not math.isfinite(notional) or not math.isfinite(snap["gross"]):
            return False, "entry notional or portfolio exposure is unavailable"
        cap = self.max_gross_exposure_pct * snap["equity"]
        if not math.isfinite(snap["equity"]) or snap["equity"] <= 0:
            return False, "combined equity is non-positive or unavailable"
        if self.enabled and snap["gross"] + abs(notional) > cap:
            return False, (
                f"combined gross exposure ${snap['gross']:,.2f} + new "
                f"${abs(notional):,.2f} would exceed the cap ${cap:,.2f} "
                f"({self.max_gross_exposure_pct:.0%} of ${snap['equity']:,.2f} equity)"
            )
        if self.correlation_enabled:
            if not product_id or not math.isfinite(notional):
                return False, "correlation guard needs product and finite notional"
            gross = dict(snap["gross_by_asset"])
            gross[product_id] = gross.get(product_id, 0.0) + abs(notional)
            if gross[product_id] > self.asset_cap * snap["equity"]:
                return False, f"asset concentration cap exceeded for {product_id}"
            projected = self._risk(gross, snap["equity"])
            if projected["effective_beta"] > self.correlated_cap:
                return False, "correlation-adjusted exposure cap exceeded"
        return True, ""
