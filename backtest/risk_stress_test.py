"""
APEX Risk Stress Test — Monte Carlo comparison of flat vs adaptive position
sizing, isolated from entry-signal alpha.

WHY THIS EXISTS: backtest/backtester.py replays real exchange candle data
through a strategy, which requires live network access to OKX/Binance/Bybit.
Position sizing / risk-throttle logic (risk/adaptive_risk.py) doesn't touch
entry signals at all — it only rescales how big each trade is once a trade
has already been decided. That means it can be validated correctly on a
SIMULATED STREAM OF TRADE OUTCOMES instead of real price data: same approach
professional risk desks use to stress-test money-management overlays
independently of the strategy that generates the trades.

This is NOT a substitute for backtester.py's real market replay — it can't
tell you whether the strategies themselves are profitable. It only answers:
"given a stream of wins/losses with realistic clustering, does the adaptive
risk throttle reduce drawdown and tail risk compared to flat sizing, and at
what cost to typical-case returns?"

Trade outcome model:
  - Outcomes are win/loss with autocorrelation (losses cluster, matching
    real market regime behaviour) rather than i.i.d. coin flips, since a
    losing-streak throttle is meaningless to test against independent trades.
  - Win size = blended R-multiple of the actual TP1/TP2/TP3 ladder
    (TP1_CLOSE_PCT/TP2_CLOSE_PCT/TP3_CLOSE_PCT at TP1/TP2/TP3_ATR_MULT,
    against STOP_LOSS_ATR) — same ladder math as risk/tp_ladder.py.
  - Loss size = -1R (full stop).
  - Signal confidence is sampled per trade and mildly, deliberately
    correlated with win probability (a well-built aggregator's vote score
    SHOULD carry some real information about win probability — if it
    didn't, sizing on it would be pointless). The correlation strength here
    is a conservative assumption, flagged clearly below.
"""
import random
import statistics

from config.settings import (
    RISK_PER_TRADE, MAX_DAILY_DRAWDOWN_PCT, STOP_LOSS_ATR,
    TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT,
    TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT, AGG_THRESHOLD,
)
from risk.adaptive_risk import combined_risk_multiplier

# ── Blended win R-multiple from the actual TP ladder ────────────────────────
# TPx_CLOSE_PCT is "% of whatever qty remains open at that point" (see
# config/settings.py comment + risk/tp_ladder.py), NOT % of the original
# position — so the fraction of the ORIGINAL position closed at each level
# has to cascade through the remaining quantity, same as the live system.
def _blended_win_r() -> float:
    r1 = TP1_ATR_MULT / STOP_LOSS_ATR
    r2 = TP2_ATR_MULT / STOP_LOSS_ATR
    r3 = TP3_ATR_MULT / STOP_LOSS_ATR

    frac_tp1 = TP1_CLOSE_PCT
    remaining = 1 - frac_tp1
    frac_tp2 = TP2_CLOSE_PCT * remaining
    remaining -= frac_tp2
    frac_tp3 = TP3_CLOSE_PCT * remaining  # closes what's left (should sum to 1.0 total)

    return frac_tp1 * r1 + frac_tp2 * r2 + frac_tp3 * r3

WIN_R  = _blended_win_r()     # ≈1.23R at current TP1/TP2/TP3 settings
LOSS_R = -1.0

BASE_WIN_RATE   = 0.42   # conservative assumption, not fit to any real data
STREAK_LOSS_BUMP= 0.12   # P(loss | previous loss) = BASE + this
STREAK_WIN_BUMP = 0.06   # P(loss | previous win)  = BASE - this

N_SIMS   = 3000
N_TRADES = 300
DD_HALT  = MAX_DAILY_DRAWDOWN_PCT  # both variants halt new trades at the same wall


def _sample_confidence_and_outcome(prev_loss: bool, rng: random.Random, base_win_rate: float):
    p_loss = (1 - base_win_rate) + (STREAK_LOSS_BUMP if prev_loss else -STREAK_WIN_BUMP)
    p_loss = min(0.85, max(0.15, p_loss))
    is_loss = rng.random() < p_loss

    # Confidence in [AGG_THRESHOLD, 1.0]; mildly higher on average for wins
    # (assumption: vote-agreement carries some real signal — kept modest).
    base = rng.uniform(AGG_THRESHOLD, 1.0)
    if not is_loss:
        base = min(1.0, base + 0.06)
    else:
        base = max(AGG_THRESHOLD, base - 0.03)
    return base, is_loss


def _running_max_dd(rng_seed, adaptive, base_win_rate):
    rng = random.Random(rng_seed)
    equity, peak, streak, prev_loss = 1.0, 1.0, 0, False
    worst_dd = 0.0
    for _ in range(N_TRADES):
        current_dd = max(0.0, (peak - equity) / peak)
        worst_dd = max(worst_dd, current_dd)
        conf, is_loss = _sample_confidence_and_outcome(prev_loss, rng, base_win_rate)
        if current_dd >= DD_HALT:
            prev_loss = is_loss
            streak = streak + 1 if is_loss else 0
            continue
        risk_pct = RISK_PER_TRADE
        if adaptive:
            mult = combined_risk_multiplier(current_dd, streak, conf, AGG_THRESHOLD)
            risk_pct = RISK_PER_TRADE * mult
        r_mult = LOSS_R if is_loss else WIN_R
        equity *= (1 + risk_pct * r_mult)
        equity = max(equity, 1e-6)
        peak = max(peak, equity)
        streak = streak + 1 if is_loss else 0
        prev_loss = is_loss
    return equity, worst_dd


def run_comparison(base_win_rate: float):
    results = {"flat": {"final": [], "dd": []}, "adaptive": {"final": [], "dd": []}}
    for i in range(N_SIMS):
        seed = 10_000 + i
        for variant in ("flat", "adaptive"):
            final_eq, worst_dd = _running_max_dd(seed, adaptive=(variant == "adaptive"), base_win_rate=base_win_rate)
            results[variant]["final"].append(final_eq)
            results[variant]["dd"].append(worst_dd)

    def pct(vals, p):
        vals = sorted(vals)
        idx = int(len(vals) * p)
        return vals[min(idx, len(vals) - 1)]

    report = {}
    for variant in ("flat", "adaptive"):
        final = results[variant]["final"]
        dd    = results[variant]["dd"]
        report[variant] = {
            "median_return_pct":  round((statistics.median(final) - 1) * 100, 2),
            "p5_return_pct":      round((pct(final, 0.05) - 1) * 100, 2),
            "p95_return_pct":     round((pct(final, 0.95) - 1) * 100, 2),
            "median_max_dd_pct":  round(statistics.median(dd) * 100, 2),
            "p95_max_dd_pct":     round(pct(dd, 0.95) * 100, 2),
            "prob_dd_over_10pct": round(sum(1 for d in dd if d > 0.10) / len(dd) * 100, 1),
            "prob_ruin_30pct":    round(sum(1 for d in dd if d > 0.30) / len(dd) * 100, 1),
        }
    return report


if __name__ == "__main__":
    import json
    print(f"WIN_R (blended TP ladder) = {WIN_R:.3f}   LOSS_R = {LOSS_R}")
    print(f"N_SIMS={N_SIMS}  N_TRADES={N_TRADES}  DD_HALT={DD_HALT*100:.0f}%\n")
    for wr in (0.38, 0.42, 0.46, 0.50):
        edge_r = wr * WIN_R + (1 - wr) * LOSS_R
        print(f"── base_win_rate={wr:.0%}  (expectancy ≈ {edge_r:+.3f}R/trade) ──")
        print(json.dumps(run_comparison(wr), indent=2))
        print()
