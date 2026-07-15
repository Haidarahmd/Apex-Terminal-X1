"""
APEX Backtester — historical simulation across any strategy + symbol.
Fetches candles from exchange public API, runs strategies bar-by-bar,
simulates fills, tracks equity curve, outputs performance report.

Usage:
  python main.py --backtest BTC-USDT-SWAP --strategy macd_ema --bars 2000
  python main.py --backtest BTCUSDT --exchange binance --bars 1000
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config.settings import (
    STOP_LOSS_ATR, TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT,
    TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT,
    RISK_PER_TRADE, BACKTEST_DIR,
)
from data.feed import MarketFeed
from indicators.core import atr

logger = logging.getLogger(__name__)


def _build_strategies(params=None):
    from strategies.macd_ema   import MACDEMAStrategy
    from strategies.rsi_reversal import RSIReversalStrategy
    from strategies.breakout   import BreakoutStrategy
    from strategies.scalp      import ScalpStrategy
    return {
        "macd_ema":     MACDEMAStrategy(params),
        "rsi_reversal": RSIReversalStrategy(params),
        "breakout":     BreakoutStrategy(params),
        "scalp":        ScalpStrategy(params),
    }


class Backtester:
    def __init__(self, exchange: str = "okx"):
        self.feed = MarketFeed(exchange)

    def run(
        self,
        symbol: str,
        strategy_name: str = "macd_ema",
        tf: str = "1H",
        htf: str = "4H",
        bars: int = 2000,
        initial_balance: float = 10_000.0,
        params: dict | None = None,
        risk_pct: float = RISK_PER_TRADE,
        sl_atr: float = STOP_LOSS_ATR,
        tp_atr: float = TP3_ATR_MULT,
    ) -> dict:
        """
        Run backtest. Returns performance dict + equity curve.
        """
        logger.info("[BT] Fetching %d bars for %s %s", bars, symbol, tf)

        # Limit API to 500 bars per request; chain requests for more
        chunk  = min(bars, 500)
        df_ltf = self.feed.get_candles(symbol, tf,  chunk)
        df_htf = self.feed.get_candles(symbol, htf, min(200, bars // 5))

        if df_ltf.empty or len(df_ltf) < 50:
            return {"error": f"Not enough data for {symbol}"}

        strategies = _build_strategies(params)
        strat = strategies.get(strategy_name)
        if strat is None:
            return {"error": f"Unknown strategy: {strategy_name}"}

        balance   = initial_balance
        trades    = []
        equity_curve = [initial_balance]
        position  = None  # current open position dict

        atr_series = atr(df_ltf, 14)

        # Walk-forward bar by bar (exclude last bar — it's incomplete)
        for i in range(50, len(df_ltf) - 1):
            df_slice  = df_ltf.iloc[:i+1]
            htf_slice = df_htf.iloc[:min(i // 6 + 1, len(df_htf))] if not df_htf.empty else None

            cur_price = float(df_slice["close"].iloc[-1])
            atr_val   = float(atr_series.iloc[i]) if i < len(atr_series) else 0

            # ── Manage open position ──────────────────────────────────────────
            if position:
                side = position["side"]

                # TP ladder — check in order, applying any partial closes/SL
                # ratchets that haven't fired yet for this position. Mirrors
                # core/engine.py + risk/tp_ladder.py so backtest expectancy
                # reflects the SAME exit behaviour live/paper trading uses,
                # not the old single-TP-only simulation.
                if atr_val > 0:
                    for level, mult, close_pct in (
                        ("tp1", TP1_ATR_MULT, TP1_CLOSE_PCT),
                        ("tp2", TP2_ATR_MULT, TP2_CLOSE_PCT),
                    ):
                        if position.get(f"{level}_done"):
                            continue
                        target = position["entry"] + (1 if side == "buy" else -1) * atr_val * mult
                        hit = (cur_price >= target) if side == "buy" else (cur_price <= target)
                        if not hit:
                            continue
                        close_qty = position["qty"] * close_pct
                        profit = (cur_price - position["entry"]) if side == "buy" else (position["entry"] - cur_price)
                        partial_pnl = profit * close_qty
                        balance += partial_pnl
                        position["qty"] -= close_qty
                        position[f"{level}_done"] = True
                        if level == "tp1":
                            position["sl"] = position["entry"]  # breakeven
                        elif level == "tp2":
                            tp1_price = position["entry"] + (1 if side == "buy" else -1) * atr_val * TP1_ATR_MULT
                            position["sl"] = tp1_price  # lock in TP1 level
                        trades.append({
                            "symbol": symbol, "strategy": strategy_name, "side": side,
                            "entry": position["entry"], "exit": cur_price,
                            "pnl": round(partial_pnl, 6), "reason": f"{level}_partial",
                            "bar_index": i,
                        })

                sl, tp = position["sl"], position["tp"]

                # SL/TP3 check — TP3 (the original "tp" field/full target)
                # closes whatever remains; SL closes whatever remains too.
                exit_price = None
                exit_reason = None
                if side == "buy":
                    if cur_price <= position["sl"]:
                        exit_price = position["sl"]; exit_reason = "sl"
                    elif cur_price >= position["tp"]:
                        exit_price = position["tp"]; exit_reason = "tp3"
                else:
                    if cur_price >= position["sl"]:
                        exit_price = position["sl"]; exit_reason = "sl"
                    elif cur_price <= position["tp"]:
                        exit_price = position["tp"]; exit_reason = "tp3"

                if exit_price:
                    pnl = (exit_price - position["entry"]) * position["qty"] \
                          if side == "buy" else \
                          (position["entry"] - exit_price) * position["qty"]
                    balance += pnl
                    trades.append({
                        "symbol":      symbol,
                        "strategy":    strategy_name,
                        "side":        side,
                        "entry":       position["entry"],
                        "exit":        exit_price,
                        "pnl":         round(pnl, 6),
                        "reason":      exit_reason,
                        "bar_index":   i,
                    })
                    position = None

            # ── New signal ────────────────────────────────────────────────────
            if position is None and atr_val > 0:
                sig = strat.generate_signal(df_slice, htf_slice)
                if sig:
                    side       = sig["side"]
                    entry      = cur_price
                    sl_dist    = atr_val * sl_atr
                    tp_dist    = atr_val * tp_atr
                    sl  = (entry - sl_dist) if side == "buy" else (entry + sl_dist)
                    tp  = (entry + tp_dist) if side == "buy" else (entry - tp_dist)
                    rr  = tp_dist / sl_dist
                    if rr < 1.5:
                        pass  # skip poor RR
                    else:
                        risk_usd  = balance * risk_pct
                        qty       = risk_usd / sl_dist if sl_dist > 0 else 0
                        position  = {
                            "side":  side, "entry": entry, "sl": sl, "tp": tp,
                            "qty":   qty, "tp1_done": False, "tp2_done": False,
                        }

            equity_curve.append(round(balance, 4))

        # Close any remaining open position at last price
        if position:
            final_price = float(df_ltf["close"].iloc[-1])
            side = position["side"]
            pnl  = (final_price - position["entry"]) * position["qty"] if side == "buy" \
                   else (position["entry"] - final_price) * position["qty"]
            balance += pnl
            trades.append({"symbol": symbol, "strategy": strategy_name, "side": side,
                           "entry": position["entry"], "exit": final_price,
                           "pnl": round(pnl, 6), "reason": "end_of_data", "bar_index": len(df_ltf)})

        # ── Stats ─────────────────────────────────────────────────────────────
        pnls  = [t["pnl"] for t in trades]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p < 0]
        total_pnl  = sum(pnls)
        max_eq     = initial_balance
        max_dd     = 0.0
        eq         = initial_balance
        for t in trades:
            eq += t["pnl"]
            max_eq = max(max_eq, eq)
            dd = (max_eq - eq) / max_eq
            max_dd = max(max_dd, dd)

        result = {
            "symbol":       symbol,
            "strategy":     strategy_name,
            "timeframe":    tf,
            "bars":         len(df_ltf),
            "trades":       len(trades),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / max(1, len(pnls)) * 100, 1),
            "total_pnl":    round(total_pnl, 4),
            "total_pnl_pct":round(total_pnl / initial_balance * 100, 2),
            "avg_win":      round(sum(wins) / max(1, len(wins)), 4),
            "avg_loss":     round(sum(losses) / max(1, len(losses)), 4),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "final_balance":round(balance, 2),
            "equity_curve": equity_curve[::10],  # downsample for transport
            "trade_log":    trades[-100:],         # last 100 trades
        }

        # Save to disk
        out_file = BACKTEST_DIR / f"{symbol.replace('/', '_')}_{strategy_name}_{tf}.json"
        with open(out_file, "w") as f:
            json.dump(result, f, indent=2)
        logger.info("[BT] Done — trades=%d win_rate=%.1f%% pnl=%.2f max_dd=%.1f%%",
                    result["trades"], result["win_rate"], total_pnl, result["max_drawdown_pct"])
        return result
