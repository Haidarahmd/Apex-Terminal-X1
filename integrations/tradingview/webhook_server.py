"""
APEX TradingView Integration — Webhook Receiver
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TradingView has no public API for reading a user's personal account
balance — that page is a logged-in web UI, not an exposed endpoint,
and scraping it would mean handling your TradingView session
credentials outside TradingView's own sanctioned channels, which is
a security liability we don't take on here.

What TradingView DOES support natively and reliably is **outgoing
webhook alerts** from Pine Script strategies/indicators. This module
receives those alerts and converts them into APEX trade signals —
this is the standard, ToS-compliant integration pattern (the same
one used by virtually every "TradingView bot" that actually works).

Setup on the TradingView side:
  1. Open your Pine strategy/indicator → "Create Alert"
  2. Condition: your strategy's buy/sell condition
  3. Webhook URL: http://<your-server>:8080/tradingview/webhook
  4. Message (JSON):
       {
         "secret":   "{{YOUR_SHARED_SECRET}}",
         "symbol":   "{{ticker}}",
         "side":     "buy",
         "price":    {{close}},
         "sl":       {{close}} * 0.98,
         "tp":       {{close}} * 1.04,
         "strategy": "tv_pine_breakout"
       }
  5. Set APEX_TV_WEBHOOK_SECRET in your environment to match {{YOUR_SHARED_SECRET}}.

Security: TradingView webhooks are unauthenticated by default — ANY
caller who knows your URL can hit it. The shared secret above is
mandatory; without a matching secret every request is rejected.
Also strongly recommend running the API server behind HTTPS (e.g. a
reverse proxy / Cloudflare Tunnel) rather than exposing :8080 raw,
since alert payloads (and your secret) would otherwise travel in
plaintext.
"""
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SECRET = os.getenv("APEX_TV_WEBHOOK_SECRET", "")
_VALID_SIDES = {"buy", "sell", "close", "close_long", "close_short"}

# Simple in-memory replay/flood guard — keyed by (symbol, side), value = last_ts
_last_seen: dict[tuple, float] = {}
_MIN_INTERVAL_SEC = 5  # ignore duplicate alerts for the same symbol+side within this window
_recent_alerts: deque = deque(maxlen=200)


def _reject(reason: str) -> dict:
    logger.warning("[TV-WEBHOOK] Rejected: %s", reason)
    return {"ok": False, "error": reason}


def handle_webhook(payload: dict, engine=None, raw_headers: dict | None = None) -> dict:
    """
    Validate and process an incoming TradingView alert payload.
    Returns {"ok": True/False, ...}. Never raises — webapp_server wraps
    this defensively, but we don't want a malformed alert to ever crash
    the server thread.
    """
    if not _SECRET:
        return _reject("APEX_TV_WEBHOOK_SECRET is not set on the server — "
                        "webhook intake is disabled until you configure it. "
                        "This prevents anyone who finds your URL from injecting fake trades.")

    if payload.get("secret") != _SECRET:
        return _reject("invalid or missing secret")

    symbol = str(payload.get("symbol", "")).strip().upper()
    side   = str(payload.get("side", "")).strip().lower()

    if not symbol:
        return _reject("missing 'symbol'")
    if side not in _VALID_SIDES:
        return _reject(f"invalid 'side' — must be one of {sorted(_VALID_SIDES)}")

    # Flood/duplicate guard (TradingView sometimes fires an alert more than once)
    key = (symbol, side)
    now = time.time()
    if key in _last_seen and now - _last_seen[key] < _MIN_INTERVAL_SEC:
        return _reject("duplicate alert ignored (same symbol+side within debounce window)")
    _last_seen[key] = now

    record = {
        "symbol": symbol, "side": side,
        "price": _safe_float(payload.get("price")),
        "sl": _safe_float(payload.get("sl")),
        "tp": _safe_float(payload.get("tp")),
        "strategy": payload.get("strategy", "tradingview_webhook"),
        "received_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _recent_alerts.append(record)
    logger.info("[TV-WEBHOOK] %s %s price=%s sl=%s tp=%s strategy=%s",
                side.upper(), symbol, record["price"], record["sl"], record["tp"], record["strategy"])

    if side in ("close", "close_long", "close_short"):
        return _handle_close(symbol, engine, record)
    return _handle_entry(symbol, side, record, engine)


def _safe_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _handle_entry(symbol: str, side: str, record: dict, engine) -> dict:
    if engine is None:
        return {"ok": True, "queued": True, "note": "no live engine attached; alert logged only"}

    if record["price"] is None:
        return _reject("entry alert missing numeric 'price'")

    # Compute SL/TP ladder from ATR if TradingView didn't supply explicit
    # levels, reusing the same risk math AND the same TP1/TP2/TP3 ladder the
    # main engine uses, so webhook trades behave identically to scanner-
    # originated ones (same risk-per-trade, same min-RR, same partial exits)
    # rather than a simplified single-TP version of the strategy.
    try:
        from config.settings import (
            STOP_LOSS_ATR, TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT,
            MIN_RR_RATIO, RISK_PER_TRADE,
        )
        from risk.position_sizing import position_size_usd, qty_from_notional

        price = record["price"]
        sl = record["sl"]
        tp_override = record["tp"]  # if TradingView explicitly set a "tp", treat it as tp3

        if sl is None:
            # Fallback fixed-pct risk if no ATR context is available for this symbol
            sl_pct = 0.015
            sl = price * (1 - sl_pct) if side == "buy" else price * (1 + sl_pct)
        sign = 1 if side == "buy" else -1
        sl_dist = abs(price - sl)

        if tp_override is not None:
            tp3 = tp_override
            # Back out an equivalent ATR-like distance so TP1/TP2 sit
            # proportionally between entry and the explicit TP3 the alert gave us.
            tp3_dist = abs(tp3 - price)
            tp1 = price + sign * tp3_dist * (TP1_ATR_MULT / TP3_ATR_MULT)
            tp2 = price + sign * tp3_dist * (TP2_ATR_MULT / TP3_ATR_MULT)
        else:
            tp1 = price + sign * sl_dist * (TP1_ATR_MULT / STOP_LOSS_ATR)
            tp2 = price + sign * sl_dist * (TP2_ATR_MULT / STOP_LOSS_ATR)
            tp3 = price + sign * sl_dist * (TP3_ATR_MULT / STOP_LOSS_ATR)

        sl_pct = sl_dist / price * 100
        tp_pct = abs(tp3 - price) / price * 100
        rr = tp_pct / sl_pct if sl_pct > 0 else 0
        if rr < MIN_RR_RATIO:
            return _reject(f"R:R {rr:.2f} below MIN_RR_RATIO {MIN_RR_RATIO} — trade skipped")

        acc = engine.exec.get_account()
        notional = position_size_usd(acc["balance"], acc["equity"], RISK_PER_TRADE, sl_pct)
        qty = qty_from_notional(notional, price)
        if qty <= 0:
            return _reject("computed qty <= 0 (balance/risk too small for this stop distance)")

        result = engine.exec.place_order(
            symbol=symbol, side=side, qty=qty, price=price, sl=sl, tp=tp3,
            tp1=tp1, tp2=tp2, tp3=tp3,
            strategy=record["strategy"],
        )
        if hasattr(engine, "corr_flt"):
            engine.corr_flt.register_open(symbol)
        if engine.notifier:
            engine.notifier.trade_opened(symbol, side, qty, price, sl, tp3, record["strategy"])
        return {"ok": True, "result": result, "qty": qty, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}

    except Exception as e:
        logger.error("[TV-WEBHOOK] entry processing failed: %s", e)
        return _reject(f"entry processing error: {e}")


def _handle_close(symbol: str, engine, record: dict) -> dict:
    if engine is None:
        return {"ok": True, "queued": True, "note": "no live engine attached; alert logged only"}
    try:
        positions = [p for p in engine.exec.get_open_positions() if p["symbol"] == symbol]
        if not positions:
            return _reject(f"close alert for {symbol} but no open position found")
        closed = []
        for pos in positions:
            result = engine.exec.close_position(pos["id"], record["price"] or pos.get("current_price", pos["entry_price"]),
                                                  reason="tradingview_webhook")
            if result:
                engine._on_close(result)
                closed.append(result)
        return {"ok": True, "closed": closed}
    except Exception as e:
        logger.error("[TV-WEBHOOK] close processing failed: %s", e)
        return _reject(f"close processing error: {e}")


def recent_alerts(n: int = 50) -> list[dict]:
    return list(_recent_alerts)[-n:]
