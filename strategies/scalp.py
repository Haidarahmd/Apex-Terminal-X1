"""
Strategy 4 — Scalp (EMA Fast/Slow Cross + MACD Histogram)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Uses the 15m timeframe for fast momentum entries.
EMA 8/21 crossover + MACD histogram momentum confirmation.
"""
import logging
import pandas as pd
from strategies.base import BaseStrategy
from indicators.core import ema, macd, atr

logger = logging.getLogger(__name__)


class ScalpStrategy(BaseStrategy):
    name = "scalp"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.ema_fast   = int(p.get("scalp_ema_fast",  8))
        self.ema_slow   = int(p.get("scalp_ema_slow",  21))
        self.macd_fast  = int(p.get("macd_fast",       12))
        self.macd_slow  = int(p.get("macd_slow",       26))
        self.macd_sig   = int(p.get("macd_signal",      9))
        self.atr_period = int(p.get("atr_period",      14))

    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> dict | None:
        min_bars = max(self.ema_slow, self.macd_slow) + 10
        if df is None or len(df) < min_bars:
            return None

        close   = df["close"]
        atr_val = atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr_val) or atr_val <= 0:
            return None

        ef = ema(close, self.ema_fast)
        es = ema(close, self.ema_slow)
        _, _, hist = macd(close, self.macd_fast, self.macd_slow, self.macd_sig)

        ef_now, ef_prev = float(ef.iloc[-1]), float(ef.iloc[-2])
        es_now, es_prev = float(es.iloc[-1]), float(es.iloc[-2])
        hist_now = float(hist.iloc[-1])
        hist_prev = float(hist.iloc[-2])
        last_c = float(close.iloc[-1])

        cross_up   = ef_prev <= es_prev and ef_now > es_now
        cross_down = ef_prev >= es_prev and ef_now < es_now
        hist_bull  = hist_now > 0 and hist_now > hist_prev
        hist_bear  = hist_now < 0 and hist_now < hist_prev

        # HTF momentum bias
        htf_bull = htf_bear = True
        if htf_df is not None and len(htf_df) >= 20:
            htf_ef = ema(htf_df["close"], self.ema_fast)
            htf_es = ema(htf_df["close"], self.ema_slow)
            htf_bull = float(htf_ef.iloc[-1]) > float(htf_es.iloc[-1])
            htf_bear = float(htf_ef.iloc[-1]) < float(htf_es.iloc[-1])

        if cross_up and hist_bull and htf_bull:
            return {"side": "buy",  "price": last_c, "atr": float(atr_val), "strategy": self.name}
        if cross_down and hist_bear and htf_bear:
            return {"side": "sell", "price": last_c, "atr": float(atr_val), "strategy": self.name}
        return None
