# Enabling the opt-in features (paper mode checklist)

Three features shipped from the 2026-07 audit-and-enhance sessions
(PR #29). **All three are OFF by default** — the five live paper accounts
behave exactly as before until you flip a flag in `config.ci.yaml` (cloud)
or `config.yaml` (local). This file is the enable-later checklist: what each
flag does, what has been verified, what has NOT, and a suggested order.

Everything here is paper trading only; none of these features can place a
real order.

---

## 1. `reentry_cooldown_bars` — post-stop-out re-entry cooldown

**What it does.** After a protective stop closes a position, new entries
(long or short) in that product are blocked until N bars have passed.
Take-profit and ordinary strategy exits start no cooldown. Rejections show
up in the activity log / overseer status as `reentry_cooldown`.

**Why it exists.** Observed live on `long_short` (2026-07-02): the ETH short
stopped out at $1,698.69 at 15:03 UTC and the level-triggered re-entry
re-shorted at $1,702.08 — a worse price — 2h21m later.

**Verified.**
- Test replays the real fills with their exact relative timing: a 1-bar
  (daily) cooldown blocks the Jul 2 re-short; the default `0` reproduces the
  live re-entry exactly (flag is a true no-op until set).
- The account's other live re-entry (BTC, 31.8h after its own stop) survives
  a 1-bar cooldown — the setting doesn't over-block.
- Long-side and normal-exit (no cooldown) cases covered; restart-safe by
  construction (derived from the persisted trade log, no in-memory state).
- The backtester applies the same gate, so the flag's historical effect is
  measurable once exchange data is reachable.

**Not verified.** The *optimal* N has not been backtested (blocked on the
network allowlist, below). 1 bar is the minimal, evidence-backed choice.

**Suggested enablement (first — lowest risk, fixes an observed bug).**
- [ ] Set `reentry_cooldown_bars: 1` in `config.ci.yaml` (top level; applies
      to all accounts, per-account override available).
- [ ] After a week+: check `rejection_reasons.reentry_cooldown` in
      `overseer-status.json` — expect a small count, mostly on `long_short`.
- [ ] Sanity: no strategy should show a *large* count (that would mean it
      stops out constantly, which is a strategy problem, not a cooldown one).

---

## 2. `portfolio_guard_enabled` — cross-account exposure guard

**What it does.** The combined gross long / gross short / net exposure of
ALL accounts is already computed and logged every tick (read-only phase —
active now, no flag needed; also visible on the dashboard's Total tab).
Enabling the flag adds the veto: a NEW entry that would push combined gross
exposure above `max_gross_exposure_pct` × combined equity is rejected
(`portfolio_exposure` in the activity log). **Exits, covers, and protective
stops are never consulted** — an over-cap book can always reduce risk.

**Verified.**
- Snapshot reproduces the real book to the cent (2026-07-04: gross long
  $881.21, gross short $2,577.69, net −$1,696.48 across the five DBs,
  matching the dashboard export's independent computation).
- Engine-level test: a veto leaves every existing position untouched, and a
  protective-stop exit executes normally while the book is over the cap.
- Disabled guard approves everything (wiring alone changes nothing).

**Not verified.** The cap *value* is untuned. The default (150% of combined
equity) is far above anything observed (combined gross has been ≤ ~7% of
equity), so at 1.5 it is effectively a disaster brake, not an active
constraint.

**Suggested enablement (second).**
- [ ] Watch the read-only exposure line in the run logs / dashboard for a
      couple of weeks to learn the book's normal range.
- [ ] Set `portfolio_guard_enabled: true` with the default
      `max_gross_exposure_pct: 1.5` (disaster brake only).
- [ ] Only tighten the cap once there's evidence of correlated pile-ups
      (all sleeves long the same coins at once) that you want to prevent.

---

## 3. `momentum_rotation` — cross-sectional rotation account

**What it does.** A sixth strategy (registered, but NO account runs it):
ranks the account's products by trailing 90-bar return and holds ONLY the
leader, and only while the leader is above its own 200-bar trend MA **and**
its trailing return is positive — otherwise cash. The positive-momentum gate
came from a real-data check: on 2026-07-04 SOL "led" at −0.1% vs BTC −8.5% /
ETH −15.4%; without the gate the strategy would have bought the least-bad
loser.

**Verified.**
- Hand-constructed universes pin the ranking (BUY leader / SELL the rest),
  the leader-below-trend-MA all-cash case, the leader-with-negative-or-zero-
  momentum all-cash case, deterministic tie-breaking, and the no-`prepare()`
  HOLD fallback.
- Engine-level ticks prove the universe hook wiring: one tick buys only the
  leader; a leadership flip rotates the position (SELL old leader, BUY new).
- Per-product strategies are unaffected by the new `prepare` hook (full
  suite green).

**NOT verified — read before enabling.**
- ⚠ **No multi-year backtest.** The backtester is single-instrument by
  design and cannot replay a cross-sectional strategy;
  `scripts/backtest.py` skips it with an explicit message. Historical
  validation needs (a) the network allowlist fix below and (b) a
  multi-product backtest mode. Until then this strategy's edge is a
  hypothesis, unit-tested but unmeasured.
- No live signal history at all (it has never run).

**Suggested enablement (third/last, and ideally not before backtesting).**
- [ ] Preferably: unblock `api.coinbase.com` (below), build the
      multi-product backtest, and validate the edge first.
- [ ] To run it live-paper anyway: uncomment the `rotation` account block in
      `config.ci.yaml` (wide disaster stops + cap sizing, like `regime`).
- [ ] Expect low churn: entries only when the leader is above trend AND
      positive over 90 bars — in the current bear tape it would sit in cash.
- [ ] Judge it against `regime` on the dashboard comparison table: rotation
      must beat regime (its closest cousin) risk-adjusted to earn its slot.

---

## Standalone blocker: network allowlist

`api.coinbase.com` is blocked by the remote environment's egress policy
("Host not in allowlist"). That is the single blocker for multi-year
backtest validation of `regime`, `trend_long_short`, and
`momentum_rotation`, and for tuning `reentry_cooldown_bars` historically.
Add the host to the environment's network egress settings, then run:

```bash
python -m scripts.backtest --granularity ONE_DAY --count 1500
```
