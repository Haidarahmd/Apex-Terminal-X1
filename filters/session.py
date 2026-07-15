"""
APEX Session Filter — time-gated trading windows.
Crypto pairs are exempt (24/7 market).
Forex/commodity session rules from trading_bot_v5 preserved + enhanced.
"""
import logging
from datetime import datetime, timezone

from config.settings import SESSION_FILTER_ENABLED, CRYPTO_24H

logger = logging.getLogger(__name__)

# UTC hour ranges: (start_inclusive, end_exclusive)
_SESSIONS = {
    "london":   (7,  16),
    "new_york": (12, 21),
    "asia":     (0,   9),
}

# Dead zones — ultra-low liquidity, avoid regardless
_DEAD_ZONES = [
    (21, 23),  # post-NY close, pre-Asia
]

# Asset class → allowed sessions
_CLASS_SESSIONS = {
    "forex":  {"london", "new_york"},
    "gold":   {"london", "new_york"},
    "silver": {"london", "new_york"},
    "jpy":    {"london", "new_york", "asia"},
    "crypto": None,   # None = always active
    "index":  {"new_york"},
    "oil":    {"london", "new_york"},
}

# Crypto keyword detection
_CRYPTO_KEYWORDS = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE",
                    "DOT", "AVAX", "MATIC", "LINK", "UNI", "USDT", "SWAP"}


def _is_crypto(symbol: str) -> bool:
    s = symbol.upper()
    return (
        any(k in s for k in _CRYPTO_KEYWORDS)
        or s.endswith("-USDT-SWAP")   # OKX perp
        or s.endswith("USDT")          # Binance / Bybit perp
    )


def _utc_hour() -> int:
    return datetime.now(tz=timezone.utc).hour


def _in_session(h: int, start: int, end: int) -> bool:
    if start < end:
        return start <= h < end
    # Wrap-around (e.g. 22–6)
    return h >= start or h < end


def current_session_name() -> str:
    if not SESSION_FILTER_ENABLED:
        return "ALL"
    h = _utc_hour()
    active = [name for name, (s, e) in _SESSIONS.items() if _in_session(h, s, e)]
    return "/".join(active).upper() if active else "DEAD_ZONE"


def is_session_active(symbol: str = "") -> bool:
    if not SESSION_FILTER_ENABLED:
        return True

    # Crypto always active if CRYPTO_24H setting is on
    if CRYPTO_24H and _is_crypto(symbol):
        return True

    h = _utc_hour()

    # Hard dead zone — nothing trades here well
    for (dz_start, dz_end) in _DEAD_ZONES:
        if _in_session(h, dz_start, dz_end):
            return False

    # At least one session must be active
    return any(_in_session(h, s, e) for s, e in _SESSIONS.values())


def session_spread_multiplier() -> float:
    """
    Returns a spread tolerance multiplier based on current session.
    Low liquidity = wider acceptable spread.
    """
    h = _utc_hour()
    # London/NY overlap — tightest spreads
    if 12 <= h < 16:
        return 1.0
    # London open
    if 7 <= h < 12:
        return 1.2
    # NY afternoon
    if 16 <= h < 21:
        return 1.3
    # Asia
    if 0 <= h < 7:
        return 1.8
    return 2.0
