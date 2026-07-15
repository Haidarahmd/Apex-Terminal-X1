"""
APEX Paper Executor — full-featured simulated trading with state persistence.

Balance and open positions survive restarts. The engine no longer resets
to $10,000 every time — it picks up exactly where it left off.

What persists across restarts (data_store/paper_state.json):
  - balance: live account equity, carries all PnL from previous sessions
  - open positions: any trades still open at the time of shutdown
  - recent closed trades: the in-memory list for get_closed_trades()

To wipe and start fresh: delete data_store/paper_state.json, or use the
dashboard Settings → "Reset paper account" button (POST /paper/reset).
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from config.settings import PAPER_STATE_FILE

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class PaperExecutor:
    """
    Simulates order execution. Each position is a dict with standard APEX fields.
    State is checkpointed to disk on every trade event so the account survives restarts.
    """

    def __init__(self, initial_balance: float = 10_000.0):
        saved = self._load_state()
        if saved:
            self._balance   = float(saved["balance"])
            self._positions = {p["id"]: p for p in saved.get("positions", [])}
            self._closed    = saved.get("closed", [])[-200:]   # cap to last 200 to avoid unbounded growth
            self._restored  = True
            logger.info("[PAPER] Restored state — balance=%.2f  open=%d  prev_closed=%d",
                        self._balance, len(self._positions), len(self._closed))
        else:
            self._balance        = initial_balance
            self._positions: dict[str, dict] = {}
            self._closed:    list[dict]       = []
            self._restored   = False
            logger.info("[PAPER] Fresh account — balance=%.2f", self._balance)
        self._equity = self._balance

    # ── Account ────────────────────────────────────────────────────────────────
    def get_account(self) -> dict:
        unrealised = sum(p.get("unrealised_pnl", 0.0) for p in self._positions.values())
        self._equity = self._balance + unrealised
        return {
            "balance":   round(self._balance, 2),
            "equity":    round(self._equity,  2),
            "mode":      "paper",
            "restored":  getattr(self, "_restored", False),
        }

    # ── Open order ─────────────────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        side: str,          # "buy" | "sell"
        qty: float,
        price: float,
        sl: float,
        tp: float,
        strategy: str = "",
        atr: float = 0.0,
        tp1: float | None = None,
        tp2: float | None = None,
        tp3: float | None = None,
        regime: str = "",
    ) -> dict:
        pos_id = str(uuid.uuid4())[:8]
        # tp stays as the FINAL exit target (same role as before — full
        # close, used by update_prices()). tp1/tp2 default to tp itself
        # when not supplied (e.g. a caller still using the old 2-arg style)
        # so the ladder degenerates gracefully to "everything at the final
        # target" rather than erroring.
        position = {
            "id":            pos_id,
            "symbol":        symbol,
            "side":          side,
            "qty":           qty,
            "entry_price":   price,
            "current_price": price,
            "sl":            sl,
            "tp":            tp,
            "tp1":           tp1 if tp1 is not None else tp,
            "tp2":           tp2 if tp2 is not None else tp,
            "tp3":           tp3 if tp3 is not None else tp,
            "strategy":      strategy,
            "atr":           atr,
            "regime":        regime,
            "unrealised_pnl":0.0,
            "tick_size":     self._guess_tick(price),
            "opened_at":     datetime.now(tz=timezone.utc).isoformat(),
        }
        self._positions[pos_id] = position
        logger.info("[PAPER] OPEN %s %s %.6f @ %.6f | SL=%.6f TP1=%.6f TP2=%.6f TP3=%.6f | id=%s",
                    side.upper(), symbol, qty, price, sl,
                    position["tp1"], position["tp2"], position["tp3"], pos_id)
        self._save_state()
        return position

    # ── Close position ─────────────────────────────────────────────────────────
    def close_position(self, pos_id: str, price: float, reason: str = "manual") -> dict | None:
        pos = self._positions.pop(pos_id, None)
        if pos is None:
            logger.warning("[PAPER] close_position: id=%s not found", pos_id)
            return None
        side = pos["side"]
        pnl  = (price - pos["entry_price"]) * pos["qty"] if side == "buy" \
               else (pos["entry_price"] - price) * pos["qty"]
        self._balance += pnl
        closed = {**pos, "exit_price": price, "pnl": round(pnl, 6),
                  "reason": reason, "closed_at": datetime.now(tz=timezone.utc).isoformat()}
        self._closed.append(closed)
        logger.info("[PAPER] CLOSE %s %s @ %.6f | PnL=%.4f | balance=%.2f | reason=%s",
                    pos.get("symbol"), pos_id, price, pnl, self._balance, reason)
        self._save_state()
        return closed

    def close_partial(self, pos_id: str, close_qty: float, price: float) -> float:
        """Close partial qty, returns realised PnL for that slice."""
        pos = self._positions.get(pos_id)
        if not pos:
            return 0.0
        side = pos["side"]
        pnl  = (price - pos["entry_price"]) * close_qty if side == "buy" \
               else (pos["entry_price"] - price) * close_qty
        pos["qty"] = round(pos["qty"] - close_qty, 10)
        self._balance += pnl
        logger.info("[PAPER] PARTIAL CLOSE %s %.6f @ %.6f | PnL=%.4f",
                    pos_id, close_qty, price, pnl)
        self._save_state()
        return pnl

    # ── Update prices ──────────────────────────────────────────────────────────
    def update_prices(self, prices: dict[str, float]) -> list[dict]:
        """
        Feed current market prices. Auto-closes positions that hit SL/TP.
        Returns list of auto-closed positions.
        """
        auto_closed = []
        for pos_id, pos in list(self._positions.items()):
            sym   = pos["symbol"]
            price = prices.get(sym)
            if price is None:
                continue
            pos["current_price"] = price
            side = pos["side"]
            if side == "buy":
                pos["unrealised_pnl"] = (price - pos["entry_price"]) * pos["qty"]
                if price <= pos["sl"]:
                    r = self.close_position(pos_id, price, reason="sl_hit")
                    if r: auto_closed.append(r)
                elif price >= pos["tp"]:
                    r = self.close_position(pos_id, price, reason="tp_hit")
                    if r: auto_closed.append(r)
            else:
                pos["unrealised_pnl"] = (pos["entry_price"] - price) * pos["qty"]
                if price >= pos["sl"]:
                    r = self.close_position(pos_id, price, reason="sl_hit")
                    if r: auto_closed.append(r)
                elif price <= pos["tp"]:
                    r = self.close_position(pos_id, price, reason="tp_hit")
                    if r: auto_closed.append(r)
        return auto_closed

    def update_sl(self, pos_id: str, new_sl: float):
        if pos_id in self._positions:
            self._positions[pos_id]["sl"] = new_sl
            self._save_state()   # persist SL ratchet so it survives restart too

    def get_open_positions(self) -> list[dict]:
        return list(self._positions.values())

    def get_closed_trades(self) -> list[dict]:
        return list(self._closed)

    # ── State persistence ──────────────────────────────────────────────────────
    def _load_state(self) -> dict | None:
        try:
            if PAPER_STATE_FILE.exists():
                with open(PAPER_STATE_FILE) as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("[PAPER] Could not load saved state (%s) — starting fresh", e)
        return None

    def _save_state(self):
        try:
            state = {
                "balance":   round(self._balance, 8),
                "positions": list(self._positions.values()),
                "closed":    self._closed[-200:],   # last 200 only; journal CSV is the full record
                "saved_at":  datetime.now(tz=timezone.utc).isoformat(),
            }
            with open(PAPER_STATE_FILE, "w") as f:
                json.dump(state, f, default=str)
        except Exception as e:
            logger.warning("[PAPER] State save failed: %s", e)

    def reset_state(self, initial_balance: float = 10_000.0):
        """Wipe persisted state and restart fresh. Called by the /paper/reset endpoint."""
        self._balance   = initial_balance
        self._equity    = initial_balance
        self._positions = {}
        self._closed    = []
        try:
            PAPER_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._restored = False
        logger.info("[PAPER] Account reset — balance=%.2f", self._balance)

    @staticmethod
    def _guess_tick(price: float) -> float:
        if price > 10_000:  return 0.1
        if price > 1_000:   return 0.01
        if price > 10:      return 0.0001
        if price > 0.1:     return 0.00001
        return 0.0000001
