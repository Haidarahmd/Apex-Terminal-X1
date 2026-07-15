"""
APEX Agent — Diagnostics & Known-Issue Knowledge Base
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Classifies exceptions/log lines into known categories and maps each
category to a SAFE auto-fix action. "Safe" means: it changes runtime
behaviour (retry, backoff, switch exchange, disable a strategy,
clear a corrupt cache file, restart a subsystem) — it never rewrites
trading/order-execution source code without human sign-off.

This is the brain the Watchdog (agent/watchdog.py) consults every
time something goes wrong. Anything not recognised here is escalated
to the PatchManager as a "needs review" item instead of being
silently auto-fixed.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    INFO     = "info"       # cosmetic / no action needed
    WARN     = "warn"       # auto-fixed, continue
    CRITICAL = "critical"   # trading halted until resolved
    FATAL    = "fatal"      # process restart required


class Category(str, Enum):
    NETWORK_DNS       = "network_dns"
    NETWORK_TIMEOUT   = "network_timeout"
    EXCHANGE_AUTH     = "exchange_auth"
    EXCHANGE_GEO_BLOCK= "exchange_geo_block"
    EXCHANGE_RATE_LIM = "exchange_rate_limit"
    EXCHANGE_REJECT   = "exchange_order_rejected"
    DATA_GAP          = "data_gap"
    DATA_CORRUPT      = "data_corrupt"
    CODE_BUG          = "code_bug"
    CONFIG_ERROR      = "config_error"
    DRAWDOWN          = "drawdown"
    UNKNOWN           = "unknown"


@dataclass
class Diagnosis:
    category: Category
    severity: Severity
    message: str
    auto_fixable: bool
    fix_action: str | None = None        # key into watchdog's ACTIONS table
    fix_args: dict = field(default_factory=dict)
    raw: str = ""
    ts: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "auto_fixable": self.auto_fixable,
            "fix_action": self.fix_action,
            "fix_args": self.fix_args,
            "ts": self.ts,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Pattern → Diagnosis rules
#  Order matters — first match wins. Keep specific patterns above generic ones.
# ══════════════════════════════════════════════════════════════════════════════
_RULES: list[tuple[re.Pattern, callable]] = []


def _rule(pattern: str):
    def deco(fn):
        _RULES.append((re.compile(pattern, re.IGNORECASE), fn))
        return fn
    return deco


@_rule(r"getaddrinfo failed|name or service not known|11001|11002|nodename nor servname")
def _dns_block(exc_text, raw):
    return Diagnosis(
        category=Category.NETWORK_DNS, severity=Severity.WARN,
        message="DNS resolution failed — likely ISP-level DNS filtering of the exchange domain.",
        auto_fixable=True, fix_action="enable_doh_and_retry", raw=raw,
    )


@_rule(r"timed out|timeout|ETIMEDOUT|read timed out")
def _net_timeout(exc_text, raw):
    return Diagnosis(
        category=Category.NETWORK_TIMEOUT, severity=Severity.WARN,
        message="Network request timed out.",
        auto_fixable=True, fix_action="backoff_and_retry", raw=raw,
    )


@_rule(r"maintenance window|system busy.*try again|in maintenance")
def _exchange_maintenance(exc_text, raw):
    return Diagnosis(
        category=Category.EXCHANGE_RATE_LIM, severity=Severity.WARN,
        message="Exchange is in a scheduled maintenance window — temporary, not an account/code issue.",
        auto_fixable=True, fix_action="backoff_and_retry", fix_args={"multiplier": 10}, raw=raw,
    )


@_rule(r"\b451\b|unavailable for legal reasons|\b403\b|forbidden")
def _geo_block(exc_text, raw):
    return Diagnosis(
        category=Category.EXCHANGE_GEO_BLOCK, severity=Severity.WARN,
        message=("Exchange returned 451/403 — this is the exchange deliberately refusing "
                  "your IP (geo-blocking), not a transient error or a DNS problem. "
                  "DoH bypass will not fix this since DNS already resolved correctly."),
        auto_fixable=True, fix_action="switch_exchange_on_geo_block", raw=raw,
    )


@_rule(r"401|invalid signature|invalid api[- ]?key|unauthorized|incorrect apikey|api-key not found")
def _auth_error(exc_text, raw):
    return Diagnosis(
        category=Category.EXCHANGE_AUTH, severity=Severity.CRITICAL,
        message="Exchange rejected API credentials (invalid key/secret/passphrase, or IP not whitelisted).",
        auto_fixable=False, fix_action="halt_live_trading", raw=raw,
    )


@_rule(r"429|too many requests|rate limit")
def _rate_limit(exc_text, raw):
    return Diagnosis(
        category=Category.EXCHANGE_RATE_LIM, severity=Severity.WARN,
        message="Exchange rate limit hit.",
        auto_fixable=True, fix_action="backoff_and_retry", fix_args={"multiplier": 3}, raw=raw,
    )


@_rule(r"insufficient (margin|balance|funds)|notional too small|min notional|order would (immediately )?trigger")
def _order_rejected(exc_text, raw):
    return Diagnosis(
        category=Category.EXCHANGE_REJECT, severity=Severity.WARN,
        message="Exchange rejected the order (sizing/margin/notional issue).",
        auto_fixable=True, fix_action="skip_symbol_this_cycle", raw=raw,
    )


@_rule(r"empty dataframe|no candles|0 rows|insufficient (data|bars|history)")
def _data_gap(exc_text, raw):
    return Diagnosis(
        category=Category.DATA_GAP, severity=Severity.INFO,
        message="Not enough candle history returned for this symbol/timeframe.",
        auto_fixable=True, fix_action="skip_symbol_this_cycle", raw=raw,
    )


@_rule(r"json\.decoder|expecting value|unterminated string|corrupt")
def _data_corrupt(exc_text, raw):
    return Diagnosis(
        category=Category.DATA_CORRUPT, severity=Severity.WARN,
        message="A local JSON/cache file appears corrupted.",
        auto_fixable=True, fix_action="quarantine_and_reset_file", raw=raw,
    )


@_rule(r"keyerror|attributeerror|typeerror|indexerror|valueerror|nameerror|zerodivisionerror")
def _code_bug(exc_text, raw):
    return Diagnosis(
        category=Category.CODE_BUG, severity=Severity.CRITICAL,
        message=f"Likely code-level bug: {exc_text.splitlines()[-1] if exc_text else 'unknown'}",
        auto_fixable=False, fix_action="queue_patch_review", raw=raw,
    )


@_rule(r"drawdown")
def _drawdown(exc_text, raw):
    return Diagnosis(
        category=Category.DRAWDOWN, severity=Severity.CRITICAL,
        message="Daily drawdown limit breached — trading halted by design (not a bug).",
        auto_fixable=False, fix_action=None, raw=raw,
    )


def diagnose(exc: BaseException | None = None, raw_text: str | None = None) -> Diagnosis:
    """
    Classify an exception (or a raw log line) into a Diagnosis.
    Pass either `exc` (a caught exception) or `raw_text` (a log/error string).
    """
    if exc is not None:
        text = "".join(traceback.format_exception_only(type(exc), exc))
        full = text + "\n" + "".join(traceback.format_tb(exc.__traceback__))
    else:
        text = raw_text or ""
        full = text

    for pattern, builder in _RULES:
        if pattern.search(full):
            return builder(text, full)

    return Diagnosis(
        category=Category.UNKNOWN, severity=Severity.CRITICAL,
        message=f"Unrecognised error — needs human review: {text.strip()[:200] if text else 'no detail'}",
        auto_fixable=False, fix_action="queue_patch_review", raw=full,
    )
