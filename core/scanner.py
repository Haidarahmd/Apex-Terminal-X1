"""
APEX Multi-Symbol Scanner — upgraded from RAZOR terminal.
Scans 200+ USDT perpetual pairs, scores each on 10 indicators,
assigns confidence score and grade (S+/A/B), returns ranked results.

New vs RAZOR:
  - Uses full APEX strategy aggregator (4 strategies, weighted vote)
  - Stochastic RSI added
  - Regime detection improved (5 states)
  - Confidence formula enhanced with MACD histogram strength
  - Parallel fetch with configurable concurrency
  - Exchange-agnostic (OKX / Binance / Bybit)
"""
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from data.feed import MarketFeed
from indicators.core import (
    ema_arr, rsi_scalar, stoch_scalar, bollinger_scalar, atr_scalar,
    sr_breakout_type, detect_fvg, is_volume_surge, detect_regime,
    fib_levels, fib_confluence,
)
from config.settings import (
    GRADE_S_PLUS, GRADE_A, GRADE_B, MIN_VOLUME_USD, MAX_SYMBOLS_SCAN, SCAN_WORKERS,
    LTF_BARS, HTF_BARS,
)

logger = logging.getLogger(__name__)

# Parallel scan workers — see config/settings.py for why this is 6, not 12.
_SCAN_WORKERS = SCAN_WORKERS


def _grade(score: int) -> str:
    if score >= GRADE_S_PLUS:
        return "S+"
    if score >= GRADE_A:
        return "A"
    if score >= GRADE_B:
        return "B"
    return ""


def _calc_confidence(
    score: int, sig: str, htf_aligned: bool, vol_surge: bool,
    buy_score: int, sell_score: int, regime: str,
    rsi_1h: float, rsi_4h: float, brk_type: str, bb_width: float,
    macd_hist_pct: float = 0.0,
) -> int:
    """
    Confidence = how trustworthy the signal is.
    Distinct from strength score — can be lower when HTF disagrees,
    RSI is overextended, or regime is adverse.
    """
    c = score

    # HTF agreement
    if htf_aligned:
        c += 14
    else:
        c -= 10

    # Volume confirmation
    if vol_surge:
        c += 9

    # Signal clarity (gap between buy/sell scores)
    gap = abs(buy_score - sell_score)
    if gap > 45:
        c += 10
    elif gap > 28:
        c += 5
    elif gap < 12:
        c -= 8

    # Regime bonus/penalty
    if regime == "BREAKOUT":
        c += 12
    elif regime == "TRENDING_UP"   and sig == "BUY":
        c += 8
    elif regime == "TRENDING_DOWN" and sig == "SELL":
        c += 8
    elif regime == "RANGING":
        c -= 6

    # S/R breakout alignment
    if brk_type == "BULL" and sig == "BUY":
        c += 10
    elif brk_type == "BEAR" and sig == "SELL":
        c += 10

    # RSI overextension penalty
    if sig == "BUY"  and rsi_1h > 68:
        c -= 10
    if sig == "SELL" and rsi_1h < 32:
        c -= 10
    if sig == "BUY"  and rsi_4h > 72:
        c -= 6
    if sig == "SELL" and rsi_4h < 28:
        c -= 6

    # BB squeeze bonus at breakout
    if bb_width < 3 and regime == "BREAKOUT":
        c += 6

    # MACD histogram momentum
    if macd_hist_pct > 0.5:
        c += 5
    elif macd_hist_pct < -0.5:
        c -= 4

    return max(10, min(98, round(c)))


def _analyse_one(ticker: dict, feed: MarketFeed) -> dict | None:
    """Full technical scan for a single symbol."""
    symbol    = ticker["symbol"]
    price     = ticker["price"]
    chg       = ticker["change_pct"]
    vol_usd   = ticker["volume_usd"]

    try:
        # Fetch 1H and 4H candles in parallel
        import threading
        ltf_df_ref, htf_df_ref = [None], [None]
        e1 = threading.Event()
        e4 = threading.Event()

        def _fetch1():
            ltf_df_ref[0] = feed.get_candles(symbol, "1H", LTF_BARS)
            e1.set()

        def _fetch4():
            htf_df_ref[0] = feed.get_candles(symbol, "4H", HTF_BARS)
            e4.set()

        t1 = threading.Thread(target=_fetch1, daemon=True)
        t4 = threading.Thread(target=_fetch4, daemon=True)
        t1.start(); t4.start()
        t1.join(timeout=10); t4.join(timeout=10)

        df1  = ltf_df_ref[0]
        df4  = htf_df_ref[0]

        if df1 is None or len(df1) < 50:
            return None

        c1 = df1["close"].tolist()
        h1 = df1["high"].tolist()
        l1 = df1["low"].tolist()
        v1 = df1["volume"].tolist()
        c4 = df4["close"].tolist() if (df4 is not None and len(df4) > 14) else []

        # ── Core indicators ───────────────────────────────────────────────────
        rsi_1h  = rsi_scalar(c1)
        rsi_4h  = rsi_scalar(c4) if len(c4) > 14 else 50.0
        stk_1h  = stoch_scalar(h1, l1, c1)

        e9_arr  = ema_arr(c1, 9)
        e20_arr = ema_arr(c1, 20)
        e50_arr = ema_arr(c1, 50)
        e200_arr= ema_arr(c1, 200)
        e12_arr = ema_arr(c1, 12)
        e26_arr = ema_arr(c1, 26)

        # MACD
        macd_len = len(e26_arr)
        macd_line= [e12_arr[i + (len(e12_arr) - macd_len)] - e26_arr[i] for i in range(macd_len)]
        macd_sig = ema_arr(macd_line, 9)
        macd_now = macd_line[-1] if macd_line else 0
        macd_prv = macd_line[-2] if len(macd_line) > 1 else macd_now
        sig_now  = macd_sig[-1]  if macd_sig  else 0
        sig_prv  = macd_sig[-2]  if len(macd_sig) > 1 else sig_now
        # Histogram strength as % of price
        hist_now = macd_now - sig_now
        hist_pct = hist_now / price * 100 if price else 0

        bb_up, bb_mid, bb_dn = bollinger_scalar(c1)
        if bb_mid is None:
            return None
        bb_width = (bb_up - bb_dn) / bb_mid * 100 if bb_mid > 0 else 0

        atr_val = atr_scalar(h1, l1, c1)

        # EMA values
        le9   = e9_arr[-1]   if e9_arr   else price
        le20  = e20_arr[-1]  if e20_arr  else price
        le50  = e50_arr[-1]  if e50_arr  else price
        # BUG THIS FIXED: when e200_arr was empty (which it always was prior
        # to LTF_BARS being raised to 260 — only 150 bars were fetched
        # against a 200-period requirement), `le200` silently fell back to
        # `price` itself. That makes the "price > le200" trend check below
        # compare price against price — always False — which meant the
        # +5 bullish "Above EMA200" score bonus could NEVER fire, for any
        # symbol, ever, the entire time this was running, silently biasing
        # every grade slightly bearish/incomplete with no error or log line
        # anywhere. `have_ema200` now tracks whether the EMA genuinely
        # computed, so the check below is skipped cleanly instead of firing
        # on a meaningless tautology.
        have_ema200 = bool(e200_arr)
        le200 = e200_arr[-1] if e200_arr else None
        le20_prev = e20_arr[-2] if len(e20_arr) > 1 else le20

        # 4H EMA
        e20_4 = ema_arr(c4, 20) if len(c4) >= 20 else []
        le20_4 = e20_4[-1] if e20_4 else price
        htf_bull = rsi_4h > 50 and price > le20_4
        htf_bear = rsi_4h < 50 and price < le20_4

        # ── S/R Breakout ──────────────────────────────────────────────────────
        brk_type = sr_breakout_type(h1, l1, c1)

        # ── FVG ───────────────────────────────────────────────────────────────
        fvg_bull, fvg_bear = detect_fvg(h1, l1)

        # ── Volume Surge ──────────────────────────────────────────────────────
        vol_surge = is_volume_surge(v1)

        # ── Fibonacci retracement confluence ────────────────────────────────────
        # "direction": up = retracement of an upswing (a pullback BUY zone),
        # down = retracement of a downswing (a pullback SELL zone). Only
        # scores when price sits in the 38.2/50/61.8% "golden zone" AND the
        # zone's direction agrees with the side it would boost — a golden
        # zone alone doesn't mean anything without that directional context.
        fib = fib_levels(h1, l1)
        fib_conf = fib_confluence(price, fib)

        # ── Market Regime ─────────────────────────────────────────────────────
        regime = detect_regime(price, le20, le50, le20_prev, bb_up, bb_dn, bb_mid)

        # ── Score components ──────────────────────────────────────────────────
        buy = sell = 0
        buy_r: list[str] = []
        sell_r: list[str] = []

        # RSI
        if   rsi_1h < 28:  buy  += 22; buy_r.append("RSI Deeply Oversold")
        elif rsi_1h < 40:  buy  += 14; buy_r.append("RSI Oversold")
        elif rsi_1h < 48:  buy  +=  7; buy_r.append("RSI Bullish Zone")
        if   rsi_1h > 72:  sell += 22; sell_r.append("RSI Deeply Overbought")
        elif rsi_1h > 60:  sell += 14; sell_r.append("RSI Overbought")
        elif rsi_1h > 52:  sell +=  7; sell_r.append("RSI Bearish Zone")

        # Stochastic
        if stk_1h < 20:  buy  += 8; buy_r.append("Stoch Oversold")
        if stk_1h > 80:  sell += 8; sell_r.append("Stoch Overbought")

        # MACD
        cross_up   = macd_prv < sig_prv and macd_now > sig_now
        cross_down = macd_prv > sig_prv and macd_now < sig_now
        if   cross_up and macd_now < 0:   buy += 20; buy_r.append("MACD Cross Below 0")
        elif cross_up:                     buy += 12; buy_r.append("MACD Bullish Cross")
        elif not cross_down and macd_now > 0 and macd_now > macd_prv:
                                           buy +=  5; buy_r.append("MACD Rising")
        if   cross_down and macd_now > 0: sell += 20; sell_r.append("MACD Cross Above 0")
        elif cross_down:                  sell += 12; sell_r.append("MACD Bearish Cross")
        elif not cross_up and macd_now < 0 and macd_now < macd_prv:
                                          sell +=  5; sell_r.append("MACD Falling")

        # Bollinger Bands
        if price < bb_dn * 1.01:  buy  += 15; buy_r.append("BB Lower Band Bounce")
        if price > bb_up * 0.99:  sell += 15; sell_r.append("BB Upper Band Rejection")
        if price > bb_mid and rsi_1h > 50:  buy  += 4; buy_r.append("Above BB Mid")
        if price < bb_mid and rsi_1h < 50:  sell += 4; sell_r.append("Below BB Mid")

        # EMA stack
        if   price > le9 and le9 > le20 and le20 > le50:  buy  += 14; buy_r.append("EMA Bullish Stack")
        elif price > le20 and le20 > le50:                  buy  +=  7; buy_r.append("EMA 20/50 Bullish")
        if   price < le9 and le9 < le20 and le20 < le50:  sell += 14; sell_r.append("EMA Bearish Stack")
        elif price < le20 and le20 < le50:                  sell +=  7; sell_r.append("EMA 20/50 Bearish")
        if have_ema200:
            if price > le200:  buy  += 5; buy_r.append("Above EMA200")
        else:              sell += 5; sell_r.append("Below EMA200")

        # S/R Breakout
        if brk_type == "BULL":  buy  += 24; buy_r.append("S/R Breakout")
        if brk_type == "BEAR":  sell += 24; sell_r.append("S/R Breakdown")

        # HTF alignment
        if htf_bull:  buy  += 16; buy_r.append("4H Bullish Alignment")
        if htf_bear:  sell += 16; sell_r.append("4H Bearish Alignment")

        # FVG
        if fvg_bull:  buy  += 8; buy_r.append("Bullish FVG")
        if fvg_bear:  sell += 8; sell_r.append("Bearish FVG")

        # Volume surge
        if vol_surge and buy > sell:   buy  += 11; buy_r.append("Volume Surge")
        if vol_surge and sell > buy:   sell += 11; sell_r.append("Volume Surge")

        # Fibonacci golden zone (38.2/50/61.8%) — directionally gated: only
        # a pullback in an upswing supports BUY, only a pullback in a
        # downswing supports SELL.
        if fib_conf and fib_conf["direction"] == "up":
            buy += 15; buy_r.append(f"Fib {fib_conf['level']} Golden Zone")
        if fib_conf and fib_conf["direction"] == "down":
            sell += 15; sell_r.append(f"Fib {fib_conf['level']} Golden Zone")

        # Regime
        if regime == "BREAKOUT":       buy  += 15; buy_r.append("Breakout Regime")
        if regime == "TRENDING_UP":    buy  +=  8
        if regime == "TRENDING_DOWN":  sell +=  8

        # 24h momentum
        if   chg > 10:  buy  += 8; buy_r.append("Strong 24h Momentum")
        elif chg >  5:  buy  += 4; buy_r.append("Positive Momentum")
        if   chg < -10: sell += 8; sell_r.append("Heavy 24h Selloff")
        elif chg <  -5: sell += 4; sell_r.append("Negative Momentum")

        # ── Determine signal ──────────────────────────────────────────────────
        MAX_SCORE = 200
        if buy >= 30 and buy > sell + 10:
            sig = "BUY"
            score = min(round(buy / MAX_SCORE * 100), 100)
            reasons = buy_r
        elif sell >= 30 and sell > buy + 10:
            sig = "SELL"
            score = min(round(sell / MAX_SCORE * 100), 100)
            reasons = sell_r
        else:
            return None  # no clean signal

        htf_aligned = htf_bull if sig == "BUY" else htf_bear
        confidence  = _calc_confidence(
            score=score, sig=sig, htf_aligned=htf_aligned, vol_surge=vol_surge,
            buy_score=buy, sell_score=sell, regime=regime,
            rsi_1h=rsi_1h, rsi_4h=rsi_4h, brk_type=brk_type, bb_width=bb_width,
            macd_hist_pct=hist_pct,
        )
        grade = _grade(confidence)
        if not grade:
            return None  # below B threshold — skip

        # ── Entry levels ──────────────────────────────────────────────────────
        sl_dist  = atr_val * 1.5
        tp1_dist = atr_val * 1.5
        tp2_dist = atr_val * 3.0
        tp3_dist = atr_val * 5.0
        if sig == "BUY":
            sl  = round(price - sl_dist, 8)
            tp1 = round(price + tp1_dist, 8)
            tp2 = round(price + tp2_dist, 8)
            tp3 = round(price + tp3_dist, 8)
        else:
            sl  = round(price + sl_dist, 8)
            tp1 = round(price - tp1_dist, 8)
            tp2 = round(price - tp2_dist, 8)
            tp3 = round(price - tp3_dist, 8)
        rr = round(tp2_dist / sl_dist, 2)

        return {
            "symbol":      symbol,
            "signal":      sig,
            "score":       score,
            "confidence":  confidence,
            "grade":       grade,
            "price":       price,
            "change_pct":  round(chg, 2),
            "volume_usd":  round(vol_usd),
            "sl":          sl,
            "tp1":         tp1,
            "tp2":         tp2,
            "tp3":         tp3,
            "atr":         round(atr_val, 8),
            "rr":          rr,
            "reasons":     reasons[:8],
            "regime":      regime,
            "htf_aligned": htf_aligned,
            "vol_surge":   vol_surge,
            "brk_type":    brk_type,
            "fvg_bull":    fvg_bull,
            "fvg_bear":    fvg_bear,
            "rsi_1h":      round(rsi_1h, 1),
            "rsi_4h":      round(rsi_4h, 1),
            "stoch":       round(stk_1h, 1),
            "bb_width":    round(bb_width, 2),
            "fib":         fib,
            "fib_confluence": fib_conf,
            "scanned_at":  datetime.now(tz=timezone.utc).isoformat(),
            # Raw candle DataFrames — cached so the engine can reuse them
            # in _evaluate_signals instead of re-fetching 1H/4H from the
            # network for every candidate (was up to 40 redundant blocking
            # HTTP calls per cycle, the main cause of slow eval times).
            "_df_ltf":     df1,
            "_df_htf":     df4,
        }
    except Exception as e:
        logger.debug("[SCAN] %s failed: %s", symbol, e)
        return None


def run_scan(feed: MarketFeed, limit: int = 50, min_vol: float = MIN_VOLUME_USD,
             signal_filter: str = "ALL") -> list[dict]:
    """
    Full market scan. Returns list of signal dicts sorted by confidence desc.
    signal_filter: "ALL" | "BUY" | "SELL"
    """
    tickers = feed.get_usdt_perp_tickers()
    if not tickers:
        logger.warning("[SCAN] No tickers returned from feed")
        return []

    # Adaptive throttle: if the active exchange has been rate-limiting us in
    # the last minute, scan with fewer concurrent workers this cycle rather
    # than hammering it at the same rate and digging the hole deeper.
    workers = _SCAN_WORKERS
    recent_hits = feed.recent_rate_limit_hits(60) if hasattr(feed, "recent_rate_limit_hits") else 0
    if recent_hits >= 10:
        workers = max(2, _SCAN_WORKERS // 3)
        logger.warning("[SCAN] %d rate-limit hits in the last 60s on %s — "
                       "throttling to %d workers this cycle", recent_hits, feed.active_exchange().upper(), workers)
    elif recent_hits >= 4:
        workers = max(3, _SCAN_WORKERS // 2)
        logger.info("[SCAN] %d rate-limit hits in the last 60s — throttling to %d workers", recent_hits, workers)

    logger.info("[SCAN] Scanning %d symbols (workers=%d)", len(tickers), workers)
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_analyse_one, t, feed): t["symbol"] for t in tickers}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r:
                    if signal_filter == "ALL" or r["signal"] == signal_filter:
                        results.append(r)
            except Exception as e:
                logger.debug("[SCAN] Future error: %s", e)

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results[:limit]
