"""
APEX Trading Engine v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The brain. Ties together:
  ✦ Multi-symbol market scan (200+ USDT perps from OKX/Binance/Bybit)
  ✦ 4-strategy signal aggregator with weighted voting
  ✦ Full risk stack: position sizing, drawdown guard, correlation filter,
    partial TP, trailing stop, time-in-position limit
  ✦ Session gate, news blackout, volatility gate
  ✦ Self-learning weight + param optimiser
  ✦ Per-symbol performance scorer (auto-pause bad symbols)
  ✦ Telegram alerts
  ✦ Paper + live execution (OKX / Binance / Bybit)
  ✦ Broker-agnostic — zero MT5 dependency

Run modes:
  python main.py                 — paper trading, default exchange
  python main.py --api           — with REST API server on :8080
  python main.py --mode live     — live trading (set API keys in env)
  python main.py --exchange binance  — use Binance instead of OKX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from config.settings import (
    EXCHANGE, MODE, RISK_PER_TRADE,
    STOP_LOSS_ATR, MIN_RR_RATIO,
    TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT,
    TP1_CLOSE_PCT, TP2_CLOSE_PCT, TP3_CLOSE_PCT,
    SCALP_SL_ATR, SCALP_TP_ATR, SCALP_MIN_RR,
    SINGLE_STRATEGY_SIZE_MULT,
    MAX_OPEN_POSITIONS, MAX_DAILY_DRAWDOWN_PCT, MAX_PORTFOLIO_HEAT_PCT,
    POLL_INTERVAL, SCAN_INTERVAL,
    VOL_GATE_LOOKBACK, VOL_GATE_MIN_RATIO,
    STRATEGY_WEIGHTS, LTF_BARS, HTF_BARS, SCALP_BARS, AGG_THRESHOLD,
)
from data.feed import MarketFeed
from strategies.macd_ema    import MACDEMAStrategy
from strategies.rsi_reversal import RSIReversalStrategy
from strategies.breakout    import BreakoutStrategy
from strategies.scalp       import ScalpStrategy
from strategies.fib_retracement import FibRetracementStrategy
from strategies.aggregator  import SignalAggregator
from execution.router       import ExecutionRouter
from risk.position_sizing   import position_size_usd, qty_from_notional
from risk.drawdown_guard    import DrawdownGuard
from risk.adaptive_risk     import combined_risk_multiplier
from risk.correlation_filter import CorrelationFilter
from risk.trailing_stop     import TrailingStopManager
from risk.tp_ladder         import TPLadderManager, blended_rr
from risk.portfolio_heat    import PortfolioHeatTracker
from filters.session        import is_session_active, current_session_name
from filters.news           import is_news_blackout
from filters.volatility_gate import VolatilityGate
from journal.trade_journal  import TradeJournal
from learning.self_learner  import SelfLearner
from learning.symbol_scorer import SymbolScorer
from utils.telegram         import TelegramNotifier
from utils.equity_tracker   import EquityTracker

logger = logging.getLogger(__name__)

# Parallel workers for multi-symbol data fetch
_FETCH_WORKERS = 8


class TradingEngine:
    def __init__(self, initial_balance: float = 10_000.0):
        logger.info("═══ APEX Terminal  mode=%s  exchange=%s ═══", MODE.upper(), EXCHANGE.upper())

        self.feed    = MarketFeed(EXCHANGE)
        self.exec    = ExecutionRouter(initial_balance)
        self.journal = TradeJournal()

        # Risk modules
        self.dd_guard = DrawdownGuard(MAX_DAILY_DRAWDOWN_PCT)
        self.corr_flt = CorrelationFilter()
        self.heat_tracker = PortfolioHeatTracker(MAX_PORTFOLIO_HEAT_PCT)
        self.trail_mgr= TrailingStopManager()
        self.tp_ladder= TPLadderManager()
        self.vol_gate = VolatilityGate(VOL_GATE_LOOKBACK, VOL_GATE_MIN_RATIO)
        self.scorer   = SymbolScorer()
        self.notifier = TelegramNotifier()
        self.learner  = None   # initialised after first journal read
        self.watchdog = None   # set by run(supervised=True); agent/watchdog.py

        # Strategy setup (rebuilt when learner updates params)
        self._params  = {}
        self._rebuild_strategies()

        # State
        self._cycle        = 0
        self._signals_cache: dict = {}
        self._scan_cache: list    = []
        self._last_scan_ts: float = 0.0
        self._live_pos_meta: dict = {}   # symbol -> {atr, strategy, entry_price, qty, side, opened_at}
                                          # backfills data live exchanges don't echo back (see _manage_positions)
        self._returns_cache: dict = {}   # symbol -> recent close-to-close % returns, refreshed as symbols
                                          # get evaluated as candidates — feeds CorrelationFilter's dynamic check
        self._last_cycle_summary: dict = {
            "candidates_evaluated": 0, "rejections": {}, "entered": [], "cycle": 0,
        }   # why zero trades happened, in plain numbers — see _evaluate_signals

        # Account + equity tracker
        acc = self.exec.get_account()
        self.equity_tracker = EquityTracker(acc["equity"])
        self.learner = SelfLearner(self.journal)

        logger.info("[ENGINE] Balance: %.2f | Mode: %s | Exchange: %s",
                    acc["balance"], MODE.upper(), EXCHANGE.upper())
        self.notifier.startup(EXCHANGE, MODE, acc["equity"])

    # ── Strategy builder ──────────────────────────────────────────────────────
    def _rebuild_strategies(self, params=None):
        p = params or self._params
        strats = [
            MACDEMAStrategy(p),
            RSIReversalStrategy(p),
            BreakoutStrategy(p),
            ScalpStrategy(p),
            FibRetracementStrategy(p),
        ]
        self.aggregator = SignalAggregator(strats)

    # ── State snapshot (for API / UI) ─────────────────────────────────────────
    def get_state(self) -> dict:
        try:
            acc = self.exec.get_account()
            open_positions = self.exec.get_open_positions()
            for p in open_positions:
                p["tp_progress"] = self.tp_ladder.progress(p.get("id", ""))
            return {
                "exchange":       EXCHANGE,
                "mode":           MODE,
                "equity":         acc["equity"],
                "balance":        acc["balance"],
                "open_positions": open_positions,
                "closed_trades":  self.exec.get_closed_trades()[-20:],
                "scan_results":   [
                    {k: v for k, v in r.items() if not k.startswith("_df_")}
                    for r in self._scan_cache[:50]
                ],
                "last_signals":   self._signals_cache,
                "symbol_scores":  self.scorer.summary(),
                "session":        current_session_name(),
                "cycle":          self._cycle,
                "journal_stats":  self.journal.stats(lookback=50),
                "strategy_stats": self.journal.strategy_stats(),
                "weights":        self.learner.get_weights() if self.learner else STRATEGY_WEIGHTS,
                "agent":          self.watchdog.status() if self.watchdog else None,
                "last_cycle_summary": self._last_cycle_summary,
                "paper_session":  self._paper_session_info(),
            }
        except Exception as e:
            logger.error("[ENGINE] get_state error: %s", e)
            return {"error": str(e)}

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self, supervised: bool = True):
        """
        By default the engine is run under the Watchdog (agent/watchdog.py),
        which catches exceptions per-cycle, applies safe auto-fixes, and
        escalates anything else to the patch-review queue instead of letting
        the whole process die. Pass supervised=False to get the old
        unsupervised loop (crashes propagate and kill the process).
        """
        if supervised:
            from agent.watchdog import Watchdog
            self.watchdog = Watchdog(self)
            logger.info("[ENGINE] Running under Watchdog supervision (poll=%ds). Ctrl+C to stop.",
                        POLL_INTERVAL)
            self.watchdog.run_forever(POLL_INTERVAL)
            return

        logger.info("[ENGINE] Running UNSUPERVISED (poll=%ds). Ctrl+C to stop.", POLL_INTERVAL)
        try:
            while True:
                self.process_cycle()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("[ENGINE] Stopped by user.")

    def process_cycle(self):
        self._cycle += 1
        session = current_session_name()
        logger.info("── Cycle %d [%s] ──", self._cycle, session)

        # ── Periodic full scan ────────────────────────────────────────────────
        now_ts = time.monotonic()
        if now_ts - self._last_scan_ts >= SCAN_INTERVAL:
            self._run_scanner()
            self._last_scan_ts = now_ts

        # ── Account status ────────────────────────────────────────────────────
        try:
            acc = self.exec.get_account()
        except Exception as e:
            logger.error("[ENGINE] Account fetch failed: %s", e)
            return

        equity       = acc["equity"]
        balance      = acc["balance"]
        dd_start     = self.equity_tracker.baseline
        current_dd   = self.dd_guard.current_dd(equity, dd_start)

        if not self.dd_guard.allowed(equity, dd_start):
            logger.warning("[ENGINE] Drawdown %.1f%% — trading paused", current_dd * 100)
            self.notifier.drawdown_alert(current_dd * 100, equity)
            # Still manage existing positions
            self._manage_positions(acc)
            return

        # ── Manage open positions ─────────────────────────────────────────────
        self._manage_positions(acc)

        # ── Self-learner tick ─────────────────────────────────────────────────
        if self.learner:
            self.learner.tick(self._cycle)
            new_params = self.learner.get_params()
            if new_params != self._params:
                self._params = new_params
                self._rebuild_strategies(new_params)

        # ── Signal generation per scan result ─────────────────────────────────
        open_count = len(self.exec.get_open_positions())
        if open_count >= MAX_OPEN_POSITIONS:
            logger.debug("[ENGINE] Max positions (%d) reached", MAX_OPEN_POSITIONS)
            return

        # News blackout?
        if is_news_blackout():
            logger.info("[ENGINE] News blackout active — skipping new entries")
            return

        self._evaluate_signals(balance, equity, current_dd)

    # ── Scanner ───────────────────────────────────────────────────────────────
    def _run_scanner(self):
        from core.scanner import run_scan
        logger.info("[ENGINE] Running full market scan...")
        try:
            self._scan_cache = run_scan(self.feed, limit=100)
            logger.info("[ENGINE] Scan complete — %d signals found", len(self._scan_cache))
        except Exception as e:
            logger.error("[ENGINE] Scan error: %s", e)

    # ── Signal evaluation + trade entry ──────────────────────────────────────
    def _evaluate_signals(self, balance: float, equity: float, current_dd: float = 0.0):
        # Reset the per-cycle diagnostic summary FIRST, so even an early
        # return (no candidates at all) still reports a clean zero rather
        # than showing stale numbers from a previous cycle.
        summary = {"candidates_evaluated": 0, "rejections": {}, "entered": [], "cycle": self._cycle}
        self._last_cycle_summary = summary

        def reject(reason: str):
            summary["rejections"][reason] = summary["rejections"].get(reason, 0) + 1

        # Use top scan results as candidates
        candidates = [r for r in self._scan_cache if r["grade"] in ("S+", "A")]
        if not candidates:
            if self._scan_cache:
                logger.info("[ENGINE] 0 of %d scan results graded S+/A this cycle — no candidates to evaluate",
                           len(self._scan_cache))
            return

        open_syms = {p["symbol"] for p in self.exec.get_open_positions()}

        # ── Pre-filter: cheap gates BEFORE any network I/O ───────────────────
        # Run all zero-cost checks up front so we don't waste a scalp-candle
        # fetch on a candidate we'd reject anyway.
        pre_filtered = []
        for candidate in candidates[:20]:
            symbol = candidate["symbol"]
            if not is_session_active(symbol):
                reject("session_inactive"); continue
            if not self.scorer.is_tradeable(symbol):
                reject("paused_by_scorer"); continue
            if not self.corr_flt.allowed(symbol):
                reject("correlation_blocked"); continue
            if symbol in open_syms:
                reject("already_open"); continue
            pre_filtered.append(candidate)

        if not pre_filtered:
            return

        # ── Parallel scalp-candle fetch (only what's actually missing) ───────
        # The scanner already fetched+analysed 1H/4H candles seconds ago and
        # cached the raw DataFrames in candidate["_df_ltf"/"_df_htf"].
        # Re-fetching them here was up to 40 redundant blocking HTTP calls
        # per cycle — the main cause of slow eval times. We now only fetch
        # the 15m scalp timeframe (which the scanner never touches), and do
        # ALL of those fetches concurrently via a thread pool.
        def _fetch_scalp(sym: str):
            try:
                return sym, self.feed.get_candles(sym, "15m", SCALP_BARS)
            except Exception as e:
                return sym, e

        scalp_dfs: dict = {}
        syms_to_fetch = [c["symbol"] for c in pre_filtered]
        with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(syms_to_fetch))) as pool:
            futures = {pool.submit(_fetch_scalp, sym): sym for sym in syms_to_fetch}
            for future in futures:
                sym, result = future.result()
                scalp_dfs[sym] = result

        # ── Signal evaluation ─────────────────────────────────────────────────
        for candidate in pre_filtered:
            symbol = candidate["symbol"]
            summary["candidates_evaluated"] += 1

            # Reuse cached DataFrames — no extra network calls
            df_ltf   = candidate.get("_df_ltf")
            df_htf   = candidate.get("_df_htf")
            df_scalp = scalp_dfs.get(symbol)

            if isinstance(df_scalp, Exception) or df_scalp is None:
                reject("candle_fetch_failed")
                logger.debug("[ENGINE] %s scalp fetch failed: %s", symbol, df_scalp)
                continue

            if df_ltf is None or df_ltf.empty or len(df_ltf) < 50:
                reject("insufficient_candle_data")
                continue

            # Cache return series for dynamic correlation check
            self._returns_cache[symbol] = df_ltf["close"].pct_change().dropna().tolist()
            if not self.corr_flt.allowed(symbol, returns_cache=self._returns_cache):
                reject("correlation_blocked_dynamic")
                continue

            # Volatility gate
            if not self.vol_gate.is_open(df_ltf):
                reject("volatility_gate_blocked")
                continue

            # Regime-blended strategy weights
            weights = self.learner.get_weights(candidate.get("regime")) if self.learner else STRATEGY_WEIGHTS

            # Aggregate strategies — pass scanner grade/side for single-strategy path
            sig = self.aggregator.aggregate(
                df_ltf, htf_df=df_htf, scalp_df=df_scalp, weights=weights,
                scanner_grade=candidate.get("grade"),
                scanner_side=candidate.get("signal"),
            )
            if sig is None:
                reject("no_strategy_agreement")
                continue

            # Direction must agree with scanner signal
            scanner_side = candidate["signal"].lower()
            if sig["side"] != scanner_side:
                reject("strategy_scanner_direction_mismatch")
                continue

            entered = self._enter_trade(symbol, sig, candidate, balance, equity, current_dd)
            if entered is True:
                summary["entered"].append(symbol)
            else:
                reject(entered if isinstance(entered, str) else "entry_rejected_unknown")

            # Check if we've hit max positions
            if len(self.exec.get_open_positions()) >= MAX_OPEN_POSITIONS:
                break

        if summary["candidates_evaluated"] and not summary["entered"]:
            top_reasons = sorted(summary["rejections"].items(), key=lambda x: -x[1])[:3]
            reason_str = ", ".join(f"{k}×{v}" for k, v in top_reasons)
            logger.info("[ENGINE] %d candidates evaluated, 0 trades entered — top reasons: %s",
                       summary["candidates_evaluated"], reason_str or "none recorded")

    def _enter_trade(self, symbol: str, sig: dict, scanner_result: dict,
                     balance: float, equity: float, current_dd: float = 0.0) -> bool | str:
        """Returns True on a successful entry, or a short reason string on rejection."""
        side     = sig["side"]
        price    = sig["price"]
        atr_val  = sig["atr"]
        strategy = ", ".join(sig.get("strategies", [sig.get("strategy", "")]))

        sl_dist  = atr_val * STOP_LOSS_ATR
        tp_levels = self.tp_ladder.levels_for(price, side, atr_val)
        # RR is judged against the REAL blended reward across the TP1/TP2/TP3
        # ladder (accounting for the partial closes at each level), not just
        # the final TP3 target as if the whole position rode there. Note this
        # is still a fixed constant for a given TP-ladder/SL config (every
        # leg scales by the same atr_val, so atr cancels out of the ratio) —
        # it only starts varying trade-to-trade once TP/SL levels themselves
        # depend on something signal-specific rather than pure ATR multiples.
        rr = blended_rr()

        if rr < MIN_RR_RATIO:
            logger.debug("[ENGINE] %s — RR %.2f < %.2f min", symbol, rr, MIN_RR_RATIO)
            return "rr_below_minimum"

        sl = (price - sl_dist) if side == "buy" else (price + sl_dist)

        sl_pct = sl_dist / price * 100

        # Adaptive risk: shrink size on active drawdown / losing streaks,
        # nudge size up or down with signal confidence. See risk/adaptive_risk.py.
        consecutive_losses = self.journal.current_streak()
        risk_mult = combined_risk_multiplier(
            current_dd=current_dd,
            consecutive_losses=consecutive_losses,
            confidence=sig.get("confidence", AGG_THRESHOLD),
            agg_threshold=AGG_THRESHOLD,
        )
        effective_risk_pct = RISK_PER_TRADE * risk_mult
        if risk_mult != 1.0:
            logger.info("[ENGINE] %s — adaptive risk ×%.2f (dd=%.1f%%, streak=%d, conf=%.3f) → risk_pct=%.4f",
                        symbol, risk_mult, current_dd * 100, consecutive_losses,
                        sig.get("confidence", 0.0), effective_risk_pct)

        # Portfolio heat cap — blocks the trade if total committed risk
        # across ALL open positions would exceed MAX_PORTFOLIO_HEAT_PCT of
        # equity, independent of position count / correlation-group checks.
        # See risk/portfolio_heat.py.
        effective_equity = min(balance, equity) if equity > 0 else balance
        risk_usd = effective_equity * effective_risk_pct
        if not self.heat_tracker.allowed(equity, risk_usd):
            return "portfolio_heat_exceeded"

        notional = position_size_usd(balance, equity, effective_risk_pct, sl_pct)
        size_mult= self.scorer.size_multiplier(symbol)
        # Single-strategy entries get sized down — lower conviction than a
        # full multi-strategy consensus, so risk is kept proportionally smaller.
        if sig.get("single_strategy"):
            size_mult *= SINGLE_STRATEGY_SIZE_MULT
            strategy += " [single]"
        notional *= size_mult
        qty = qty_from_notional(notional, price)

        if qty <= 0:
            return "qty_too_small"

        regime = scanner_result.get("regime", "")

        logger.info("[ENGINE] ➤ %s %s %s | qty=%.6f price=%.6f sl=%.6f "
                    "tp1=%.6f tp2=%.6f tp3=%.6f rr=%.2f",
                    side.upper(), symbol, strategy, qty, price, sl,
                    tp_levels["tp1"], tp_levels["tp2"], tp_levels["tp3"], rr)

        try:
            result = self.exec.place_order(
                symbol=symbol, side=side, qty=qty,
                price=price, sl=sl, tp=tp_levels["tp3"],
                tp1=tp_levels["tp1"], tp2=tp_levels["tp2"], tp3=tp_levels["tp3"],
                strategy=strategy, atr=atr_val, regime=regime,
            )
            self.corr_flt.register_open(symbol)
            self.heat_tracker.register_open(symbol, risk_usd)
            self._signals_cache[symbol] = {
                "side": side, "price": price, "strategy": strategy,
                "confidence": sig["confidence"], "ts": datetime.now(tz=timezone.utc).isoformat(),
            }
            if MODE == "live":
                self._live_pos_meta[symbol] = {
                    "atr": atr_val, "strategy": strategy, "entry_price": price,
                    "qty": qty, "side": side, "last_price": price,
                    "opened_at": datetime.now(tz=timezone.utc).isoformat(),
                    "regime": regime,
                }
            self.notifier.trade_opened(symbol, side, qty, price, sl, tp_levels["tp3"], strategy)
            return True
        except Exception as e:
            logger.error("[ENGINE] Order failed for %s: %s", symbol, e)
            self.notifier.error(f"Order failed {symbol}: {e}")
            return "order_placement_failed"

    # ── Position management ───────────────────────────────────────────────────
    def _manage_positions(self, acc: dict):
        positions = self.exec.get_open_positions()

        # Update prices for paper executor
        try:
            tickers = self.feed.get_usdt_perp_tickers()
            price_map = {t["symbol"]: t["price"] for t in tickers}
        except Exception:
            price_map = {}

        # ── TP ladder check BEFORE the full SL/TP3 auto-close below ──────────
        # update_prices() (paper) / the exchange's own SL/TP order (live) only
        # know about a single final exit price (tp3) and the stop-loss — they
        # have no concept of TP1/TP2 partial exits. So the ladder has to be
        # evaluated here, first, on the position's CURRENT (pre-update) price,
        # and apply any partial closes + SL ratchets before price is allowed
        # to auto-close the remainder at tp3 or sl in the step after this one.
        for pos in list(positions):
            symbol = pos["symbol"]
            pos_id = pos["id"]
            if MODE == "live":
                meta = self._live_pos_meta.get(symbol, {})
                pos["atr"] = pos.get("atr") or meta.get("atr", 0)
            atr_val = pos.get("atr", 0)
            if atr_val <= 0:
                continue

            cur_price = price_map.get(symbol, pos.get("current_price", pos["entry_price"]))
            pos["current_price"] = cur_price

            events = self.tp_ladder.check(pos, atr_val)
            for ev in events:
                level, close_pct = ev["level"], ev["close_pct"]
                if level == "tp3":
                    # TP3 = full exit of whatever remains. Let the normal
                    # close path handle it (consistent PnL/journal/reason
                    # handling with an SL hit) rather than a partial-close
                    # call for 100% of qty.
                    closed = self.exec.close_position(pos_id, cur_price, reason="tp3_hit")
                    if closed:
                        self._on_close(closed)
                    break  # position is gone — don't process further events for it
                close_qty = self.tp_ladder.close_qty_for(pos, close_pct)
                if close_qty <= 0:
                    continue
                pnl = self.exec.close_partial(pos_id, close_qty, cur_price)
                pos["qty"] = max(0.0, pos.get("qty", 0) - close_qty)  # keep local copy in sync for next event this loop
                new_sl = self.tp_ladder.sl_after(level, pos)
                if new_sl is not None:
                    self.exec.update_sl(pos_id, new_sl)
                    pos["sl"] = new_sl
                logger.info("[ENGINE] %s %s — closed %.6f @ %.6f pnl=%.4f, SL -> %.6f",
                            symbol, level.upper(), close_qty, cur_price, pnl,
                            new_sl if new_sl is not None else pos.get("sl", 0))

        # Paper executor auto-closes anything that still hits SL or the
        # tp3 ceiling (covers the case where TP1/TP2 never triggered at all
        # and price ran straight to the final target, or hit SL first).
        if price_map and MODE == "paper":
            auto_closed = self.exec.update_prices(price_map)
            for closed in auto_closed:
                self.tp_ladder.reset(closed.get("id", ""))
                self._on_close(closed)

        # Live mode: the exchange's own SL/TP orders close positions
        # server-side — APEX finds out by noticing a tracked symbol has
        # disappeared from the live position list, not via a callback.
        if MODE == "live":
            self._detect_live_closes(positions)

        positions = self.exec.get_open_positions()
        if not positions:
            return

        # Trailing stop for remaining open positions
        for pos in list(positions):
            pos_id = pos["id"]
            symbol = pos["symbol"]

            # Live positions don't carry atr/strategy (the exchange doesn't
            # store that) — backfill from what we cached when the trade was
            # opened, so trailing-stop still works in live mode.
            if MODE == "live":
                meta = self._live_pos_meta.get(symbol, {})
                pos["atr"] = pos.get("atr") or meta.get("atr", 0)
                pos["strategy"] = pos.get("strategy") or meta.get("strategy", "")
            atr_val = pos.get("atr", 0)

            cur_price = price_map.get(symbol, pos.get("current_price", pos["entry_price"]))
            pos["current_price"] = cur_price
            if MODE == "live" and symbol in self._live_pos_meta:
                self._live_pos_meta[symbol]["last_price"] = cur_price

            # Trailing stop only takes over once the ladder has reached TP2
            # (i.e. we're already past breakeven and locking in TP1's level)
            # — letting it run earlier could undercut the planned TP1/TP2
            # ratchets above with a tighter or looser stop than intended.
            if atr_val > 0 and "tp2" in self.tp_ladder.progress(pos_id):
                updated, new_sl = self.trail_mgr.update(pos, atr_val)
                if updated:
                    self.exec.update_sl(pos_id, new_sl)

    def _detect_live_closes(self, positions: list):
        """A symbol we were tracking is no longer in the exchange's open
        position list -> its SL or TP must have filled server-side."""
        current_symbols = {p["symbol"] for p in positions}
        vanished = [s for s in self._live_pos_meta if s not in current_symbols]
        for symbol in vanished:
            meta = self._live_pos_meta.pop(symbol)
            logger.info("[ENGINE] Live position %s no longer open — exchange-side SL/TP likely filled", symbol)
            # We don't know the exact fill price/PnL without trade-history
            # API support (exchange-specific, left as a future enhancement
            # — see README "Known Limitations"). Log a best-effort estimate
            # using the last known price so the journal/scorer still learn.
            est_price = meta.get("last_price", meta.get("entry_price", 0))
            pnl_est = (est_price - meta["entry_price"]) * meta["qty"] if meta["side"] == "buy" \
                else (meta["entry_price"] - est_price) * meta["qty"]
            self._on_close({
                "symbol": symbol, "side": meta["side"], "qty": meta["qty"],
                "entry_price": meta["entry_price"], "exit_price": est_price,
                "pnl": round(pnl_est, 6), "reason": "exchange_sl_tp_fill_estimated",
                "strategy": meta.get("strategy", ""), "opened_at": meta.get("opened_at", ""),
                "closed_at": datetime.now(tz=timezone.utc).isoformat(),
                "regime": meta.get("regime", ""),
            })

    def _paper_session_info(self) -> dict | None:
        """Returns paper account summary for the dashboard — only relevant in paper mode."""
        if MODE != "paper":
            return None
        try:
            from config.settings import PAPER_STATE_FILE
            acc = self.exec.get_account()
            closed    = self.exec.get_closed_trades()
            total_pnl = sum(t.get("pnl", 0) for t in closed)
            # Use executor's own flag (set in __init__) — more precise than
            # just checking if the state file exists (it may exist but be empty).
            restored  = getattr(self.exec, "_restored", PAPER_STATE_FILE.exists())
            return {
                "balance":    acc["balance"],
                "equity":     acc.get("equity", acc["balance"]),
                "total_pnl":  round(total_pnl, 2),
                "n_closed":   len(closed),
                "restored":   restored,
            }
        except Exception:
            return None

    def _on_close(self, closed: dict):
        """Called when a position is auto-closed (SL/TP hit)."""
        symbol   = closed.get("symbol", "")
        side     = closed.get("side", "")
        pnl      = closed.get("pnl", 0.0)
        reason   = closed.get("reason", "")
        strategy = closed.get("strategy", "")

        self._live_pos_meta.pop(symbol, None)
        self.scorer.update(symbol, pnl)
        self.corr_flt.register_close(symbol)
        self.heat_tracker.register_close(symbol)
        self.journal.log_trade({
            **closed,
            "strategy":  strategy,
            "confidence":"",
        })
        self.notifier.trade_closed(symbol, side, pnl, reason, strategy)
        logger.info("[ENGINE] ✓ Closed %s %s pnl=%.4f reason=%s", side.upper(), symbol, pnl, reason)
