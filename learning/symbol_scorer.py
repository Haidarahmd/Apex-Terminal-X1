"""
APEX Symbol Scorer — per-symbol performance tracking.
Pauses symbols with < 40% win rate.
Scales position size by expectancy.
Persists to disk across restarts.
"""
import json
import logging
from pathlib import Path

from config.settings import (
    PERF_SCORE_LOOKBACK, PERF_SCORE_MIN_WIN_RATE,
    PERF_SCORE_SIZE_SCALE, SCORE_FILE,
)

logger = logging.getLogger(__name__)


class SymbolScorer:
    def __init__(self):
        self._scores: dict[str, list[float]] = {}
        self._load()

    def update(self, symbol: str, pnl: float):
        self._scores.setdefault(symbol, []).append(pnl)
        self._scores[symbol] = self._scores[symbol][-PERF_SCORE_LOOKBACK:]
        self._save()

    def is_tradeable(self, symbol: str) -> bool:
        trades = self._scores.get(symbol, [])
        if len(trades) < 5:
            return True
        win_rate = sum(1 for t in trades if t > 0) / len(trades)
        if win_rate < PERF_SCORE_MIN_WIN_RATE:
            logger.info("[SCORER] %s paused — win rate %.0f%% < %.0f%%",
                        symbol, win_rate * 100, PERF_SCORE_MIN_WIN_RATE * 100)
            return False
        return True

    def size_multiplier(self, symbol: str) -> float:
        if not PERF_SCORE_SIZE_SCALE:
            return 1.0
        trades = self._scores.get(symbol, [])
        if len(trades) < 5:
            return 1.0
        wins  = [t for t in trades if t > 0]
        losses= [t for t in trades if t < 0]
        wr    = len(wins) / len(trades)
        aw    = sum(wins)  / max(1, len(wins))
        al    = abs(sum(losses) / max(1, len(losses)))
        exp   = wr * aw - (1 - wr) * al
        mult  = 1.0 + min(0.5, max(-0.5, exp / (aw + 1e-9) * 0.5))
        return round(mult, 2)

    def summary(self) -> dict:
        out = {}
        for sym, trades in self._scores.items():
            if not trades:
                continue
            wins = [t for t in trades if t > 0]
            losses = [t for t in trades if t < 0]
            out[sym] = {
                "trades":    len(trades),
                "win_rate":  round(len(wins) / len(trades) * 100, 1),
                "total_pnl": round(sum(trades), 4),
                "avg_win":   round(sum(wins) / max(1, len(wins)), 4),
                "avg_loss":  round(sum(losses) / max(1, len(losses)), 4),
                "tradeable": self.is_tradeable(sym),
            }
        return out

    def _save(self):
        try:
            with open(SCORE_FILE, "w") as f:
                json.dump(self._scores, f)
        except Exception as e:
            logger.warning("[SCORER] Save failed: %s", e)

    def _load(self):
        if not Path(SCORE_FILE).exists():
            return
        try:
            with open(SCORE_FILE) as f:
                self._scores = json.load(f)
            logger.info("[SCORER] Loaded scores for %d symbols", len(self._scores))
        except Exception as e:
            logger.warning("[SCORER] Load failed: %s", e)
