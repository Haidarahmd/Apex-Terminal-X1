"""
APEX Broker Adapter — Crypto Exchange (reuses execution/live_executor.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Not a new integration — this just wraps APEX's existing OKX/Binance/
Bybit/MEXC live executor so it shows up in the same "Connected Accounts"
panel as Deriv/MT5, giving you one unified account-state view instead
of two separate ones.
"""
import logging

from config.settings import EXCHANGE, MODE
from integrations.tradingview.broker_adapters.base import AccountState, BrokerAdapter

logger = logging.getLogger(__name__)


class CryptoExchangeAdapter(BrokerAdapter):
    name = "crypto_exchange"

    def is_configured(self) -> bool:
        return MODE == "live"

    def get_account(self) -> AccountState:
        if MODE != "paper" and MODE != "live":
            return AccountState(broker=EXCHANGE, connected=False, error="unknown mode")
        try:
            from execution.live_executor import LiveExecutor
            if MODE != "live":
                return AccountState(broker=EXCHANGE, connected=False,
                                     error="running in paper mode — no live exchange account to show",
                                     is_demo=True)
            ex = LiveExecutor(EXCHANGE)
            acc = ex.get_account()
            return AccountState(
                broker=EXCHANGE, connected=True,
                account_name=f"{EXCHANGE.upper()} account",
                currency="USDT", balance=float(acc.get("balance", 0)),
                equity=float(acc.get("equity", 0)), is_demo=False,
                can_trade_live=True,
            )
        except Exception as e:
            logger.error("[CRYPTO-ADAPTER] %s", e)
            return AccountState(broker=EXCHANGE, connected=False, error=str(e))
