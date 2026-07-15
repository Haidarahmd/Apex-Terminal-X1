"""
Strategy 5 — Fibonacci Retracement Pullback
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Classic fib pullback-continuation setup:
  - Find the most recent significant swing (see indicators.core.fib_levels)
  - Price must be sitting in the 38.2/50/61.8% "golden zone" retracement
    of that swing
  - The swing's direction must agree with the trade direction (a golden
    zone on its own means nothing without this — see fib_confluence)
  - EMA trend filter + HTF agreement, same discipline as the other
    strategies, so this doesn't fire on a golden-zone touch inside a
    contradicting broader trend
  - A momentum-turn confirmation (RSI turning back up/down off the zone)
    is required rather than firing the instant price touches the level,
    since raw fib levels alone are a very weak, extremely common signal —
    price touches SOME fib ratio constantly. The turn confirmation is what
    makes this a real setup instead of noise.
"""
import logging
import pandas as pd
from strategies.base import BaseStrategy
from indicators.core import ema, atr, rsi, fib_levels, fib_confluence

logger = logging.getLogger(__name__)


class FibRetracementStrategy(BaseStrategy):
    name = "fib_retracement"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.swing_lookback = int(p.get("swing_lookback", 50))
        self.tolerance_pct  = float(p.get("tolerance_pct", 0.3))
        self.ema_trend      = int(p.get("ema_trend",       200))
        self.atr_period     = int(p.get("atr_period",       14))
        self.rsi_period     = int(p.get("rsi_period",       14))

    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> dict | None:
        min_bars = max(self.swing_lookback, self.ema_trend, self.rsi_period) + 5
        if df is None or len(df) < min_bars:
            return None

        close, high, low = df["close"], df["high"], df["low"]
        atr_val = atr(df, self.atr_period).iloc[-1]
        if pd.isna(atr_val) or atr_val <= 0:
            return None

        last_c = float(close.iloc[-1])

        fib = fib_levels(high.values, low.values, lookback=self.swing_lookback)
        conf = fib_confluence(last_c, fib, tolerance_pct=self.tolerance_pct)
        if not conf:
            return None

        # EMA trend filter — the golden-zone pullback should be WITH the
        # broader trend, not a counter-trend guess.
        ema_now = float(ema(close, self.ema_trend).iloc[-1])
        trend_up   = last_c > ema_now
        trend_down = last_c < ema_now

        # HTF agreement, same discipline as the other strategies.
        htf_bull = htf_bear = True
        if htf_df is not None and len(htf_df) >= 50:
            htf_ema  = ema(htf_df["close"], 50)
            htf_last = float(htf_df["close"].iloc[-1])
            htf_bull = htf_last > float(htf_ema.iloc[-1])
            htf_bear = htf_last < float(htf_ema.iloc[-1])

        # Momentum-turn confirmation — RSI ticking back up/down off the
        # zone, not just a raw price touch (which happens constantly and
        # carries almost no signal on its own).
        rsi_s = rsi(close, self.rsi_period)
        rsi_turning_up   = float(rsi_s.iloc[-1]) > float(rsi_s.iloc[-2])
        rsi_turning_down = float(rsi_s.iloc[-1]) < float(rsi_s.iloc[-2])

        # conf["direction"] == "up"   -> pullback in an upswing -> BUY zone
        # conf["direction"] == "down" -> pullback in a downswing -> SELL zone
        if conf["direction"] == "up" and trend_up and htf_bull and rsi_turning_up:
            return {"side": "buy", "price": last_c, "atr": float(atr_val),
                    "strategy": self.name, "fib_level": conf["level"]}

        if conf["direction"] == "down" and trend_down and htf_bear and rsi_turning_down:
            return {"side": "sell", "price": last_c, "atr": float(atr_val),
                    "strategy": self.name, "fib_level": conf["level"]}

        return None
