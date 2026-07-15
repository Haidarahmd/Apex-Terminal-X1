"""
APEX Telegram Notifier — rich trade alerts with emoji formatting.
Silently does nothing if token/chat_id are not configured.
"""
import json
import logging
import urllib.request
from datetime import datetime, timezone

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self._token   = TELEGRAM_BOT_TOKEN
        self._chat_id = TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        if self._enabled:
            logger.info("[TG] Notifier active — chat_id=%s", self._chat_id)

    def _send(self, text: str):
        if not self._enabled:
            return
        try:
            url     = f"https://api.telegram.org/bot{self._token}/sendMessage"
            payload = json.dumps({"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}).encode()
            req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            logger.warning("[TG] Send failed: %s", e)

    def _now(self) -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def signal(self, symbol, side, confidence, price, sl, tp, strategies):
        emoji = "🟢" if side == "buy" else "🔴"
        strats = ", ".join(strategies) if isinstance(strategies, list) else strategies
        self._send(
            f"{emoji} <b>APEX SIGNAL — {side.upper()}</b>\n"
            f"Symbol    : <b>{symbol}</b>\n"
            f"Price     : {price:.6f}\n"
            f"SL        : {sl:.6f}  |  TP: {tp:.6f}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Strategies: {strats}\n"
            f"<i>{self._now()}</i>"
        )

    def trade_opened(self, symbol, side, qty, entry, sl, tp, strategy):
        emoji = "🟢" if side == "buy" else "🔴"
        self._send(
            f"{emoji} <b>TRADE OPENED — {side.upper()}</b>\n"
            f"Symbol  : <b>{symbol}</b>\n"
            f"Qty     : {qty:.6f}  @  {entry:.6f}\n"
            f"SL      : {sl:.6f}  |  TP: {tp:.6f}\n"
            f"Strategy: {strategy}\n"
            f"<i>{self._now()}</i>"
        )

    def trade_closed(self, symbol, side, pnl, reason, strategy):
        emoji = "✅" if pnl >= 0 else "❌"
        self._send(
            f"{emoji} <b>TRADE CLOSED — {symbol} {side.upper()}</b>\n"
            f"P&L      : {'+'if pnl>=0 else ''}{pnl:.4f}\n"
            f"Reason   : {reason}\n"
            f"Strategy : {strategy}\n"
            f"<i>{self._now()}</i>"
        )

    def daily_summary(self, equity, start_equity, stats, weights):
        pnl   = equity - start_equity
        emoji = "📈" if pnl >= 0 else "📉"
        w_str = "  ".join(f"{k[:4]}:{v:.0%}" for k, v in weights.items())
        self._send(
            f"{emoji} <b>Daily Summary</b>\n"
            f"Equity   : {equity:.2f} ({'+' if pnl>=0 else ''}{pnl:.2f})\n"
            f"Trades   : {stats.get('trades',0)}  |  WR: {stats.get('win_rate',0):.1f}%\n"
            f"Total PnL: {stats.get('total_pnl',0):.4f}\n"
            f"Weights  : {w_str}\n"
            f"<i>{self._now()}</i>"
        )

    def drawdown_alert(self, dd_pct, equity):
        self._send(
            f"⚠️ <b>DRAWDOWN ALERT</b>\n"
            f"Drawdown : {dd_pct:.1f}%\n"
            f"Equity   : {equity:.2f}\n"
            f"<b>Trading paused for this cycle.</b>"
        )

    def startup(self, exchange, mode, equity):
        self._send(
            f"🚀 <b>APEX Terminal Started</b>\n"
            f"Exchange : {exchange.upper()}\n"
            f"Mode     : {mode.upper()}\n"
            f"Equity   : {equity:.2f}\n"
            f"<i>{self._now()}</i>"
        )

    def error(self, message: str):
        self._send(f"🚨 <b>APEX ERROR</b>\n{message}")
