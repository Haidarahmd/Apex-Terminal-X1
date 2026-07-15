"""
APEX Volatility Gate — skip flat/compressed markets.
Compares current ATR to recent average ATR.
If current < min_ratio × average, market is considered dead.
"""
import logging
import pandas as pd
from indicators.core import atr

logger = logging.getLogger(__name__)


class VolatilityGate:
    def __init__(self, lookback: int = 20, min_ratio: float = 0.8):
        self.lookback  = lookback
        self.min_ratio = min_ratio

    def is_open(self, df: pd.DataFrame, period: int = 14) -> bool:
        if df is None or len(df) < self.lookback + period:
            return True  # not enough data — don't block
        atr_series  = atr(df, period).dropna()
        if len(atr_series) < self.lookback:
            return True
        current_atr = float(atr_series.iloc[-1])
        avg_atr     = float(atr_series.iloc[-self.lookback:-1].mean())
        if avg_atr <= 0:
            return True
        ratio = current_atr / avg_atr
        if ratio < self.min_ratio:
            logger.debug("[VOL_GATE] BLOCKED — ATR ratio %.2f < %.2f", ratio, self.min_ratio)
            return False
        return True
