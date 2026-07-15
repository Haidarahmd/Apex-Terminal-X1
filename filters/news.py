"""
APEX News Filter — high-impact event blackout.
Tries live ForexFactory RSS feed first; falls back to static schedule.
Results cached for 60 s to avoid hammering the RSS endpoint.
"""
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

from config.settings import NEWS_BLACKOUT_MINUTES, NEWS_LIVE_FEED

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache_result:    bool  = False
_cache_ts:        float = 0.0
_CACHE_TTL:       int   = 60  # seconds

# ── Static high-impact schedule (UTC) ─────────────────────────────────────────
# Format: (weekday 0=Mon, hour_utc, tag)
_STATIC = [
    (4,  12, "NFP"),    # First Friday
    (4,  13, "NFP"),
    (2,  18, "FOMC"),   # Wednesday
    (2,  19, "FOMC"),
    (1,  12, "CPI"),    # Tuesday
    (2,  12, "CPI"),
    (3,  12, "ECB"),    # Thursday
    (3,  13, "ECB"),
    (3,  11, "BOE"),    # Thursday
    (2,  14, "EIA"),    # Wednesday — crude inventory
    (3,  14, "EIA"),
]


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def is_news_blackout(symbol: str | None = None) -> bool:
    global _cache_result, _cache_ts
    now_mono = time.monotonic()
    if now_mono - _cache_ts < _CACHE_TTL:
        return _cache_result

    result = False
    if NEWS_LIVE_FEED:
        try:
            result = _live_check()
        except Exception as exc:
            logger.debug("[NEWS] Live feed failed (%s) — using static", exc)
            result = _static_check()
    else:
        result = _static_check()

    _cache_result = result
    _cache_ts     = now_mono
    return result


def _static_check() -> bool:
    now = _now()
    wd  = now.weekday()
    cur = now.hour * 60 + now.minute
    for (ev_wd, ev_h, tag) in _STATIC:
        if wd != ev_wd:
            continue
        ev_min = ev_h * 60
        if abs(cur - ev_min) <= NEWS_BLACKOUT_MINUTES:
            logger.info("[NEWS] Static blackout: %s within %d min", tag, NEWS_BLACKOUT_MINUTES)
            return True
    return False


def _live_check() -> bool:
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    req = urllib.request.Request(url, headers={"User-Agent": "APEX/1.0"})
    with urllib.request.urlopen(req, timeout=5) as r:
        data = r.read()
    root   = ET.fromstring(data)
    now    = _now()
    window = timedelta(minutes=NEWS_BLACKOUT_MINUTES)
    for event in root.findall("event"):
        if event.findtext("impact", "").lower() != "high":
            continue
        date_s = event.findtext("date", "")
        time_s = event.findtext("time", "")
        if not date_s or not time_s:
            continue
        try:
            dt = datetime.strptime(f"{date_s} {time_s}", "%b %d, %Y %I:%M%p").replace(tzinfo=timezone.utc)
            if abs(now - dt) <= window:
                logger.info("[NEWS] Live blackout: %s at %s", event.findtext("title", "?"), dt)
                return True
        except ValueError:
            continue
    return False
