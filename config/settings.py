"""
APEX Terminal — Master Configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Broker-agnostic. Supports:
  - OKX  (live scan + paper execution via public API — no key needed for data)
  - Binance USDT-M Futures (public REST, no key needed for data)
  - Paper mode (simulated fills, any exchange as data source)
  - Custom REST broker (implement execution/custom_executor.py)

No MetaTrader5 dependency. All data is fetched via public REST/WebSocket.
For live order execution, supply your exchange API key + secret below.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os
from pathlib import Path

# ── Project root ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent

# ══════════════════════════════════════════════════════════════════════════════
#  EXECUTION MODE
#  "paper"  — simulated fills, no real orders sent (safe default)
#  "live"   — real orders via your chosen EXCHANGE below
# ══════════════════════════════════════════════════════════════════════════════
MODE = os.getenv("APEX_MODE", "paper")

# ══════════════════════════════════════════════════════════════════════════════
#  EXCHANGE SELECTION
#  "okx"       — OKX USDT perpetual futures
#  "binance"   — Binance USDT-M futures
#  "bybit"     — Bybit USDT perpetual
#  "mexc"      — MEXC USDT-M futures — offered specifically because OKX has
#                formally withdrawn Nigerian retail service and Binance/Bybit
#                are commonly IP-geofenced there too, while MEXC has
#                consistently remained accessible. Requires a KYC-verified
#                account for order-placement API permissions (read-only API
#                keys can be created pre-KYC, but can't trade).
# ══════════════════════════════════════════════════════════════════════════════
EXCHANGE = os.getenv("APEX_EXCHANGE", "mexc")

# ── API credentials (only needed for LIVE mode) ───────────────────────────────
# Set via environment variables — never hardcode secrets
OKX_API_KEY    = os.getenv("OKX_API_KEY",    "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY",    "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

MEXC_API_KEY    = os.getenv("MEXC_API_KEY",    "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")

# ══════════════════════════════════════════════════════════════════════════════
#  SYMBOL SCANNER SETTINGS
# ══════════════════════════════════════════════════════════════════════════════
MAX_SYMBOLS_SCAN = int(os.getenv("APEX_MAX_SYMBOLS", "200"))
MIN_VOLUME_USD   = float(os.getenv("APEX_MIN_VOL", "1_000_000"))

# Concurrent scanner workers fetching candles per cycle. The original
# default of 12 was tripping OKX's per-IP rate limit on every cycle
# (HTTP 429s) when scanning 180+ symbols from a single IP — especially
# common from a single residential/VPS connection rather than a
# distributed setup. Lowered default to 6; override with APEX_SCAN_WORKERS
# if your connection/exchange tolerates more (or needs fewer).
SCAN_WORKERS = int(os.getenv("APEX_SCAN_WORKERS", "6"))

# Timeframes
LTF        = "1H"
HTF        = "4H"
SCALP_TF   = "15m"

# BUG THIS FIXES: these were 150/100/200 — but macd_ema (the highest-weighted
# strategy, 30% of total) needs ema_period(200) + macd_slow(26) + 5 = 231
# bars on the 1H timeframe before it will even attempt to generate a signal
# (rsi_reversal and breakout need 205). With only 150 bars fetched, THREE
# OF FOUR STRATEGIES — 80% of total strategy weight — silently returned
# None on every single call, every cycle, regardless of market conditions.
# Only `scalp` (20% weight) could ever fire, and its max possible vote
# (0.20) can never clear AGG_THRESHOLD (0.38) alone. The result: zero
# trades could ever be entered, full stop — confirmed from a real log
# showing "no_strategy_agreement" as the rejection reason on every single
# cycle across 150+ cycles, with S+/A graded candidates every time.
#
# The scanner's own grading was ALSO silently degraded by the same
# shortage: ema_arr(closes, 200) returns [] when given <200 bars, and
# core/scanner.py's fallback for that case was `le200 = price` — using
# the current price itself as a stand-in for the 200-EMA, which makes
# any trend-position check against it structurally meaningless. So an
# "S+/A" grade computed under the old bar count was missing real
# coverage from one of its 16 indicators the entire time.
LTF_BARS   = 260   # was 150 — now comfortably covers macd_ema's 231-bar floor
HTF_BARS   = 100
SCALP_BARS = 200

# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY WEIGHTS  (auto-normalised — they don't need to sum to 1)
# ══════════════════════════════════════════════════════════════════════════════
STRATEGY_WEIGHTS = {
    "macd_ema":       0.28,
    "rsi_reversal":   0.22,
    "breakout":       0.22,
    "scalp":          0.16,
    "fib_retracement":0.12,
}

AGG_THRESHOLD  = 0.38
AGG_MARGIN     = 0.12
CONFLICT_BLOCK = True

# ══════════════════════════════════════════════════════════════════════════════
#  SINGLE-STRATEGY CONFLUENCE  (fixes the "zero trades ever" bug)
#  With 5 strategies, normalised weights are: macd_ema=0.280, rsi=0.220,
#  breakout=0.220, scalp=0.160, fib=0.120. AGG_THRESHOLD of 0.38 is higher
#  than every single weight — no individual strategy can ever fire alone.
#  This secondary path lets macd_ema (the only one above the 0.25 cutoff)
#  fire alone when the scanner's independent 16-indicator grade agrees
#  directionally (S+ or A). Entries flagged single_strategy are sized down
#  to SINGLE_STRATEGY_SIZE_MULT of normal to reflect the lower conviction.
# ══════════════════════════════════════════════════════════════════════════════
SINGLE_STRATEGY_ENABLED    = os.getenv("APEX_SINGLE_STRATEGY", "1") != "0"
SINGLE_STRATEGY_MIN_WEIGHT = float(os.getenv("APEX_SINGLE_MIN_WEIGHT", "0.25"))
SINGLE_STRATEGY_MIN_GRADE  = os.getenv("APEX_SINGLE_MIN_GRADE", "A")
SINGLE_STRATEGY_SIZE_MULT  = float(os.getenv("APEX_SINGLE_SIZE_MULT", "0.6"))

GRADE_S_PLUS = 65
GRADE_A      = 50
GRADE_B      = 38

# ══════════════════════════════════════════════════════════════════════════════
#  RISK MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
RISK_PER_TRADE         = float(os.getenv("APEX_RISK_PCT", "0.01"))
MAX_OPEN_POSITIONS     = int(os.getenv("APEX_MAX_POS",    "5"))
MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("APEX_MAX_DD",   "0.05"))

# Caps AGGREGATE risk-usd across all open positions, independent of position
# count and correlation-group checks — see risk/portfolio_heat.py. Set below
# the naive MAX_OPEN_POSITIONS * RISK_PER_TRADE ceiling (5% at defaults) to
# leave headroom for the fact that "uncorrelated by our group list" doesn't
# mean "uncorrelated in a market-wide move".
MAX_PORTFOLIO_HEAT_PCT = float(os.getenv("APEX_MAX_HEAT_PCT", "0.04"))

STOP_LOSS_ATR   = 1.5

# Screened against the REAL blended reward across the TP1/TP2/TP3 ladder
# (risk/tp_ladder.py: blended_rr()), not the old TP3-only calculation —
# that one always evaluated to a constant 2.0 (TP3_ATR_MULT/STOP_LOSS_ATR,
# with atr cancelling out of the ratio) regardless of MIN_RR_RATIO, i.e. it
# could never actually reject a trade. The real blended figure at the TP
# ladder settings below is ~1.33, so 1.5 would now reject every trade —
# recalibrated to 1.1 to leave a modest margin under that on the new scale.
# If you change TP1/TP2/TP3_ATR_MULT, TPx_CLOSE_PCT, or STOP_LOSS_ATR below,
# recheck this against the new blended_rr() value.
MIN_RR_RATIO    = 1.1

# ── Take-profit ladder (TP1 / TP2 / TP3) ────────────────────────────────────
# Replaces the old single-TP + one-shot partial-close design. Each level is
# an ATR multiple from entry and closes a percentage of whatever quantity
# remains open at the time it triggers (not of the original size), so the
# three percentages naturally consume the whole position:
#   TP1 hits  -> close TP1_CLOSE_PCT of remaining qty, SL -> breakeven
#   TP2 hits  -> close TP2_CLOSE_PCT of what's left, SL -> TP1 price (lock more in)
#   TP3 hits  -> close the rest (100% of remaining)
# TP3's distance matches the old TAKE_PROFIT_ATR=3.0 default, so existing
# expectancy/backtests at the FINAL target are unchanged — TP1/TP2 just give
# you earlier, partial exits along the way instead of all-or-nothing.
TP1_ATR_MULT    = 1.0
TP2_ATR_MULT    = 2.0
TP3_ATR_MULT    = 3.0
TP1_CLOSE_PCT   = 0.40   # of remaining qty
TP2_CLOSE_PCT   = 0.35   # of remaining qty (so cumulative ≈ 61% by TP2)
TP3_CLOSE_PCT   = 1.00   # closes everything left

SCALP_SL_ATR  = 1.0
SCALP_TP_ATR  = 2.0
SCALP_MIN_RR  = 1.5

TRAIL_ACTIVATION_ATR = 1.0
TRAIL_STEP_ATR       = 0.5

COMPOUND_MODE = "equity"

# ══════════════════════════════════════════════════════════════════════════════
#  FILTERS
# ══════════════════════════════════════════════════════════════════════════════
SESSION_FILTER_ENABLED = True
CRYPTO_24H             = True

NEWS_BLACKOUT_MINUTES = 30
NEWS_LIVE_FEED        = True

VOL_GATE_ENABLED   = True
VOL_GATE_LOOKBACK  = 20
VOL_GATE_MIN_RATIO = 0.8

MAX_SPREAD_PCT = 0.05

# ══════════════════════════════════════════════════════════════════════════════
#  SELF-LEARNER
# ══════════════════════════════════════════════════════════════════════════════
LEARNING_ENABLED         = True
LEARNING_INTERVAL_CYCLES = 20
LEARNING_LOOKBACK        = 50
PARAM_EXPLORE_SIGMA      = 0.08

# ══════════════════════════════════════════════════════════════════════════════
#  SYMBOL SCORER
# ══════════════════════════════════════════════════════════════════════════════
PERF_SCORE_LOOKBACK     = 30
PERF_SCORE_MIN_WIN_RATE = 0.40
PERF_SCORE_SIZE_SCALE   = True

# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ══════════════════════════════════════════════════════════════════════════════
#  ENGINE TIMING
# ══════════════════════════════════════════════════════════════════════════════
POLL_INTERVAL = 10
SCAN_INTERVAL = 60

# ══════════════════════════════════════════════════════════════════════════════
#  PATHS
# ══════════════════════════════════════════════════════════════════════════════
DATA_DIR     = ROOT / "data_store"
JOURNAL_FILE = DATA_DIR / "trade_journal.csv"
EQUITY_FILE  = DATA_DIR / "equity_baseline.json"
PARAMS_FILE  = DATA_DIR / "learned_params.json"
SCORE_FILE   = DATA_DIR / "symbol_scores.json"
BACKTEST_DIR = DATA_DIR / "backtests"
PAPER_STATE_FILE = DATA_DIR / "paper_state.json"   # persists balance + open positions across restarts

DATA_DIR.mkdir(exist_ok=True)
BACKTEST_DIR.mkdir(exist_ok=True)

# ── Network / DNS bypass ──────────────────────────────────────────────────────
# Set APEX_USE_DOH=1 in your environment (or .env file) to route all crypto
# exchange DNS lookups through DNS-over-HTTPS (Cloudflare + Google), bypassing
# ISP-level geo-blocking common in Nigeria and similar markets.
APEX_USE_DOH = os.getenv("APEX_USE_DOH", "0") != "0"

ORDER_COMMENT = "APEX_v1"
DEVIATION     = 20

DEFAULT_PARAMS = {
    "ema_trend":      200,
    "macd_fast":       12,
    "macd_slow":       26,
    "macd_signal":      9,
    "atr_period":      14,
    "rsi_period":      14,
    "rsi_ob":          70,
    "rsi_os":          30,
    "bb_period":       20,
    "bb_std":         2.0,
    "lookback":        20,
    "scalp_ema_fast":   8,
    "scalp_ema_slow":  21,
    "stoch_period":    14,
}
