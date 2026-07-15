"""
APEX Agent — Health Checks
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Proactive checks run on a timer (independent of whether the engine
has thrown an exception yet). Each check returns:
    {"name": str, "ok": bool, "detail": str}

Catches things like "exchange reachable but auth silently expired"
or "disk almost full" before they cause a missed trade.
"""
import logging
import shutil

from config.settings import DATA_DIR, MODE, EXCHANGE

logger = logging.getLogger(__name__)


def _check_disk_space() -> dict:
    try:
        usage = shutil.disk_usage(DATA_DIR)
        free_pct = usage.free / usage.total * 100
        ok = free_pct > 5
        return {"name": "disk_space", "ok": ok,
                "detail": f"{free_pct:.1f}% free" if ok else f"LOW DISK: {free_pct:.1f}% free"}
    except Exception as e:
        return {"name": "disk_space", "ok": False, "detail": str(e)}


def _check_feed_reachable(engine) -> dict:
    """
    BUG THIS FIXES: this used to call feed.connection_status(), which pings
    ALL FOUR configured exchanges every single time — including the three
    you deliberately aren't using. Confirmed from a real log: after
    switching to MEXC (working fine — scans completing, signals found),
    this check kept generating DNS-failure noise for OKX/Binance/Bybit
    every ~120s on its own timer, for exchanges that were never going to
    be used and were independently known-unreachable on that connection.

    Now: probe ONLY the active exchange first. Only fall through to
    checking the other three if the active one actually fails — at which
    point that information is genuinely useful (is there anything else
    reachable to switch to?), not noise.
    """
    try:
        feed = getattr(engine, "feed", None)
        if feed is None:
            return {"name": "feed_reachable", "ok": False, "detail": "no feed on engine"}

        active = feed.active_exchange()
        active_only_status = feed.connection_status(exchanges=[active])
        active_status = active_only_status.get(active, {})

        if active_status.get("ok"):
            return {"name": "feed_reachable", "ok": True, "detail": f"{active} reachable"}

        # Active exchange failed this probe — NOW it's worth checking the
        # others, since "is anything reachable at all" is actually useful
        # information once there's a real problem to react to.
        full_status = feed.connection_status()

        if active_status.get("geo_blocked") and hasattr(feed, "_blocked_exchanges"):
            feed._blocked_exchanges.add(active)
            switched = feed._try_fallback(reason="geo-blocked (detected by periodic health check)") \
                if hasattr(feed, "_try_fallback") else False
            if switched:
                return {"name": "feed_reachable", "ok": True,
                         "detail": f"{active} was geo-blocked — switched to {feed.active_exchange()}"}

        any_ok = any(v.get("ok") for v in full_status.values())
        if any_ok:
            return {"name": "feed_reachable", "ok": False,
                     "detail": f"{active} unreachable but fallback exchange(s) are up — "
                               f"consider switching APEX_EXCHANGE"}
        return {"name": "feed_reachable", "ok": False, "detail": "ALL exchanges unreachable"}
    except Exception as e:
        return {"name": "feed_reachable", "ok": False, "detail": str(e)}


def _check_live_auth(engine) -> dict:
    if MODE != "live":
        return {"name": "live_auth", "ok": True, "detail": "paper mode — n/a"}
    try:
        acc = engine.exec.get_account()
        if acc and acc.get("equity", -1) >= 0:
            return {"name": "live_auth", "ok": True, "detail": f"{EXCHANGE} account OK"}
        return {"name": "live_auth", "ok": False, "detail": "unexpected account response"}
    except Exception as e:
        return {"name": "live_auth", "ok": False, "detail": f"account fetch failed: {e}"}


def _check_journal_integrity() -> dict:
    try:
        from journal.trade_journal import TradeJournal
        j = TradeJournal()
        trades = j.load_all()
        return {"name": "journal_integrity", "ok": True, "detail": f"{len(trades)} rows readable"}
    except Exception as e:
        return {"name": "journal_integrity", "ok": False, "detail": f"journal unreadable: {e}"}


def _check_drawdown_state(engine) -> dict:
    try:
        from config.settings import MAX_DAILY_DRAWDOWN_PCT
        acc = engine.exec.get_account()
        dd = engine.dd_guard.current_dd(acc["equity"], engine.equity_tracker.baseline)
        ok = dd < MAX_DAILY_DRAWDOWN_PCT
        return {"name": "drawdown_state", "ok": ok,
                "detail": f"{dd*100:.2f}% / {MAX_DAILY_DRAWDOWN_PCT*100:.1f}% limit"}
    except Exception as e:
        return {"name": "drawdown_state", "ok": False, "detail": str(e)}


def run_all_checks(engine) -> list[dict]:
    checks = [
        _check_disk_space(),
        _check_feed_reachable(engine),
        _check_live_auth(engine),
        _check_journal_integrity(),
        _check_drawdown_state(engine),
    ]
    return checks
