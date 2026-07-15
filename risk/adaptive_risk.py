"""
APEX Adaptive Risk — scales RISK_PER_TRADE per-trade based on three
independent signals instead of one flat number:

  1. Intraday drawdown tier   (defensive) — cut size progressively as the
     day's losses build, instead of trading full size right up until the
     DrawdownGuard hard-halts everything at MAX_DAILY_DRAWDOWN_PCT.
  2. Consecutive loss streak  (defensive) — cut size after a run of losses
     even if cumulative DD is still small (e.g. 4 losses at 1% risk each is
     only 4% DD — under the halt — but is still a real "something's off"
     signal worth de-risking for).
  3. Signal confidence        (offensive) — the aggregator's weighted vote
     score is currently computed and stored but never used; a unanimous
     high-conviction signal should size up a bit, a signal that just barely
     cleared AGG_THRESHOLD should size down a bit.

All three combine multiplicatively, then get clamped to a sane band. This
means the two defensive multipliers can stack to meaningfully shrink size
during a bad patch, but a single very-confident signal alone can never scale
size up by more than CONF_MAX_MULT, and never overrides an active
drawdown/streak throttle back up to full size.
"""

# ── Drawdown tiers ───────────────────────────────────────────────────────────
# (dd_upper_bound, risk_multiplier) — first tier whose upper bound the current
# DD is strictly below wins. MAX_DAILY_DRAWDOWN_PCT itself is enforced
# separately by DrawdownGuard as a hard halt — these tiers just make the
# approach to that wall progressively softer instead of cliff-edged.
DD_TIERS = [
    (0.03, 1.00),   # 0.0% – 3.0% DD: full size
    (0.04, 0.50),   # 3.0% – 4.0% DD: half size
    (0.05, 0.25),   # 4.0% – 5.0% DD: quarter size
]
DD_FLOOR_MULT = 0.25  # shouldn't be reached in practice — DrawdownGuard halts at the same 5% boundary


def drawdown_multiplier(current_dd: float) -> float:
    for upper_bound, mult in DD_TIERS:
        if current_dd < upper_bound:
            return mult
    return DD_FLOOR_MULT


# ── Losing-streak tiers ──────────────────────────────────────────────────────
# (max_consecutive_losses, risk_multiplier) — first tier the streak fits
# within wins. Resets to full size the moment a winning trade breaks the streak.
STREAK_TIERS = [
    (2, 1.00),   # 0–2 consecutive losses: full size
    (4, 0.60),   # 3–4 consecutive losses: 60% size
    (6, 0.30),   # 5–6 consecutive losses: 30% size
]
STREAK_FLOOR_MULT = 0.15  # 7+ consecutive losses


def streak_multiplier(consecutive_losses: int) -> float:
    for max_losses, mult in STREAK_TIERS:
        if consecutive_losses <= max_losses:
            return mult
    return STREAK_FLOOR_MULT


# ── Confidence scaling ───────────────────────────────────────────────────────
# Aggregator confidence is a weighted-vote score bounded roughly
# [AGG_THRESHOLD, 1.0] (anything below AGG_THRESHOLD never reaches a trade at
# all, so that floor maps to the minimum multiplier; 1.0 = every weighted
# strategy agreed, mapping to the maximum multiplier).
CONF_MIN_MULT = 0.70   # size at a signal that only just cleared entry threshold
CONF_MAX_MULT = 1.30   # size at maximum possible conviction (full agreement)


def confidence_multiplier(confidence: float, agg_threshold: float) -> float:
    if confidence <= agg_threshold:
        return CONF_MIN_MULT
    span = max(1e-6, 1.0 - agg_threshold)
    frac = min(1.0, (confidence - agg_threshold) / span)
    return CONF_MIN_MULT + frac * (CONF_MAX_MULT - CONF_MIN_MULT)


# ── Combined ──────────────────────────────────────────────────────────────
OVERALL_MIN_MULT = 0.10
OVERALL_MAX_MULT = 1.30


def combined_risk_multiplier(current_dd: float, consecutive_losses: int,
                              confidence: float, agg_threshold: float) -> float:
    """Returns the multiplier to apply to RISK_PER_TRADE for a single trade."""
    m = (
        drawdown_multiplier(current_dd)
        * streak_multiplier(consecutive_losses)
        * confidence_multiplier(confidence, agg_threshold)
    )
    return max(OVERALL_MIN_MULT, min(OVERALL_MAX_MULT, m))
