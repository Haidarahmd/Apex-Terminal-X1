"""
APEX Indicators — all technical indicators in one module.
All functions accept numpy arrays or pandas Series and return float / Series / dict.
No external TA library dependencies — pure numpy/pandas.
"""
import math
import numpy as np
import pandas as pd


# ── EMA ───────────────────────────────────────────────────────────────────────
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def ema_arr(arr, period: int):
    """Lightweight EMA for arrays (used in scanner hot path)."""
    if len(arr) < period:
        return []
    k = 2 / (period + 1)
    prev = sum(arr[:period]) / period
    result = [prev]
    for v in arr[period:]:
        prev = v * k + prev * (1 - k)
        result.append(prev)
    return result


# ── RSI ───────────────────────────────────────────────────────────────────────
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def rsi_scalar(closes, period: int = 14) -> float:
    """Fast scalar RSI for scanner — returns single float."""
    c = list(closes)
    if len(c) < period + 1:
        return 50.0
    g = l = 0.0
    for i in range(len(c) - period, len(c)):
        d = c[i] - c[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + (g / period) / (l / period))


# ── MACD ──────────────────────────────────────────────────────────────────────
def macd(series: pd.Series, fast=12, slow=26, signal=9):
    e_fast  = ema(series, fast)
    e_slow  = ema(series, slow)
    line    = e_fast - e_slow
    sig_line= ema(line, signal)
    hist    = line - sig_line
    return line, sig_line, hist


# ── Bollinger Bands ───────────────────────────────────────────────────────────
def bollinger(series: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid  = series.rolling(period).mean()
    std  = series.rolling(period).std(ddof=0)
    up   = mid + std_mult * std
    dn   = mid - std_mult * std
    return up, mid, dn


def bollinger_scalar(closes, period=20):
    """Returns (upper, mid, lower) scalars for scanner hot path."""
    s = list(closes[-period:])
    if len(s) < period:
        return None, None, None
    mid = sum(s) / period
    std = math.sqrt(sum((v - mid) ** 2 for v in s) / period)
    return mid + 2 * std, mid, mid - 2 * std


# ── ATR ───────────────────────────────────────────────────────────────────────
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def atr_scalar(highs, lows, closes, period=14) -> float:
    """Fast scalar ATR for scanner."""
    h, l, c = list(highs), list(lows), list(closes)
    if len(h) < period + 1:
        return 0.0
    trs = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(h))]
    return sum(trs[-period:]) / period


# ── Stochastic ────────────────────────────────────────────────────────────────
def stochastic(df: pd.DataFrame, period: int = 14, smooth: int = 3):
    lo = df["low"].rolling(period).min()
    hi = df["high"].rolling(period).max()
    k  = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    d  = k.rolling(smooth).mean()
    return k, d


def stoch_scalar(highs, lows, closes, period=14) -> float:
    h, l, c = list(highs[-period:]), list(lows[-period:]), closes
    if len(h) < period:
        return 50.0
    hi, lo = max(h), min(l)
    if hi == lo:
        return 50.0
    return (c[-1] - lo) / (hi - lo) * 100


# ── Support / Resistance Pivots ───────────────────────────────────────────────
def find_pivots(highs, lows, closes, lookback: int = 5):
    """
    Returns (resistance_levels, support_levels) as sorted lists.
    Uses N-bar swing high/low detection.
    """
    h, l = list(highs), list(lows)
    supports, resistances = [], []
    for i in range(lookback, len(h) - lookback):
        if all(l[i] <= l[j] for j in range(i-lookback, i+lookback+1) if j != i):
            supports.append(l[i])
        if all(h[i] >= h[j] for j in range(i-lookback, i+lookback+1) if j != i):
            resistances.append(h[i])
    return sorted(resistances), sorted(supports)


def sr_breakout_type(highs, lows, closes, lookback: int = 5) -> str:
    """Returns 'BULL', 'BEAR', or 'NONE' based on recent S/R breaks."""
    res, sup = find_pivots(highs, lows, closes, lookback)
    last_c, prev_c = closes[-1], closes[-2] if len(closes) > 1 else closes[-1]
    # Bullish breakout — closed above a resistance
    for r in res[-6:]:
        if prev_c < r and last_c > r * 1.003:
            return "BULL"
    # Bearish breakdown — closed below a support
    for s in sup[-6:]:
        if prev_c > s and last_c < s * 0.997:
            return "BEAR"
    return "NONE"


# ── Fair Value Gap ────────────────────────────────────────────────────────────
def detect_fvg(highs, lows, lookback: int = 30):
    """
    Returns (fvg_bull, fvg_bear) booleans.
    Bullish FVG: gap[i].low > gap[i-2].high (imbalance upward)
    Bearish FVG: gap[i].high < gap[i-2].low (imbalance downward)
    """
    h, l = list(highs), list(lows)
    fvg_bull = fvg_bear = False
    start = max(2, len(h) - lookback)
    for i in range(start, len(h)):
        if l[i] > h[i - 2]:
            fvg_bull = True
        if h[i] < l[i - 2]:
            fvg_bear = True
    return fvg_bull, fvg_bear


# ── Market Regime ─────────────────────────────────────────────────────────────
def detect_regime(price, ema20, ema50, ema20_prev, bb_upper, bb_lower, bb_mid) -> str:
    bb_width = (bb_upper - bb_lower) / bb_mid * 100 if bb_mid else 0
    if bb_width > 6 and price > ema20 and ema20 > ema50:
        return "BREAKOUT"
    if price > ema20 and ema20 > ema50 and ema20 > ema20_prev:
        return "TRENDING_UP"
    if price < ema20 and ema20 < ema50 and ema20 < ema20_prev:
        return "TRENDING_DOWN"
    return "RANGING"


# ── Fibonacci Retracement / Extension ─────────────────────────────────────────
FIB_RETRACEMENT_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]
FIB_EXTENSION_RATIOS   = [1.272, 1.618]
FIB_GOLDEN_ZONE = ("382", "500", "618")   # the classic pullback-entry zone


def fib_levels(highs, lows, lookback: int = 50) -> dict | None:
    """
    Fibonacci retracement + extension levels off the most recent significant
    swing high/low within the lookback window.

    Direction is inferred from which extreme happened LESS recently — that's
    the anchor being retraced FROM, since price is currently sitting near
    whichever extreme formed more recently:
      - swing LOW is the more recent extreme -> price topped out earlier,
        then pulled back down to a fresh low -> retracing DOWN off an
        upswing (a pullback in an uptrend) -> retracement levels measured
        down from the (older) high, extensions projected up beyond it.
      - swing HIGH is the more recent extreme -> price bottomed out earlier,
        then bounced up to a fresh high -> retracing UP off a downswing (a
        bounce in a downtrend) -> retracement levels measured up from the
        (older) low, extensions projected down beyond it.

    Returns None if there isn't enough data or the swing has zero range.
    Levels are keyed by the ratio with the decimal point removed, e.g.
    0.618 -> "618", so they can be used as plain dict keys.
    """
    h, l = list(highs), list(lows)
    n = min(len(h), len(l))
    if n < 10:
        return None
    lookback = min(lookback, n)
    window_h = h[-lookback:]
    window_l = l[-lookback:]

    hi = max(window_h)
    lo = min(window_l)
    if hi == lo:
        return None
    # Index of the LAST occurrence of the extreme, so a fresh swing that
    # just formed is picked over a stale one earlier in the window.
    hi_idx = len(window_h) - 1 - window_h[::-1].index(hi)
    lo_idx = len(window_l) - 1 - window_l[::-1].index(lo)

    swing_range = hi - lo
    if hi_idx >= lo_idx:
        direction = "up"      # low came first, high is the fresher extreme -> net move was low->high (an upswing) -> pullback BUY zone
        levels     = {_fib_key(r): hi - swing_range * r for r in FIB_RETRACEMENT_RATIOS}
        extensions = {f"ext_{_fib_key(r)}": hi + swing_range * (r - 1) for r in FIB_EXTENSION_RATIOS}
    else:
        direction = "down"    # high came first, low is the fresher extreme -> net move was high->low (a downswing) -> pullback SELL zone
        levels     = {_fib_key(r): lo + swing_range * r for r in FIB_RETRACEMENT_RATIOS}
        extensions = {f"ext_{_fib_key(r)}": lo - swing_range * (r - 1) for r in FIB_EXTENSION_RATIOS}

    return {
        "direction": direction,
        "swing_high": hi,
        "swing_low": lo,
        "levels": levels,
        "extensions": extensions,
    }


def _fib_key(ratio: float) -> str:
    return str(int(round(ratio * 1000)))


def fib_confluence(price: float, fib: dict | None, tolerance_pct: float = 0.3) -> dict | None:
    """
    Checks whether `price` sits within tolerance_pct% of one of the "golden
    zone" retracement levels (38.2 / 50 / 61.8%) — the zone most commonly
    used as a pullback-entry confluence signal. Returns the closest matching
    level's info, or None if price isn't near any of them / fib is None.
    """
    if not fib:
        return None
    best = None
    for name in FIB_GOLDEN_ZONE:
        target = fib["levels"].get(name)
        if not target or target <= 0:
            continue
        dist_pct = abs(price - target) / target * 100
        if dist_pct <= tolerance_pct and (best is None or dist_pct < best["distance_pct"]):
            best = {"level": name, "target_price": target, "distance_pct": round(dist_pct, 4),
                    "direction": fib["direction"]}
    return best



def is_volume_surge(volumes, window: int = 20, threshold: float = 1.4) -> bool:
    vols = list(volumes)
    if len(vols) < window + 3:
        return False
    avg     = sum(vols[-(window + 3):-3]) / window
    recent3 = sum(vols[-3:]) / 3
    return avg > 0 and recent3 > avg * threshold
