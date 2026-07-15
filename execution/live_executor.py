"""
APEX Live Executor — real order execution via exchange REST API.
Supports OKX, Binance USDT-M Futures, Bybit, MEXC Futures.
Uses HMAC-signed requests — credentials loaded from env vars.

Usage:
  Set environment variables:
    OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE   (for OKX)
    BINANCE_API_KEY / BINANCE_API_SECRET             (for Binance)
    BYBIT_API_KEY   / BYBIT_API_SECRET               (for Bybit)
    MEXC_API_KEY    / MEXC_API_SECRET                (for MEXC)

  Then set APEX_MODE=live and APEX_EXCHANGE=okx (or binance/bybit/mexc).

A NOTE ON MEXC SPECIFICALLY: MEXC's futures API requires the account to
have completed KYC before order-placement permissions can be enabled on
an API key — a key can be created pre-KYC but will be read-only for
trading until verification clears (see config/settings.py for the
EXCHANGE-CHOICE README note on why MEXC is offered here for users in
regions where OKX/Binance/Bybit are geo-blocked but MEXC isn't).
MEXC also runs brief scheduled maintenance windows on its futures API
fairly often (typically <35 min); during one, order placement/cancel
fails with a specific error this module surfaces distinctly from a
real outage rather than retrying blindly — see _mexc_post.
"""
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from config.settings import (
    EXCHANGE,
    OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE,
    BINANCE_API_KEY, BINANCE_API_SECRET,
    BYBIT_API_KEY, BYBIT_API_SECRET,
    MEXC_API_KEY, MEXC_API_SECRET,
    ORDER_COMMENT,
)

logger = logging.getLogger(__name__)


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _ts_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LiveExecutor:
    """
    Thin broker-agnostic live order router.
    Methods raise RuntimeError on failure — engine catches and logs.
    """

    def __init__(self, exchange: str = EXCHANGE):
        self.exchange = exchange.lower()
        self._closed_cache: list = []   # session-local; the journal is the durable record
        logger.info("[LIVE] Executor initialised — exchange=%s", self.exchange.upper())

    # ══════════════════════════════════════════════════════════════════════════
    #  PUBLIC INTERFACE
    # ══════════════════════════════════════════════════════════════════════════

    def get_account(self) -> dict:
        if self.exchange == "okx":
            return self._okx_account()
        elif self.exchange == "binance":
            return self._binance_account()
        elif self.exchange == "bybit":
            return self._bybit_account()
        elif self.exchange == "mexc":
            return self._mexc_account()
        raise NotImplementedError(f"Exchange {self.exchange} not implemented")

    def place_order(
        self,
        symbol: str,
        side: str,       # "buy" | "sell"
        qty: float,
        price: float,
        sl: float,
        tp: float,
        strategy: str = "",
        atr: float = 0.0,
        tp1: float | None = None,
        tp2: float | None = None,
        tp3: float | None = None,
        regime: str = "",   # not sent to the exchange — engine._live_pos_meta tracks it locally
    ) -> dict:
        """
        Exchanges only support ONE native take-profit trigger per position
        (OKX tpTriggerPx / Binance TAKE_PROFIT_MARKET / Bybit takeProfit) —
        there's no exchange-side equivalent of "3 separate partial-TP
        triggers with auto SL ratchet on each fill". So the exchange-side
        order always uses the FINAL target (tp3, same value as the old
        single-tp behaviour) as a safety net in case APEX itself is down
        when price gets there. TP1/TP2 partial closes are handled in
        SOFTWARE by core/engine.py's _manage_positions loop calling
        close_partial()/update_sl() — which means TP1/TP2 only fire while
        APEX is actually running and polling prices. If APEX is offline,
        price runs straight to the exchange-side TP3 (or SL) with no
        partial exits, same as before this ladder existed.
        """
        tp_final = tp3 if tp3 is not None else tp
        logger.info("[LIVE] Placing %s %s qty=%.6f sl=%.6f tp1=%s tp2=%s tp3(native)=%.6f",
                    side.upper(), symbol, qty, sl, tp1, tp2, tp_final)
        if self.exchange == "okx":
            return self._okx_order(symbol, side, qty, sl, tp_final)
        elif self.exchange == "binance":
            return self._binance_order(symbol, side, qty, sl, tp_final)
        elif self.exchange == "bybit":
            return self._bybit_order(symbol, side, qty, sl, tp_final)
        elif self.exchange == "mexc":
            return self._mexc_order(symbol, side, qty, sl, tp_final)
        raise NotImplementedError(f"Exchange {self.exchange} not implemented")

    def close_position(self, pos_id: str, price: float, reason: str = "manual") -> dict:
        """
        pos_id here is expected to be the SYMBOL (e.g. "BTC-USDT-SWAP" /
        "BTCUSDT") because, unlike the paper executor, live exchanges track
        positions by symbol, not by an APEX-generated UUID. The engine's
        `get_open_positions()` for live mode (see below) returns the
        exchange's real position list with "id" == symbol for this reason —
        keep that contract if you change either side.
        """
        logger.info("[LIVE] Close position symbol=%s @ %.6f reason=%s", pos_id, price, reason)
        pos_before = next((p for p in self.get_open_positions() if p["symbol"] == pos_id), None)

        if self.exchange == "okx":
            raw = self._okx_close(pos_id, reason)
        elif self.exchange == "binance":
            raw = self._binance_close(pos_id, reason)
        elif self.exchange == "bybit":
            raw = self._bybit_close(pos_id, reason)
        elif self.exchange == "mexc":
            raw = self._mexc_close(pos_id, reason)
        else:
            raise NotImplementedError(f"Exchange {self.exchange} not implemented")

        if pos_before:
            exit_price = price or pos_before.get("current_price", pos_before["entry_price"])
            side = pos_before["side"]
            pnl = (exit_price - pos_before["entry_price"]) * pos_before["qty"] if side == "buy" \
                else (pos_before["entry_price"] - exit_price) * pos_before["qty"]
            closed = {**pos_before, "exit_price": exit_price, "pnl": round(pnl, 6),
                      "reason": reason, "closed_at": _ts_iso()}
            self._closed_cache.append(closed)
            return closed
        return raw

    def close_partial(self, pos_id: str, close_qty: float, price: float) -> float:
        """Reduce-only market order for part of the position; returns estimated realised PnL."""
        pos = next((p for p in self.get_open_positions() if p["symbol"] == pos_id), None)
        if not pos:
            logger.warning("[LIVE] close_partial: no open position for %s", pos_id)
            return 0.0
        close_side = "sell" if pos["side"] == "buy" else "buy"
        try:
            if self.exchange == "okx":
                self._okx_post("/api/v5/trade/order", {
                    "instId": pos_id, "tdMode": "cross", "side": close_side,
                    "ordType": "market", "sz": str(close_qty), "reduceOnly": "true",
                })
            elif self.exchange == "binance":
                self._binance_post("/fapi/v1/order", {
                    "symbol": pos_id, "side": close_side.upper(), "type": "MARKET",
                    "quantity": str(close_qty), "reduceOnly": "true",
                })
            elif self.exchange == "bybit":
                self._bybit_post("/v5/order/create", {
                    "category": "linear", "symbol": pos_id, "side": close_side.capitalize(),
                    "orderType": "Market", "qty": str(close_qty), "reduceOnly": True,
                })
            elif self.exchange == "mexc":
                mexc_pos = next((p for p in self._mexc_positions() if p["symbol"] == pos_id), None)
                self._mexc_post("/api/v1/private/order/create", {
                    "symbol": pos_id, "price": 0, "vol": close_qty,
                    "side": 4 if pos["side"] == "buy" else 2,  # 4=close long, 2=close short
                    "type": 5, "openType": 2,
                    "positionId": mexc_pos["position_id"] if mexc_pos else None,
                })
        except Exception as e:
            logger.error("[LIVE] close_partial order failed for %s: %s", pos_id, e)
            return 0.0

        pnl = (price - pos["entry_price"]) * close_qty if pos["side"] == "buy" \
            else (pos["entry_price"] - price) * close_qty
        return pnl

    def get_closed_trades(self) -> list:
        return list(self._closed_cache)

    def update_sl(self, pos_id: str, new_sl: float):
        """pos_id is the symbol — see note on close_position."""
        logger.info("[LIVE] Update SL symbol=%s -> %.6f", pos_id, new_sl)
        try:
            if self.exchange == "okx":
                self._okx_update_sl(pos_id, new_sl)
            elif self.exchange == "binance":
                self._binance_update_sl(pos_id, new_sl)
            elif self.exchange == "bybit":
                self._bybit_update_sl(pos_id, new_sl)
            elif self.exchange == "mexc":
                self._mexc_update_sl(pos_id, new_sl)
            else:
                raise NotImplementedError(f"Exchange {self.exchange} not implemented")
        except Exception as e:
            logger.error("[LIVE] update_sl failed for %s: %s", pos_id, e)
            raise

    def get_open_positions(self) -> list:
        if self.exchange == "okx":
            return self._okx_positions()
        elif self.exchange == "binance":
            return self._binance_positions()
        elif self.exchange == "bybit":
            return self._bybit_positions()
        elif self.exchange == "mexc":
            return self._mexc_positions()
        raise NotImplementedError(f"Exchange {self.exchange} not implemented")

    # ══════════════════════════════════════════════════════════════════════════
    #  OKX
    # ══════════════════════════════════════════════════════════════════════════

    def _okx_sign(self, ts: str, method: str, path: str, body: str = "") -> dict:
        msg   = ts + method.upper() + path + body
        sig   = hmac.new(OKX_API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
        import base64
        return {
            "OK-ACCESS-KEY":        OKX_API_KEY,
            "OK-ACCESS-SIGN":       base64.b64encode(sig).decode(),
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
            "Content-Type":         "application/json",
        }

    def _okx_post(self, path: str, body: dict) -> dict:
        ts      = _ts_iso()
        payload = json.dumps(body)
        headers = self._okx_sign(ts, "POST", path, payload)
        req = urllib.request.Request(
            f"https://www.okx.com{path}",
            data=payload.encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        if resp.get("code") != "0":
            raise RuntimeError(f"OKX error: {resp}")
        return resp

    def _okx_get_signed(self, path: str) -> dict:
        ts      = _ts_iso()
        headers = self._okx_sign(ts, "GET", path)
        req = urllib.request.Request(f"https://www.okx.com{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _okx_account(self) -> dict:
        resp = self._okx_get_signed("/api/v5/account/balance?ccy=USDT")
        details = resp["data"][0]["details"]
        usdt = next((d for d in details if d["ccy"] == "USDT"), {})
        eq   = float(usdt.get("eq", 0))
        cash = float(usdt.get("cashBal", eq))
        return {"balance": cash, "equity": eq, "mode": "live_okx"}

    def _okx_order(self, symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
        body = {
            "instId":   symbol,
            "tdMode":   "cross",
            "side":     side,
            "ordType":  "market",
            "sz":       str(qty),
            "tpTriggerPx": str(tp),
            "tpOrdPx":     "-1",
            "slTriggerPx": str(sl),
            "slOrdPx":     "-1",
            "clOrdId":  ORDER_COMMENT[:32],
        }
        return self._okx_post("/api/v5/trade/order", body)

    def _okx_positions(self) -> list:
        resp = self._okx_get_signed("/api/v5/account/positions?instType=SWAP")
        out = []
        for p in resp.get("data", []):
            qty = float(p.get("pos", 0))
            if qty == 0:
                continue
            out.append({
                "id": p["instId"], "symbol": p["instId"],
                "side": "buy" if qty > 0 else "sell",
                "qty": abs(qty), "entry_price": float(p.get("avgPx", 0)),
                "current_price": float(p.get("markPx", p.get("avgPx", 0))),
                "unrealised_pnl": float(p.get("upl", 0)),
                "sl": 0.0, "tp": 0.0, "atr": 0.0, "strategy": "",
                "opened_at": "",
            })
        return out

    def _okx_close(self, symbol: str, reason: str) -> dict:
        body = {"instId": symbol, "mgnMode": "cross", "autoCxl": "true"}
        return self._okx_post("/api/v5/trade/close-position", body)

    def _okx_update_sl(self, symbol: str, new_sl: float):
        # OKX: cancel existing algo (SL/TP) orders for this instrument, then
        # re-place just the stop-loss leg at the new trigger price.
        algos = self._okx_get_signed(f"/api/v5/trade/orders-algo-pending?instId={symbol}&ordType=oco")
        for o in algos.get("data", []):
            self._okx_post("/api/v5/trade/cancel-algos", {"algoId": o["algoId"], "instId": symbol})
        pos = next((p for p in self._okx_positions() if p["symbol"] == symbol), None)
        if not pos:
            raise RuntimeError(f"No open OKX position for {symbol} to update SL on")
        close_side = "sell" if pos["side"] == "buy" else "buy"
        self._okx_post("/api/v5/trade/order-algo", {
            "instId": symbol, "tdMode": "cross", "side": close_side,
            "ordType": "conditional", "sz": str(pos["qty"]),
            "slTriggerPx": str(new_sl), "slOrdPx": "-1",
        })

    # ══════════════════════════════════════════════════════════════════════════
    #  BINANCE USDT-M FUTURES
    # ══════════════════════════════════════════════════════════════════════════

    def _binance_sign(self, params: dict) -> str:
        query   = urllib.parse.urlencode(params)
        sig     = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        return query + "&signature=" + sig

    def _binance_post(self, path: str, params: dict) -> dict:
        params["timestamp"] = _ts_ms()
        signed  = self._binance_sign(params)
        req = urllib.request.Request(
            f"https://fapi.binance.com{path}",
            data=signed.encode(),
            headers={"X-MBX-APIKEY": BINANCE_API_KEY, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _binance_get_signed(self, path: str, params: dict = None) -> dict:
        p = params or {}
        p["timestamp"] = _ts_ms()
        signed = self._binance_sign(p)
        req = urllib.request.Request(
            f"https://fapi.binance.com{path}?{signed}",
            headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _binance_account(self) -> dict:
        resp = self._binance_get_signed("/fapi/v2/account")
        bal  = float(resp.get("totalWalletBalance", 0))
        eq   = float(resp.get("totalMarginBalance", bal))
        return {"balance": bal, "equity": eq, "mode": "live_binance"}

    def _binance_order(self, symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
        params = {
            "symbol":          symbol,
            "side":            side.upper(),
            "type":            "MARKET",
            "quantity":        str(qty),
            "newClientOrderId": ORDER_COMMENT[:36],
        }
        result = self._binance_post("/fapi/v1/order", params)
        # Place SL + TP as separate stop orders
        for otype, sp in [("STOP_MARKET", sl), ("TAKE_PROFIT_MARKET", tp)]:
            close_side = "SELL" if side == "buy" else "BUY"
            try:
                self._binance_post("/fapi/v1/order", {
                    "symbol":       symbol,
                    "side":         close_side,
                    "type":         otype,
                    "stopPrice":    str(sp),
                    "closePosition":"true",
                })
            except Exception as e:
                logger.warning("[LIVE] Binance %s order failed: %s", otype, e)
        return result

    def _binance_positions(self) -> list:
        resp = self._binance_get_signed("/fapi/v2/positionRisk")
        out = []
        for p in resp:
            qty = float(p.get("positionAmt", 0))
            if qty == 0:
                continue
            out.append({
                "id": p["symbol"], "symbol": p["symbol"],
                "side": "buy" if qty > 0 else "sell",
                "qty": abs(qty), "entry_price": float(p.get("entryPrice", 0)),
                "current_price": float(p.get("markPrice", 0)),
                "unrealised_pnl": float(p.get("unRealizedProfit", 0)),
                "sl": 0.0, "tp": 0.0, "atr": 0.0, "strategy": "",
                "opened_at": "",
            })
        return out

    def _binance_close(self, symbol: str, reason: str) -> dict:
        positions = self._binance_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if pos is None:
            raise RuntimeError(f"No open Binance position found for {symbol}")
        close_side = "SELL" if pos["side"] == "buy" else "BUY"
        return self._binance_post("/fapi/v1/order", {
            "symbol": symbol, "side": close_side, "type": "MARKET",
            "quantity": str(pos["qty"]), "reduceOnly": "true",
        })

    def _binance_update_sl(self, symbol: str, new_sl: float):
        # Binance: cancel the existing STOP_MARKET order(s) for this symbol,
        # then place a new one at the updated trigger price.
        open_orders = self._binance_get_signed("/fapi/v1/openOrders", {"symbol": symbol})
        for o in open_orders:
            if o.get("type") == "STOP_MARKET":
                self._binance_cancel(symbol, o["orderId"])
        pos = next((p for p in self._binance_positions() if p["symbol"] == symbol), None)
        if not pos:
            raise RuntimeError(f"No open Binance position for {symbol} to update SL on")
        close_side = "SELL" if pos["side"] == "buy" else "BUY"
        self._binance_post("/fapi/v1/order", {
            "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
            "stopPrice": str(new_sl), "closePosition": "true",
        })

    def _binance_cancel(self, symbol: str, order_id):
        params = {"symbol": symbol, "orderId": str(order_id), "timestamp": _ts_ms()}
        signed = self._binance_sign(params)
        req = urllib.request.Request(
            f"https://fapi.binance.com/fapi/v1/order?{signed}",
            headers={"X-MBX-APIKEY": BINANCE_API_KEY},
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    # ══════════════════════════════════════════════════════════════════════════
    #  BYBIT
    # ══════════════════════════════════════════════════════════════════════════

    def _bybit_sign(self, params_str: str) -> str:
        ts  = _ts_ms()
        msg = ts + BYBIT_API_KEY + "5000" + params_str
        return hmac.new(BYBIT_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest(), ts

    def _bybit_post(self, path: str, body: dict) -> dict:
        payload    = json.dumps(body)
        sig, ts    = self._bybit_sign(payload)
        req = urllib.request.Request(
            f"https://api.bybit.com{path}",
            data=payload.encode(),
            headers={
                "X-BAPI-API-KEY":       BYBIT_API_KEY,
                "X-BAPI-SIGN":          sig,
                "X-BAPI-TIMESTAMP":     ts,
                "X-BAPI-RECV-WINDOW":   "5000",
                "Content-Type":         "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Bybit error: {resp}")
        return resp

    def _bybit_account(self) -> dict:
        qs  = f"accountType=UNIFIED&timestamp={_ts_ms()}&api_key={BYBIT_API_KEY}"
        sig, ts = self._bybit_sign(qs)
        req = urllib.request.Request(
            f"https://api.bybit.com/v5/account/wallet-balance?accountType=UNIFIED",
            headers={
                "X-BAPI-API-KEY":     BYBIT_API_KEY,
                "X-BAPI-SIGN":        sig,
                "X-BAPI-TIMESTAMP":   ts,
                "X-BAPI-RECV-WINDOW": "5000",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        coins = resp["result"]["list"][0]["coin"]
        usdt  = next((c for c in coins if c["coin"] == "USDT"), {})
        eq    = float(usdt.get("equity", 0))
        bal   = float(usdt.get("walletBalance", eq))
        return {"balance": bal, "equity": eq, "mode": "live_bybit"}

    def _bybit_order(self, symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
        body = {
            "category":    "linear",
            "symbol":      symbol,
            "side":        side.capitalize(),
            "orderType":   "Market",
            "qty":         str(qty),
            "stopLoss":    str(sl),
            "takeProfit":  str(tp),
            "orderLinkId": ORDER_COMMENT[:36],
        }
        return self._bybit_post("/v5/order/create", body)

    def _bybit_get_signed(self, path: str, query: str = "") -> dict:
        sig, ts = self._bybit_sign(query)
        req = urllib.request.Request(
            f"https://api.bybit.com{path}?{query}",
            headers={
                "X-BAPI-API-KEY":     BYBIT_API_KEY,
                "X-BAPI-SIGN":        sig,
                "X-BAPI-TIMESTAMP":   ts,
                "X-BAPI-RECV-WINDOW": "5000",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _bybit_positions(self) -> list:
        resp = self._bybit_get_signed("/v5/position/list", "category=linear&settleCoin=USDT")
        out = []
        for p in resp.get("result", {}).get("list", []):
            qty = float(p.get("size", 0))
            if qty == 0:
                continue
            side = p.get("side", "").lower()  # "Buy" / "Sell"
            out.append({
                "id": p["symbol"], "symbol": p["symbol"],
                "side": "buy" if side == "buy" else "sell",
                "qty": qty, "entry_price": float(p.get("avgPrice", 0)),
                "current_price": float(p.get("markPrice", 0)),
                "unrealised_pnl": float(p.get("unrealisedPnl", 0)),
                "sl": float(p.get("stopLoss", 0) or 0), "tp": float(p.get("takeProfit", 0) or 0),
                "atr": 0.0, "strategy": "", "opened_at": "",
            })
        return out

    def _bybit_close(self, symbol: str, reason: str) -> dict:
        positions = self._bybit_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if pos is None:
            raise RuntimeError(f"No open Bybit position found for {symbol}")
        close_side = "Sell" if pos["side"] == "buy" else "Buy"
        body = {
            "category": "linear", "symbol": symbol, "side": close_side,
            "orderType": "Market", "qty": str(pos["qty"]), "reduceOnly": True,
        }
        return self._bybit_post("/v5/order/create", body)

    def _bybit_update_sl(self, symbol: str, new_sl: float):
        # Bybit has a dedicated endpoint for modifying SL/TP on an existing
        # position — no cancel/recreate dance needed, unlike OKX/Binance.
        body = {"category": "linear", "symbol": symbol, "stopLoss": str(new_sl)}
        self._bybit_post("/v5/position/trading-stop", body)

    # ══════════════════════════════════════════════════════════════════════════
    #  MEXC
    # ══════════════════════════════════════════════════════════════════════════
    # Symbol format: MEXC contract symbols use underscores, e.g. "BTC_USDT"
    # (vs OKX "BTC-USDT-SWAP" / Binance "BTCUSDT" / Bybit "BTCUSDT"). The
    # engine passes through whatever symbol the data feed produced for the
    # active exchange — make sure APEX_EXCHANGE=mexc is paired with a feed
    # that emits MEXC-style symbols, or adapt at the call site.
    #
    # Signing rule (per MEXC's official docs): signature string =
    #   accessKey + requestTimeMs + paramString
    # where paramString is the sorted "&"-joined query string for GET/DELETE,
    # or the raw JSON body string for POST — NOT the same concatenation
    # order OKX/Bybit use, so this is deliberately its own method rather
    # than reusing _okx_sign/_bybit_sign.

    def _mexc_sign(self, param_string: str) -> tuple[str, str]:
        ts  = _ts_ms()
        msg = MEXC_API_KEY + ts + param_string
        sig = hmac.new(MEXC_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return sig, ts

    def _mexc_headers(self, sig: str, ts: str) -> dict:
        return {
            "ApiKey":        MEXC_API_KEY,
            "Request-Time":  ts,
            "Signature":     sig,
            "Content-Type":  "application/json",
        }

    def _mexc_check_maintenance(self, resp: dict):
        """
        MEXC runs brief (usually <35 min) scheduled futures maintenance
        windows fairly often, during which order placement/cancellation
        specifically fails while query endpoints keep working. Surfacing
        this distinctly (rather than as a generic API error) means the
        Watchdog's diagnostics can treat it as a transient/expected
        condition instead of escalating it as a code bug.
        """
        code = resp.get("code")
        msg  = str(resp.get("message", "")).lower()
        if code in (510, 511) or "maintenance" in msg or "try again" in msg:
            raise RuntimeError(f"MEXC futures API in maintenance window (code={code}): "
                                f"{resp.get('message', '')} — this is a temporary, scheduled "
                                f"condition (typically <35 min), not an account/code problem.")

    def _mexc_post(self, path: str, body: dict) -> dict:
        payload    = json.dumps(body)
        sig, ts    = self._mexc_sign(payload)
        req = urllib.request.Request(
            f"https://api.mexc.com{path}",
            data=payload.encode(),
            headers=self._mexc_headers(sig, ts),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        if not resp.get("success", False):
            self._mexc_check_maintenance(resp)
            raise RuntimeError(f"MEXC error: {resp}")
        return resp

    def _mexc_get_signed(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        param_string = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        sig, ts = self._mexc_sign(param_string)
        qs = ("?" + param_string) if param_string else ""
        req = urllib.request.Request(f"https://api.mexc.com{path}{qs}",
                                       headers=self._mexc_headers(sig, ts))
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        if not resp.get("success", False):
            raise RuntimeError(f"MEXC error: {resp}")
        return resp

    def _mexc_account(self) -> dict:
        resp = self._mexc_get_signed("/api/v1/private/account/assets")
        assets = resp.get("data", [])
        usdt   = next((a for a in assets if a.get("currency") == "USDT"), {})
        eq     = float(usdt.get("equity", 0))
        cash   = float(usdt.get("cashBalance", eq))
        return {"balance": cash, "equity": eq, "mode": "live_mexc"}

    def _mexc_order(self, symbol: str, side: str, qty: float, sl: float, tp: float) -> dict:
        # MEXC side codes: 1=open long, 2=close short, 3=open short, 4=close long
        mexc_side = 1 if side == "buy" else 3
        body = {
            "symbol":          symbol,
            "price":           0,      # ignored for market orders (type=5)
            "vol":             qty,
            "leverage":        1,       # required when opening; APEX sizes via qty, not leverage — see risk/position_sizing.py
            "side":            mexc_side,
            "type":            5,      # 5 = market order
            "openType":        2,      # 2 = cross margin (matches OKX/Bybit "cross" default elsewhere)
            "stopLossPrice":   sl,
            "takeProfitPrice": tp,
            "lossTrend":       1,      # 1 = latest price
            "profitTrend":     1,      # 1 = latest price
            "externalOid":     ORDER_COMMENT[:32],
        }
        return self._mexc_post("/api/v1/private/order/create", body)

    def _mexc_positions(self) -> list:
        resp = self._mexc_get_signed("/api/v1/private/position/open_positions")
        out = []
        for p in resp.get("data", []):
            vol = float(p.get("holdVol", 0))
            if vol == 0:
                continue
            # positionType: 1=long, 2=short
            side = "buy" if p.get("positionType") == 1 else "sell"
            entry = float(p.get("holdAvgPrice", 0))
            # The open_positions endpoint doesn't return a live mark price or
            # unrealised PnL field (only `realised`, which is REALIZED PnL
            # accumulated so far on partial closes — easy to misread as
            # unrealised at a glance, so flagging this explicitly here).
            # Fetch the current price from the public ticker to compute
            # unrealised PnL ourselves, same approach used for OKX above.
            mark = entry
            try:
                tick = self._mexc_get_unsigned(f"/api/v1/contract/ticker?symbol={p['symbol']}")
                mark = float(tick.get("data", {}).get("lastPrice", entry))
            except Exception:
                pass
            unrealised = (mark - entry) * vol if side == "buy" else (entry - mark) * vol
            out.append({
                "id": p["symbol"], "symbol": p["symbol"], "position_id": p.get("positionId"),
                "side": side, "qty": vol,
                "entry_price": entry, "current_price": mark,
                "unrealised_pnl": round(unrealised, 6),
                "sl": 0.0, "tp": 0.0, "atr": 0.0, "strategy": "",
                "opened_at": "",
            })
        return out

    def _mexc_get_unsigned(self, path: str) -> dict:
        """Public market endpoints don't need auth headers at all."""
        with urllib.request.urlopen(f"https://api.mexc.com{path}", timeout=10) as r:
            return json.loads(r.read())

    def _mexc_close(self, symbol: str, reason: str) -> dict:
        pos = next((p for p in self._mexc_positions() if p["symbol"] == symbol), None)
        if pos is None:
            raise RuntimeError(f"No open MEXC position found for {symbol}")
        close_side = 4 if pos["side"] == "buy" else 2  # 4=close long, 2=close short
        body = {
            "symbol": symbol, "price": 0, "vol": pos["qty"],
            "side": close_side, "type": 5, "openType": 2,
            "positionId": pos.get("position_id"),
        }
        return self._mexc_post("/api/v1/private/order/create", body)

    def _mexc_update_sl(self, symbol: str, new_sl: float):
        """
        Uses MEXC's documented `Place TP/SL Order by Position` endpoint
        (POST /api/v1/private/stoporder/place), which is the verified,
        current way to attach/update a stop-loss on an existing position —
        confirmed against MEXC's live API docs rather than guessed. This
        places a position-wide stop-loss (volType=2) at the latest price
        (lossTrend=1), matching how OKX/Binance/Bybit's update_sl behaves
        elsewhere in this file.
        """
        pos = next((p for p in self._mexc_positions() if p["symbol"] == symbol), None)
        if not pos:
            raise RuntimeError(f"No open MEXC position for {symbol} to update SL on")
        self._mexc_post("/api/v1/private/stoporder/place", {
            "positionId": pos.get("position_id"),
            "vol": pos["qty"],
            "stopLossPrice": new_sl,
            "lossTrend": 1,       # 1 = latest price
            "profitTrend": 1,     # required field even though we're only setting SL here
            "volType": 2,         # 2 = whole-position TP/SL (not a partial slice)
            "stopLossType": 0,    # 0 = market SL
            "takeProfitType": 0,  # 0 = market TP (unused here, but required by the API)
        })
