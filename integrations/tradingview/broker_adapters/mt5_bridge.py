"""
APEX Broker Adapter — MT5 Bridge (covers Exness and most retail forex/CFD brokers)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Honesty check first: Exness (and most brokers popular on TradingView
charts in Nigeria — Exness, HFM, FXTM, etc.) do NOT expose a public
REST API for a personal trading account's balance. The only
"Exness API" that's publicly documented is the Partnership/affiliate
API (for IB commissions), which is a different product and won't
give you YOUR trading account's balance. Your real trading account
on these brokers lives on an MT4/MT5 server.

So getting genuine balance/equity/positions out of Exness (or any
MT5 broker) means talking to MT5 itself. There's no way around that
without scraping a logged-in web session, which we won't build (see
broker_adapters/base.py for why).

This adapter therefore expects ONE of:
  (a) the official MetaTrader5 Python package, running on Windows
      (or Wine on Linux) with a real terminal logged into your
      Exness/MT5 account — `pip install MetaTrader5`
  (b) a small bridge service of your own (e.g. an MT5 Expert Advisor
      that writes account state to a local JSON file or local HTTP
      endpoint, which this adapter reads) — useful if APEX itself
      runs on a Linux VPS without a Windows MT5 terminal available.

This is opt-in and OFF by default — APEX core has zero MT5
dependency, exactly as the original README states, and this stays
true unless you explicitly configure one of the two paths above.

Env vars:
  MT5_BRIDGE_MODE   = "native" | "http" | "" (disabled, default)
  MT5_LOGIN / MT5_PASSWORD / MT5_SERVER   (for native mode)
  MT5_BRIDGE_URL    (for http mode — your own bridge endpoint, e.g.
                      http://127.0.0.1:9100/account, returning
                      {"name":..,"login":..,"balance":..,"equity":..,
                       "currency":..,"leverage":..,"is_demo":bool})
"""
import logging
import os

from integrations.tradingview.broker_adapters.base import AccountState, BrokerAdapter

logger = logging.getLogger(__name__)

_MODE = os.getenv("MT5_BRIDGE_MODE", "").lower()


class MT5BridgeAdapter(BrokerAdapter):
    name = "mt5_bridge"

    def is_configured(self) -> bool:
        return _MODE in ("native", "http")

    def get_account(self) -> AccountState:
        if _MODE == "native":
            return self._fetch_native()
        if _MODE == "http":
            return self._fetch_http()
        return AccountState(
            broker="mt5_bridge", connected=False,
            error=("Not configured. Exness/MT5 brokers have no public balance API — "
                   "set MT5_BRIDGE_MODE=native (requires Windows + MetaTrader5 package) "
                   "or MT5_BRIDGE_MODE=http (point at your own bridge). See module "
                   "docstring for details."),
        )

    def _fetch_native(self) -> AccountState:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            return AccountState(
                broker="mt5_bridge", connected=False,
                error="pip install MetaTrader5 (Windows only — MT5 has no official Linux/Mac SDK)",
            )
        try:
            login = int(os.getenv("MT5_LOGIN", "0"))
            password = os.getenv("MT5_PASSWORD", "")
            server = os.getenv("MT5_SERVER", "")
            if not mt5.initialize(login=login, password=password, server=server):
                return AccountState(broker="mt5_bridge", connected=False,
                                     error=f"MT5 initialize failed: {mt5.last_error()}")
            info = mt5.account_info()
            mt5.shutdown()
            if info is None:
                return AccountState(broker="mt5_bridge", connected=False,
                                     error="account_info() returned None")
            return AccountState(
                broker=f"mt5_bridge ({server or 'mt5'})", connected=True,
                account_name=info.name, account_id=str(info.login),
                currency=info.currency, balance=float(info.balance),
                equity=float(info.equity), is_demo=(info.trade_mode == 0),
                can_trade_live=(info.trade_mode != 0), leverage=float(info.leverage),
                extra={"server": info.server, "margin_free": info.margin_free},
            )
        except Exception as e:
            return AccountState(broker="mt5_bridge", connected=False, error=str(e))

    def _fetch_http(self) -> AccountState:
        import json
        import urllib.request
        url = os.getenv("MT5_BRIDGE_URL", "")
        if not url:
            return AccountState(broker="mt5_bridge", connected=False,
                                 error="MT5_BRIDGE_URL not set")
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.loads(r.read())
            return AccountState(
                broker="mt5_bridge (custom)", connected=True,
                account_name=d.get("name", ""), account_id=str(d.get("login", "")),
                currency=d.get("currency", "USD"), balance=float(d.get("balance", 0)),
                equity=float(d.get("equity", d.get("balance", 0))),
                is_demo=bool(d.get("is_demo", True)),
                can_trade_live=not bool(d.get("is_demo", True)),
                leverage=d.get("leverage"), extra=d.get("extra", {}),
            )
        except Exception as e:
            return AccountState(broker="mt5_bridge (custom)", connected=False, error=str(e))
