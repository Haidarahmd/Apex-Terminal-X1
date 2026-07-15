"""
APEX Take-Profit Ladder Manager (TP1 / TP2 / TP3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces the old single-TP + one-shot 50% partial-close design with a
proper 3-level ladder:

  TP1 (1.0x ATR)  -> close 40% of remaining qty, SL -> breakeven
  TP2 (2.0x ATR)  -> close 35% of what's left,    SL -> TP1 price
  TP3 (3.0x ATR)  -> close everything left (full exit)

Each level closes a PERCENTAGE OF WHAT'S CURRENTLY OPEN, not of the
original size — so the percentages don't need to sum to exactly 100%,
and TP3 always sweeps up any remainder regardless of rounding.

A position can only trigger each level once (tracked by position id),
and levels must fire in order — if price gaps straight through TP1 to
TP2 in one tick (common with low-liquidity altcoins or a stale price
feed during network issues), this manager fires whichever levels were
skipped in sequence rather than just the highest one, so the partial
closes and SL ratchets still happen correctly.
"""
import logging

from config.settings import (
    TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT,
    TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT, STOP_LOSS_ATR,
)

logger = logging.getLogger(__name__)

_LEVELS = [
    ("tp1", TP1_ATR_MULT, TP1_CLOSE_PCT),
    ("tp2", TP2_ATR_MULT, TP2_CLOSE_PCT),
    ("tp3", TP3_ATR_MULT, TP3_CLOSE_PCT),
]


def blended_rr() -> float:
    """The REAL expected reward-to-risk of the ladder, accounting for partial
    closes at TP1/TP2 rather than assuming the full position rides to TP3.

    TPx_CLOSE_PCT is "% of whatever qty remains open at that point" (not %
    of the original position), so the fraction of the ORIGINAL position
    closed at each level has to cascade through the remaining quantity —
    same math the live ladder actually executes.

    Independent of ATR/entry price/symbol: since every leg scales by the
    same atr_val, it cancels out of the ratio, so this is a single constant
    determined purely by the TPx_ATR_MULT / TPx_CLOSE_PCT / STOP_LOSS_ATR
    config values. That's intentional — it's what makes it usable as a
    static entry-quality gate (see MIN_RR_RATIO in config/settings.py).
    """
    r1 = TP1_ATR_MULT / STOP_LOSS_ATR
    r2 = TP2_ATR_MULT / STOP_LOSS_ATR
    r3 = TP3_ATR_MULT / STOP_LOSS_ATR

    frac_tp1 = TP1_CLOSE_PCT
    remaining = 1 - frac_tp1
    frac_tp2 = TP2_CLOSE_PCT * remaining
    remaining -= frac_tp2
    frac_tp3 = TP3_CLOSE_PCT * remaining

    return frac_tp1 * r1 + frac_tp2 * r2 + frac_tp3 * r3


class TPLadderManager:
    def __init__(self):
        # pos_id -> set of level names already triggered, e.g. {"tp1"}
        self._triggered: dict[str, set] = {}

    def levels_for(self, entry: float, side: str, atr_val: float) -> dict[str, float]:
        """Compute the 3 absolute price levels for a fresh position."""
        sign = 1 if side == "buy" else -1
        return {
            name: entry + sign * atr_val * mult
            for name, mult, _ in _LEVELS
        }

    def check(self, position: dict, atr_val: float) -> list[dict]:
        """
        Check a position's current price against its TP ladder.
        Returns a list of newly-triggered level events (usually 0 or 1,
        but can be more than one if price gapped through multiple levels
        in a single update — each is returned in order so the caller can
        apply partial closes sequentially).
        Each event: {"level": "tp1", "price": <level price>, "close_pct": 0.40}
        """
        if atr_val <= 0:
            return []
        pos_id = position["id"]
        side   = position["side"]
        entry  = position["entry_price"]
        cur    = position.get("current_price", entry)
        done   = self._triggered.setdefault(pos_id, set())

        events = []
        for name, mult, close_pct in _LEVELS:
            if name in done:
                continue
            target = entry + (1 if side == "buy" else -1) * atr_val * mult
            hit = (cur >= target) if side == "buy" else (cur <= target)
            if hit:
                done.add(name)
                events.append({"level": name, "price": target, "close_pct": close_pct})
                logger.info("[TP_LADDER] %s %s — %s hit @ %.6f (target %.6f)",
                            position.get("symbol"), pos_id, name.upper(), cur, target)
        return events

    def sl_after(self, level: str, position: dict) -> float | None:
        """
        What the SL should move to after a given level triggers.
        TP1 -> breakeven (entry + a tick, in the trade's favour)
        TP2 -> TP1's price (lock in the TP1 gain on the remainder)
        TP3 -> position is fully closed, no SL needed (return None)
        """
        side  = position["side"]
        entry = position["entry_price"]
        tick  = position.get("tick_size", 0.00001)
        atr   = position.get("atr", 0)

        if level == "tp1":
            return (entry + tick) if side == "buy" else (entry - tick)
        if level == "tp2":
            tp1_price = entry + (1 if side == "buy" else -1) * atr * TP1_ATR_MULT
            return tp1_price
        return None  # tp3 — fully closed

    def close_qty_for(self, position: dict, close_pct: float) -> float:
        """close_pct applies to whatever qty is CURRENTLY open."""
        return round(position.get("qty", 0) * close_pct, 8)

    def reset(self, pos_id: str):
        """Clear tracking when a position fully closes (so the id can be reused safely)."""
        self._triggered.pop(pos_id, None)

    def progress(self, pos_id: str) -> list[str]:
        """Which levels have already fired for this position — used by the dashboard."""
        return sorted(self._triggered.get(pos_id, set()))
