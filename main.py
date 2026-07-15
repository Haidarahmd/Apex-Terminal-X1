"""
APEX Terminal — Entry Point
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
  python main.py                              # paper trading, OKX — dashboard UI on :8080 by default
  python main.py --no-api                     # console-only, no dashboard (e.g. running under systemd)
  python main.py --mode live                  # live trading
  python main.py --exchange binance           # use Binance
  python main.py --backtest BTC-USDT-SWAP     # run backtest
  python main.py --scan                       # one-shot market scan, then exit
  python main.py --port 9000                  # custom dashboard port

The dashboard (open http://localhost:8080 after starting) is where you'll
SEE things the console log only states in passing — exchange reachability
per-exchange (including whether a 451/403 is a geo-block vs a transient
blip), the Watchdog's pending patch queue, and connected broker accounts.
If you only ever watch the terminal, you're missing the part of this
built specifically to make these issues visible rather than buried in
scrolling log lines.

Environment variables (set before running):
  APEX_MODE          paper | live
  APEX_EXCHANGE      okx | binance | bybit | mexc
  APEX_RISK_PCT      0.01  (1% per trade)
  APEX_MAX_DD        0.05  (5% daily drawdown halt)
  APEX_MAX_POS       5     (max concurrent positions)
  TELEGRAM_BOT_TOKEN ...
  TELEGRAM_CHAT_ID   ...
  OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE  (for live OKX)
  BINANCE_API_KEY / BINANCE_API_SECRET            (for live Binance)
  BYBIT_API_KEY / BYBIT_API_SECRET                (for live Bybit)
  MEXC_API_KEY / MEXC_API_SECRET                  (for live MEXC — KYC required for trading perms)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import argparse
import logging
import os
import sys

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("apex_terminal.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("apex")


def parse_args():
    parser = argparse.ArgumentParser(description="APEX Terminal")
    parser.add_argument("--no-api",   action="store_true",
                         help="Console-only — don't start the dashboard/REST API (it starts by default)")
    parser.add_argument("--port",     type=int, default=8080, help="Dashboard/API server port (default 8080)")
    parser.add_argument("--mode",     choices=["paper", "live"], default=None)
    parser.add_argument("--exchange", choices=["okx", "binance", "bybit", "mexc"], default=None)
    parser.add_argument("--balance",  type=float, default=10_000.0, help="Initial paper balance")
    parser.add_argument("--scan",     action="store_true", help="One-shot scan then exit")
    parser.add_argument("--backtest", metavar="SYMBOL", default=None, help="Run backtest on symbol")
    parser.add_argument("--strategy", default="macd_ema", help="Strategy for backtest")
    parser.add_argument("--tf",       default="1H",   help="Timeframe for backtest")
    parser.add_argument("--bars",     type=int, default=500, help="Bars for backtest")
    parser.add_argument("--debug",    action="store_true", help="Verbose logging")
    parser.add_argument("--no-watchdog", action="store_true",
                         help="Disable the self-healing Watchdog supervisor (crashes kill the process)")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Override settings via CLI
    if args.mode:
        os.environ["APEX_MODE"] = args.mode
    if args.exchange:
        os.environ["APEX_EXCHANGE"] = args.exchange

    # Re-import settings after env vars are set
    import importlib
    import config.settings as cfg
    importlib.reload(cfg)

    # ── One-shot scan ─────────────────────────────────────────────────────────
    if args.scan:
        logger.info("Running one-shot market scan...")
        from data.feed    import MarketFeed
        from core.scanner import run_scan
        feed    = MarketFeed()
        results = run_scan(feed, limit=50)
        logger.info("=== SCAN RESULTS (%d signals) ===", len(results))
        for r in results:
            logger.info("%-25s %s  conf=%d%%  grade=%s  regime=%-12s  htf=%s",
                        r["symbol"], r["signal"], r["confidence"], r["grade"],
                        r["regime"], "✓" if r["htf_aligned"] else "✗")
        return

    # ── Backtest ──────────────────────────────────────────────────────────────
    if args.backtest:
        from backtest.backtester import Backtester
        from config.settings import EXCHANGE
        logger.info("Running backtest: %s  strategy=%s  tf=%s  bars=%d",
                    args.backtest, args.strategy, args.tf, args.bars)
        bt     = Backtester(EXCHANGE)
        result = bt.run(
            symbol        = args.backtest,
            strategy_name = args.strategy,
            tf            = args.tf,
            bars          = args.bars,
            initial_balance = args.balance,
        )
        logger.info("=== BACKTEST RESULTS ===")
        logger.info("Symbol      : %s", result.get("symbol"))
        logger.info("Strategy    : %s", result.get("strategy"))
        logger.info("Trades      : %d", result.get("trades", 0))
        logger.info("Win Rate    : %.1f%%", result.get("win_rate", 0))
        logger.info("Total PnL   : %.4f (%.2f%%)", result.get("total_pnl", 0), result.get("total_pnl_pct", 0))
        logger.info("Max Drawdown: %.1f%%", result.get("max_drawdown_pct", 0))
        logger.info("Final Bal   : %.2f", result.get("final_balance", 0))
        return

    # ── Trading engine ────────────────────────────────────────────────────────
    from core.engine import TradingEngine
    engine = TradingEngine(initial_balance=args.balance)

    if not args.no_api:
        from webapp_server import start_server
        start_server(engine, port=args.port)
        banner = f"  Dashboard running →  http://localhost:{args.port}  "
        logger.info("=" * len(banner))
        logger.info(banner)
        logger.info("=" * len(banner))
        logger.info("Exchange status, the Watchdog's patch queue, and connected accounts "
                    "are all there — the console log only tells part of the story.")
    else:
        logger.info("[MAIN] Running console-only (--no-api). No dashboard will be available; "
                    "check /agent/status equivalents only via the log.")

    engine.run(supervised=not args.no_watchdog)


if __name__ == "__main__":
    main()
