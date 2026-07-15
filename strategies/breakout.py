"""
Strategy 3 — ATR Volatility Breakout
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- N-bar high/low breakout
- Volume surge confirmation (1.4× avg)
- EMA trend filter prevents counter-trend fades
- FVG proximity bonus (institutional order flow)
"""
import logging
import pandas as pd
from strategies.base import BaseStrategy
from indicators.core import ema, atr, is_volume_surge, detect_fvg

logger = logging.getLogger(__name__)


class BreakoutStrategy(BaseStrategy):
    name = "breakout"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.lookback   = int(p.get("lookback",    20))
        self.ema_period = int(p.get("ema_trend",  200))
        self.atr_period = int(p.get("atr_period",  14))
        self.vol_mult   = float(p.get("vol_mult",  1.4))

    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> dict | None:
        min_bars = max(self.lookback, self.ema_period) + 5
        if df is None or len(df) < min_bars:
            return None

        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
        atr_val = atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr_val) or atr_val <= 0:
            return None

        ema_val   = ema(close, self.ema_period)
        ema_now   = float(ema_val.iloc[-1])
        last_c    = float(close.iloc[-1])
        prev_c    = float(close.iloc[-2])

        window_high = float(high.iloc[-(self.lookback + 1):-1].max())
        window_low  = float(low.iloc[-(self.lookback + 1):-1].min())

        vol_surge = is_volume_surge(vol.values, window=20, threshold=self.vol_mult)

        # FVG context
        fvg_bull, fvg_bear = detect_fvg(high.values, low.values, lookback=30)

        # HTF momentum bias
        htf_bull = htf_bear = True
        if htf_df is not None and len(htf_df) >= 50:
            htf_ema  = ema(htf_df["close"], 50)
            htf_last = float(htf_df["close"].iloc[-1])
            htf_bull = htf_last > float(htf_ema.iloc[-1])
            htf_bear = htf_last < float(htf_ema.iloc[-1])

        # Bullish breakout
        if prev_c < window_high and last_c > window_high and vol_surge and last_c > ema_now and htf_bull:
            return {"side": "buy",  "price": last_c, "atr": float(atr_val),
                    "strategy": self.name, "fvg_aligned": fvg_bull}

        # Bearish breakdown
        if prev_c > window_low and last_c < window_low and vol_surge and last_c < ema_now and htf_bear:
            return {"side": "sell", "price": last_c, "atr": float(atr_val),
                    "strategy": self.name, "fvg_aligned": fvg_bear}

        return None
