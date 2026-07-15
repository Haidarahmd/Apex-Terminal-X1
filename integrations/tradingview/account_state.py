"""
APEX Account State Aggregator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Combines every configured broker adapter into one response for the
dashboard's "Connected Accounts" panel: name, balance, currency,
demo/real flag, and trading privileges — per account, pulled fresh
each call.

This is intentionally a pull (not push/cached) aggregator — account
state should always reflect the current real balance, not a stale
cache, since the whole point is to know what you can actually trade
with right now.
"""
import logging

from integrations.tradingview.broker_adapters.crypto_exchange import CryptoExchangeAdapter
from integrations.tradingview.broker_adapters.deriv import DerivAdapter
from integrations.tradingview.broker_adapters.mt5_bridge import MT5BridgeAdapter

logger = logging.getLogger(__name__)

ACCOUNT_ADAPTERS = [
    CryptoExchangeAdapter(),
    DerivAdapter(),
    MT5BridgeAdapter(),
]


def get_connected_account_state() -> dict:
    """
    Returns {"accounts": [...], "configured_count": N, "connected_count": N}
    Each account adapter that ISN'T configured is still listed (connected=False,
    with a helpful `error` explaining what env var to set) so the dashboard can
    show "not connected — click to set up" rather than silently omitting it.
    """
    accounts = []
    for adapter in ACCOUNT_ADAPTERS:
        try:
            state = adapter.get_account()
            accounts.append(state.to_dict())
        except Exception as e:
            logger.error("[ACCOUNT-STATE] adapter %s crashed: %s", adapter.name, e)
            accounts.append({"broker": adapter.name, "connected": False, "error": str(e)})

    return {
        "accounts": accounts,
        "configured_count": sum(1 for a in ACCOUNT_ADAPTERS if a.is_configured()),
        "connected_count": sum(1 for a in accounts if a.get("connected")),
    }
