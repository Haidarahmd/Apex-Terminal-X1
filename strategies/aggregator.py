"""
APEX Signal Aggregator — weighted majority vote with conflict filtering.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIX (v2.1) — the "zero trades ever" bug
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
5-strategy normalised weights: macd_ema=0.280, rsi_reversal=0.220,
breakout=0.220, scalp=0.160, fib_retracement=0.120.
AGG_THRESHOLD=0.38 is above EVERY single weight — meaning no single
strategy can ever trigger a trade alone; 2+ diverse strategies must
fire on the exact same bar, which real market data shows is rare
(confirmed: "no_strategy_agreement" on every cycle across days of
live logs, zero trades entered).

Fix: SINGLE-STRATEGY CONFLUENCE secondary path.
If exactly ONE strategy fires with normalised weight >=
SINGLE_STRATEGY_MIN_WEIGHT (default 0.25 → only macd_ema at 0.280
qualifies), there is no opposing vote at all (clean, uncontested
signal), AND the scanner's own independent 16-indicator grade agrees
directionally with SINGLE_STRATEGY_MIN_GRADE quality (A or S+) —
accept the trade, flag it single_strategy=True, and the engine sizes
it down (see SINGLE_STRATEGY_SIZE_MULT in config/settings.py).

The original multi-strategy consensus path is UNCHANGED and remains
the primary, full-size path.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
from config.settings import (
    STRATEGY_WEIGHTS, AGG_THRESHOLD, AGG_MARGIN, CONFLICT_BLOCK,
    SINGLE_STRATEGY_MIN_WEIGHT, SINGLE_STRATEGY_MIN_GRADE, SINGLE_STRATEGY_ENABLED,
)

logger = logging.getLogger(__name__)

_GRADE_RANK = {"S+": 3, "A": 2, "B": 1, "": 0}


class SignalAggregator:
    def __init__(self, strategies: list):
        self.strategies = strategies

    def aggregate(
        self, df, htf_df=None, scalp_df=None, weights: dict | None = None,
        scanner_grade: str | None = None, scanner_side: str | None = None,
    ) -> dict | None:
        raw_w = weights or STRATEGY_WEIGHTS
        total_w = sum(raw_w.values()) or 1.0
        w = {k: v / total_w for k, v in raw_w.items()}

        votes    = {"buy": 0.0, "sell": 0.0}
        sources  = {"buy": [],  "sell": []}
        best_sig = {"buy": None, "sell": None}

        for strat in self.strategies:
            weight = w.get(strat.name, 0.0)
            if weight <= 0:
                continue

            # Scalp strategy uses the scalp (15m) df
            input_df = scalp_df if (strat.name == "scalp" and scalp_df is not None) else df

            try:
                sig = strat.generate_signal(input_df, htf_df)
            except Exception as exc:
                logger.warning("[AGG] %s error: %s", strat.name, exc)
                continue

            if sig is None:
                continue

            side = sig["side"]
            votes[side] += weight
            sources[side].append(strat.name)
            if best_sig[side] is None:
                best_sig[side] = sig

        buy_s, sell_s = votes["buy"], votes["sell"]

        logger.debug("[AGG] BUY=%.3f (%s) | SELL=%.3f (%s)",
                     buy_s, ",".join(sources["buy"]) or "none",
                     sell_s, ",".join(sources["sell"]) or "none")

        # ── Conflict block — both sides simultaneously above threshold ────────
        if CONFLICT_BLOCK and buy_s >= AGG_THRESHOLD and sell_s >= AGG_THRESHOLD:
            logger.info("[AGG] ⚠ Conflict — both sides above %.2f — no trade", AGG_THRESHOLD)
            return None

        # ── PRIMARY: full multi-strategy consensus ────────────────────────────
        if buy_s >= AGG_THRESHOLD or sell_s >= AGG_THRESHOLD:
            best_side  = "buy" if buy_s >= sell_s else "sell"
            other_side = "sell" if best_side == "buy" else "buy"
            margin = votes[best_side] - votes[other_side]

            if margin < AGG_MARGIN:
                logger.info("[AGG] Margin %.3f < %.2f — signal too weak", margin, AGG_MARGIN)
                return None

            sig_obj = best_sig[best_side]
            if sig_obj is None:
                return None

            logger.info("[AGG] ✓ CONSENSUS %s | score=%.3f | margin=%.3f | strategies=%s",
                        best_side.upper(), votes[best_side], margin, sources[best_side])

            return {
                "side":            best_side,
                "price":           sig_obj["price"],
                "atr":             sig_obj["atr"],
                "strategies":      sources[best_side],
                "confidence":      round(votes[best_side], 4),
                "single_strategy": False,
            }

        # ── SECONDARY: single high-conviction strategy + scanner confluence ───
        # Only fires when the primary path returns nothing. Requires:
        #   1. Exactly ONE strategy fired (not two weak ones summing up)
        #   2. Its normalised weight >= SINGLE_STRATEGY_MIN_WEIGHT (0.25)
        #      → only macd_ema (0.280) qualifies with current 5-strat weights
        #   3. ZERO opposing votes — clean, uncontested signal
        #   4. Scanner's independent grade >= SINGLE_STRATEGY_MIN_GRADE (A)
        #      AND scanner direction agrees — this is the confluence gate
        if not SINGLE_STRATEGY_ENABLED:
            return None

        for side in ("buy", "sell"):
            other = "sell" if side == "buy" else "buy"
            if votes[other] > 0:
                continue   # any opposing vote disqualifies — must be clean
            if votes[side] < SINGLE_STRATEGY_MIN_WEIGHT:
                continue
            if len(sources[side]) != 1:
                continue   # exactly one strategy, not two weak ones

            # Scanner confluence gate
            if scanner_side is None or scanner_side.lower() != side:
                continue
            if _GRADE_RANK.get(scanner_grade, 0) < _GRADE_RANK.get(SINGLE_STRATEGY_MIN_GRADE, 2):
                continue

            sig_obj = best_sig[side]
            if sig_obj is None:
                continue

            logger.info("[AGG] ✓ SINGLE-STRATEGY %s | %s weight=%.3f | scanner=%s",
                        side.upper(), sources[side][0], votes[side], scanner_grade)

            return {
                "side":            side,
                "price":           sig_obj["price"],
                "atr":             sig_obj["atr"],
                "strategies":      sources[side],
                "confidence":      round(votes[side], 4),
                "single_strategy": True,
            }

        return None
