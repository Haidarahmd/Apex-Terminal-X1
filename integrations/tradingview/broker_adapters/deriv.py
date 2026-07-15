"""
APEX Broker Adapter — Deriv
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Deriv is one of the brokers commonly charted via TradingView that
ALSO exposes its own real account API — so we get genuine account
state (balance, currency, demo/real flag, login id) honestly, via
Deriv's documented WebSocket protocol, instead of trying to scrape
anything from TradingView's UI.

Setup:
  1. Create a Deriv account (deriv.com) — available to Nigerian users.
  2. Get an API token: Deriv app → Settings → API token
     (https://app.deriv.com/account/api-token)
     Scope needed: "Read" is enough for balance/account info.
  3. Register an app_id at https://api.deriv.com (free, instant) — or
     use the public test app_id 1089 for personal/non-commercial use.
  4. Set env vars:
       DERIV_API_TOKEN=your_token
       DERIV_APP_ID=your_app_id        (optional, defaults to 1089)

This adapter is READ-ONLY by design (authorize + balance + account
list). It does not place trades on Deriv — APEX's own execution
layer (execution/) is for your crypto exchange accounts. Use this
purely to surface Deriv account state in the dashboard alongside
your crypto positions.
"""
import json
import logging
import os
import ssl
import time

from integrations.tradingview.broker_adapters.base import AccountState, BrokerAdapter

logger = logging.getLogger(__name__)

_APP_ID = os.getenv("DERIV_APP_ID", "1089")
_TOKEN = os.getenv("DERIV_API_TOKEN", "")
_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={_APP_ID}"


class DerivAdapter(BrokerAdapter):
    name = "deriv"

    def is_configured(self) -> bool:
        return bool(_TOKEN)

    def get_account(self) -> AccountState:
        if not _TOKEN:
            return AccountState(broker="deriv", connected=False,
                                 error="DERIV_API_TOKEN not set")
        try:
            return self._fetch_via_ws()
        except Exception as e:
            logger.error("[DERIV] account fetch failed: %s", e)
            return AccountState(broker="deriv", connected=False, error=str(e))

    def _fetch_via_ws(self) -> AccountState:
        # Using the `websocket-client` sync library kept optional — if it's
        # not installed we fail clearly rather than crashing the dashboard.
        try:
            import websocket  # websocket-client package
        except ImportError:
            return AccountState(
                broker="deriv", connected=False,
                error="Install 'websocket-client' (pip install websocket-client) to enable the Deriv adapter",
            )

        ws = websocket.create_connection(_WS_URL, timeout=10,
                                          sslopt={"cert_reqs": ssl.CERT_REQUIRED})
        try:
            ws.send(json.dumps({"authorize": _TOKEN}))
            auth_resp = json.loads(ws.recv())
            if auth_resp.get("error"):
                return AccountState(broker="deriv", connected=False,
                                     error=auth_resp["error"].get("message", "authorize failed"))

            auth = auth_resp.get("authorize", {})

            ws.send(json.dumps({"balance": 1, "account": "current"}))
            bal_resp = json.loads(ws.recv())
            balance_info = bal_resp.get("balance", {})

            is_virtual = bool(auth.get("is_virtual", 0))
            return AccountState(
                broker="deriv",
                connected=True,
                account_name=auth.get("fullname", "") or auth.get("email", ""),
                account_id=auth.get("loginid", ""),
                currency=auth.get("currency", "USD"),
                balance=float(balance_info.get("balance", auth.get("balance", 0))),
                equity=float(balance_info.get("balance", auth.get("balance", 0))),  # Deriv has no separate margin-equity for non-MT5 accounts
                is_demo=is_virtual,
                can_trade_live=not is_virtual,
                leverage=None,
                open_positions=0,  # would require portfolio call; left out of this read-only summary
                extra={
                    "landing_company": auth.get("landing_company_name", ""),
                    "scopes": auth.get("scopes", []),
                    "country": auth.get("country", ""),
                },
            )
        finally:
            try:
                ws.close()
            except Exception:
                pass
