"""
APEX Trade Journal — CSV-backed trade log with analytics.
Records every trade open/close and computes performance statistics.
"""
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from config.settings import JOURNAL_FILE

logger = logging.getLogger(__name__)

_COLUMNS = [
    "id", "symbol", "side", "strategy", "entry_price", "exit_price",
    "qty", "sl", "tp", "pnl", "pnl_pct", "reason", "opened_at", "closed_at",
    "atr", "confidence", "regime",
]


class TradeJournal:
    def __init__(self, path: Path = JOURNAL_FILE):
        self.path = path
        self._ensure_header()
        logger.info("[JOURNAL] Trade log: %s", path)

    def _ensure_header(self):
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(_COLUMNS)
            return
        # File already exists — check its header still matches _COLUMNS.
        # (Guards against a pre-upgrade journal, e.g. one written before the
        # "regime" column existed, silently going out of alignment: new rows
        # would have one more field than the old header expects.)
        try:
            with open(self.path, newline="") as f:
                existing_header = next(csv.reader(f), [])
        except Exception:
            existing_header = []
        if existing_header and existing_header != _COLUMNS:
            logger.warning(
                "[JOURNAL] %s has an outdated header %s (expected %s) — "
                "new columns will be missing on old rows. Rename/delete the "
                "file to start a fresh journal with the new schema, or "
                "migrate it manually.", self.path, existing_header, _COLUMNS)

    def log_trade(self, trade: dict):
        row = [trade.get(c, "") for c in _COLUMNS]
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow(row)
        logger.info("[JOURNAL] Logged trade %s %s pnl=%.4f",
                    trade.get("symbol"), trade.get("id"), trade.get("pnl", 0))

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def load_recent(self, n: int = 50) -> list[dict]:
        all_trades = self.load_all()
        return all_trades[-n:]

    def stats(self, lookback: int | None = None) -> dict:
        trades = self.load_all()
        if lookback:
            trades = trades[-lookback:]
        if not trades:
            return {"trades": 0, "win_rate": 0.0, "avg_pnl": 0.0,
                    "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                    "best": 0.0, "worst": 0.0, "expectancy": 0.0}

        pnls  = [float(t.get("pnl", 0)) for t in trades]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p < 0]

        win_rate  = len(wins) / len(pnls) if pnls else 0
        avg_win   = sum(wins)  / len(wins)   if wins   else 0
        avg_loss  = sum(losses)/ len(losses) if losses else 0
        expectancy= win_rate * avg_win + (1 - win_rate) * avg_loss

        return {
            "trades":     len(pnls),
            "win_rate":   round(win_rate * 100, 1),
            "total_pnl":  round(sum(pnls), 4),
            "avg_pnl":    round(sum(pnls) / len(pnls), 4),
            "avg_win":    round(avg_win, 4),
            "avg_loss":   round(avg_loss, 4),
            "best":       round(max(pnls), 4),
            "worst":      round(min(pnls), 4),
            "expectancy": round(expectancy, 4),
        }

    def current_streak(self, lookback: int = 20) -> int:
        """Number of consecutive losing trades ending at the most recent
        trade (0 if the last trade was a win/breakeven, or there's no
        history yet). Used by risk/adaptive_risk.py to throttle size after
        a run of losses, independent of cumulative drawdown %."""
        trades = self.load_recent(lookback)
        streak = 0
        for t in reversed(trades):
            try:
                pnl = float(t.get("pnl", 0))
            except (TypeError, ValueError):
                break
            if pnl < 0:
                streak += 1
            else:
                break
        return streak

    def strategy_stats(self) -> dict[str, dict]:
        trades = self.load_all()
        by_strat: dict[str, list[float]] = {}
        for t in trades:
            strat = t.get("strategy", "unknown")
            pnl   = float(t.get("pnl", 0))
            by_strat.setdefault(strat, []).append(pnl)

        out = {}
        for strat, pnls in by_strat.items():
            wins  = [p for p in pnls if p > 0]
            losses= [p for p in pnls if p < 0]
            out[strat] = {
                "trades":   len(pnls),
                "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                "total_pnl":round(sum(pnls), 4),
                "avg_win":  round(sum(wins) / max(1, len(wins)), 4),
                "avg_loss": round(sum(losses) / max(1, len(losses)), 4),
            }
        return out
