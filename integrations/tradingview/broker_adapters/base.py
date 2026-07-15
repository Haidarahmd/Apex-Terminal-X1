"""
APEX Broker Adapter Interface
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A "broker adapter" pulls REAL account state — name/label, balance,
equity, currency, account privileges (can it trade live? is it a
demo account? what instruments is it permissioned for?) — from a
broker's own sanctioned API, NOT from scraping TradingView's UI.

Why this design instead of reading TradingView directly:
TradingView's account/broker panel is a feature of its logged-in web
app. There's no public REST endpoint for "give me my balance" — the
only way to get that out of TradingView itself would be driving a
browser session with your TradingView login, which means storing/
replaying your session cookie outside TradingView's own auth flow.
That's a real account-security liability (anyone who can read that
session token can act as you on TradingView) and it breaks the
moment TradingView changes a DOM selector. Brokers that are popular
on TradingView's charts (Deriv, Exness, and others) instead expose
their OWN REST/WebSocket APIs with proper API-key auth — that's what
these adapters talk to. You still get "balance + privileges" in your
dashboard; it's just sourced honestly.

To add a new broker: subclass BrokerAdapter, implement get_account(),
and register it in ACCOUNT_ADAPTERS in account_state.py.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AccountState:
    broker: str
    connected: bool
    account_name: str = ""
    account_id: str = ""
    currency: str = "USD"
    balance: float = 0.0
    equity: float = 0.0
    is_demo: bool = True
    can_trade_live: bool = False
    leverage: float | None = None
    open_positions: int = 0
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "broker": self.broker, "connected": self.connected,
            "account_name": self.account_name, "account_id": self.account_id,
            "currency": self.currency, "balance": self.balance, "equity": self.equity,
            "is_demo": self.is_demo, "can_trade_live": self.can_trade_live,
            "leverage": self.leverage, "open_positions": self.open_positions,
            "error": self.error, "extra": self.extra,
        }


class BrokerAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True if the env vars/credentials this adapter needs are present."""
        ...

    @abstractmethod
    def get_account(self) -> AccountState:
        """Fetch live account state. Must never raise — catch internally and
        return AccountState(connected=False, error=...) on any failure."""
        ...
