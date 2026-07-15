"""Persists daily equity baseline so drawdown is correct across restarts."""
import json
import logging
from datetime import date
from pathlib import Path

from config.settings import EQUITY_FILE

logger = logging.getLogger(__name__)


class EquityTracker:
    def __init__(self, current_equity: float):
        self._file    = Path(EQUITY_FILE)
        self._today   = str(date.today())
        self._baseline= self._load(current_equity)
        logger.info("[EQUITY] Baseline: %.2f (date: %s)", self._baseline, self._today)

    def _load(self, fallback: float) -> float:
        if self._file.exists():
            try:
                with open(self._file) as f:
                    data = json.load(f)
                if data.get("date") == self._today:
                    return float(data["baseline"])
            except Exception:
                pass
        # New day or missing file — set fresh baseline
        self._save(fallback)
        return fallback

    def _save(self, baseline: float):
        try:
            with open(self._file, "w") as f:
                json.dump({"date": self._today, "baseline": baseline}, f)
        except Exception as e:
            logger.warning("[EQUITY] Save failed: %s", e)

    @property
    def baseline(self) -> float:
        return self._baseline
