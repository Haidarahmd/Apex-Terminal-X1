"""
APEX Position Sizing — equity-aware, broker-agnostic.
For crypto: returns position size in USD notional (base qty = notional / price).
Uses min(balance, equity) to account for floating losses.
"""
import math


def position_size_usd(
    balance: float,
    equity: float,
    risk_pct: float,
    stop_distance_pct: float,  # stop distance as % of price (e.g. 0.015 = 1.5%)
    leverage: float = 1.0,
    max_usd: float = 1_000_000,
) -> float:
    """
    Returns USD notional to risk.
    stop_distance_pct = (entry - sl) / entry * 100 (absolute value)
    """
    if balance <= 0 or risk_pct <= 0 or stop_distance_pct <= 0:
        return 0.0

    effective = min(balance, equity) if equity > 0 else balance
    risk_usd  = effective * risk_pct
    # notional = risk_usd / stop_distance_pct (so loss at SL = risk_usd)
    notional  = risk_usd / (stop_distance_pct / 100)
    notional  = min(notional * leverage, max_usd)
    return round(notional, 2)


def qty_from_notional(notional_usd: float, price: float, min_qty: float = 0.001) -> float:
    """Convert USD notional to base asset quantity."""
    if price <= 0:
        return min_qty
    return max(min_qty, round(notional_usd / price, 8))
