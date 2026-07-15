"""
Strategy 1 — MACD + EMA Trend Follow
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- EMA(200) trend direction on HTF (4H)
- MACD crossover on LTF (1H) for entry timing
- RSI filter: avoids overbought/oversold entries
"""
import logging
import pandas as pd
from strategies.base import BaseStrategy
from indicators.core import ema, macd, rsi, atr

logger = logging.getLogger(__name__)


class MACDEMAStrategy(BaseStrategy):
    name = "macd_ema"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.ema_period  = int(p.get("ema_trend",   200))
        self.macd_fast   = int(p.get("macd_fast",    12))
        self.macd_slow   = int(p.get("macd_slow",    26))
        self.macd_signal = int(p.get("macd_signal",   9))
        self.rsi_period  = int(p.get("rsi_period",   14))
        self.rsi_ob      = float(p.get("rsi_ob",     70))
        self.rsi_os      = float(p.get("rsi_os",     30))
        self.atr_period  = int(p.get("atr_period",   14))

    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> dict | None:
        min_bars = self.ema_period + self.macd_slow + 5
        if df is None or len(df) < min_bars:
            return None

        close   = df["close"]
        atr_val = atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr_val) or atr_val <= 0:
            return None

        # HTF bias
        htf_bias = None
        if htf_df is not None and len(htf_df) >= self.ema_period:
            htf_ema  = ema(htf_df["close"], self.ema_period)
            htf_bias = "bull" if htf_df["close"].iloc[-1] > htf_ema.iloc[-1] else "bear"

        # LTF indicators
        ema_val               = ema(close, self.ema_period)
        macd_line, sig_line, _= macd(close, self.macd_fast, self.macd_slow, self.macd_signal)
        rsi_val               = rsi(close, self.rsi_period).iloc[-1]

        last_close = float(close.iloc[-1])
        ema_now    = float(ema_val.iloc[-1])
        macd_now   = float(macd_line.iloc[-1])
        macd_prev  = float(macd_line.iloc[-2])
        sig_now    = float(sig_line.iloc[-1])
        sig_prev   = float(sig_line.iloc[-2])

        cross_up   = (macd_prev <= sig_prev) and (macd_now > sig_now)
        cross_down = (macd_prev >= sig_prev) and (macd_now < sig_now)
        bull_trend = last_close > ema_now
        bear_trend = last_close < ema_now
        rsi_ok_buy = rsi_val < self.rsi_ob
        rsi_ok_sel = rsi_val > self.rsi_os
        htf_buy    = htf_bias is None or htf_bias == "bull"
        htf_sell   = htf_bias is None or htf_bias == "bear"

        if cross_up and bull_trend and rsi_ok_buy and htf_buy:
            return {"side": "buy",  "price": last_close, "atr": float(atr_val), "strategy": self.name}
        if cross_down and bear_trend and rsi_ok_sel and htf_sell:
            return {"side": "sell", "price": last_close, "atr": float(atr_val), "strategy": self.name}
        return None
