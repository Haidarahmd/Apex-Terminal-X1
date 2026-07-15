"""
Strategy 2 — RSI Reversal + Bollinger Band Touch
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- RSI exits oversold/overbought zones
- Price must touch/cross Bollinger Band
- HTF trend agreement required
"""
import logging
import pandas as pd
from strategies.base import BaseStrategy
from indicators.core import rsi, bollinger, atr, ema

logger = logging.getLogger(__name__)


class RSIReversalStrategy(BaseStrategy):
    name = "rsi_reversal"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.rsi_period = int(p.get("rsi_period",   14))
        self.rsi_ob     = float(p.get("rsi_ob",     70))
        self.rsi_os     = float(p.get("rsi_os",     30))
        self.bb_period  = int(p.get("bb_period",    20))
        self.bb_std     = float(p.get("bb_std",     2.0))
        self.atr_period = int(p.get("atr_period",   14))
        self.ema_trend  = int(p.get("ema_trend",   200))

    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> dict | None:
        min_bars = max(self.bb_period, self.rsi_period, self.ema_trend) + 5
        if df is None or len(df) < min_bars:
            return None

        close   = df["close"]
        atr_val = atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr_val) or atr_val <= 0:
            return None

        rsi_s = rsi(close, self.rsi_period)
        rsi_now  = float(rsi_s.iloc[-1])
        rsi_prev = float(rsi_s.iloc[-2])

        bb_up, bb_mid, bb_dn = bollinger(close, self.bb_period, self.bb_std)
        last_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        up_now, dn_now = float(bb_up.iloc[-1]), float(bb_dn.iloc[-1])

        # EMA trend filter
        ema_val   = ema(close, self.ema_trend)
        ema_now   = float(ema_val.iloc[-1])
        long_bull = last_close > ema_now
        long_bear = last_close < ema_now

        # HTF bias
        htf_bull = htf_bear = True
        if htf_df is not None and len(htf_df) >= 50:
            htf_rsi = rsi(htf_df["close"], self.rsi_period).iloc[-1]
            htf_bull = htf_rsi > 40
            htf_bear = htf_rsi < 60

        # RSI exits oversold AND BB lower band touch
        rsi_exits_os  = rsi_prev < self.rsi_os and rsi_now >= self.rsi_os
        bb_lower_touch = prev_close <= dn_now or last_close <= dn_now * 1.005

        if rsi_exits_os and bb_lower_touch and long_bull and htf_bull:
            return {"side": "buy", "price": last_close, "atr": float(atr_val), "strategy": self.name}

        # RSI exits overbought AND BB upper band touch
        rsi_exits_ob  = rsi_prev > self.rsi_ob and rsi_now <= self.rsi_ob
        bb_upper_touch = prev_close >= up_now or last_close >= up_now * 0.995

        if rsi_exits_ob and bb_upper_touch and long_bear and htf_bear:
            return {"side": "sell", "price": last_close, "atr": float(atr_val), "strategy": self.name}

        return None
