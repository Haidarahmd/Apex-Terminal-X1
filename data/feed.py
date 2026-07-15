"""
APEX Data Feed — broker-agnostic market data via public REST APIs.
Supports: OKX, Binance USDT-M Futures, Bybit USDT Perpetual.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NETWORK HARDENING (v1.1)
  • Retry with exponential back-off (3 attempts)
  • DNS-over-HTTPS bypass (APEX_USE_DOH=1 or auto-detected)
  • IP cache from check_network.py loaded at startup
  • Multi-exchange fallback: OKX → Binance → Bybit
  • Windows DNS / getaddrinfo error auto-detection
  • Optional HTTP proxy via HTTPS_PROXY env var
  • SSL verification toggle via APEX_VERIFY_SSL=0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from config.settings import (
    EXCHANGE, LTF, HTF, SCALP_TF, LTF_BARS, HTF_BARS, SCALP_BARS,
    MAX_SYMBOLS_SCAN, MIN_VOLUME_USD, ROOT,
)

logger = logging.getLogger(__name__)

# ── Network settings ──────────────────────────────────────────────────────────
_TIMEOUT     = int(os.getenv("APEX_TIMEOUT",     "12"))
_RETRIES     = int(os.getenv("APEX_RETRIES",      "3"))
_RETRY_DELAY = float(os.getenv("APEX_RETRY_DELAY", "2"))
_VERIFY_SSL  = os.getenv("APEX_VERIFY_SSL", "1") != "0"
_PROXY       = os.getenv("HTTPS_PROXY", os.getenv("HTTP_PROXY", ""))
_USE_DOH     = os.getenv("APEX_USE_DOH", "0") != "0"   # DNS-over-HTTPS bypass

# SSL context
_SSL_CTX = ssl.create_default_context()
if not _VERIFY_SSL:
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode    = ssl.CERT_NONE
    logger.warning("[FEED] SSL verification disabled")

# ── Exchange endpoints ────────────────────────────────────────────────────────
_ENDPOINTS = {
    "okx": {
        "host":    "www.okx.com",
        "tickers": "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
        "candles": "https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}",
    },
    "binance": {
        "host":    "fapi.binance.com",
        "tickers": "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "candles": "https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={bar}&limit={limit}",
    },
    "bybit": {
        "host":    "api.bybit.com",
        "tickers": "https://api.bybit.com/v5/market/tickers?category=linear",
        "candles": "https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={bar}&limit={limit}",
    },
    "mexc": {
        # Public market data needs no auth — same base domain as the
        # authenticated futures API (api.mexc.com), confirmed current as of
        # the June 2026 docs (older third-party guides reference the
        # legacy contract.mexc.com domain, which still works but is not
        # the documented current path).
        "host":    "api.mexc.com",
        "tickers": "https://api.mexc.com/api/v1/contract/ticker",
        "candles": "https://api.mexc.com/api/v1/contract/kline/{symbol}?interval={bar}",
    },
}

_FALLBACK_ORDER = {
    "okx":     ["binance", "bybit", "mexc"],
    "binance": ["okx",     "bybit", "mexc"],
    "bybit":   ["binance", "okx",   "mexc"],
    "mexc":    ["binance", "bybit", "okx"],
}

_OKX_BAR     = {"1H":"1H",   "4H":"4H",  "15m":"15m", "1m":"1m",  "5m":"5m",  "1D":"1D"}
_BINANCE_BAR = {"1H":"1h",   "4H":"4h",  "15m":"15m", "1m":"1m",  "5m":"5m",  "1D":"1d"}
_BYBIT_BAR   = {"1H":"60",   "4H":"240", "15m":"15",  "1m":"1",   "5m":"5",   "1D":"D"}
_MEXC_BAR    = {"1H":"Min60","4H":"Hour4","15m":"Min15","1m":"Min1","5m":"Min5","1D":"Day1"}


# ══════════════════════════════════════════════════════════════════════════════
#  DNS-over-HTTPS resolver
# ══════════════════════════════════════════════════════════════════════════════
_DOH_CACHE: dict[str, str] = {}   # hostname → IP, populated at startup

_DOH_PROVIDERS = [
    "https://dns.google/resolve?name={}&type=A",
    "https://cloudflare-dns.com/dns-query?name={}&type=A",
]

def _load_ip_cache():
    """Load pre-resolved IPs from check_network.py output."""
    cache_file = ROOT / "data_store" / "dns_cache.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                data = json.load(f)
            _DOH_CACHE.update(data)
            logger.info("[FEED] Loaded DNS IP cache: %d entries", len(data))
        except Exception as e:
            logger.warning("[FEED] Could not load DNS cache: %s", e)


def _resolve_doh(hostname: str) -> str | None:
    """Resolve hostname via DNS-over-HTTPS, bypassing ISP DNS."""
    if hostname in _DOH_CACHE:
        return _DOH_CACHE[hostname]

    ctx = ssl.create_default_context()
    for template in _DOH_PROVIDERS:
        url = template.format(hostname)
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/dns-json", "User-Agent": "APEX/1.1"}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            for ans in data.get("Answer", []):
                if ans.get("type") == 1:   # A record
                    ip = ans["data"]
                    _DOH_CACHE[hostname] = ip
                    logger.debug("[FEED] DoH resolved %s → %s", hostname, ip)
                    return ip
        except Exception:
            continue
    return None


def _build_opener(use_ip_for: str | None = None, hostname: str | None = None) -> urllib.request.OpenerDirector:
    """Build urllib opener, optionally routing to a specific IP."""
    handlers = []
    if _PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}))

    # If we have an IP override we need a custom SSL context that skips hostname check
    if use_ip_for:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        if not _VERIFY_SSL:
            ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    else:
        handlers.append(urllib.request.HTTPSHandler(context=_SSL_CTX))

    return urllib.request.build_opener(*handlers)


# ══════════════════════════════════════════════════════════════════════════════
#  Core HTTP GET with retry + DoH fallback
# ══════════════════════════════════════════════════════════════════════════════
def _get(url: str, timeout: int = _TIMEOUT, retries: int = _RETRIES,
         hostname: str | None = None) -> dict | list:
    """
    GET JSON from url.
    - Retries on network errors with exponential back-off
    - If DNS fails and USE_DOH is set, resolves via DoH and retries with IP
    - hostname: override for Host header (when using IP directly)
    """
    last_err  = None
    _tried_doh = False

    def _attempt(attempt_url: str, host_hdr: str | None, ip_mode: bool):
        opener = _build_opener(use_ip_for=host_hdr, hostname=host_hdr)
        headers = {"User-Agent": "APEX/1.1"}
        if host_hdr:
            headers["Host"] = host_hdr
        req = urllib.request.Request(attempt_url, headers=headers)
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())

    for attempt in range(1, retries + 1):
        try:
            return _attempt(url, hostname, bool(hostname))

        except urllib.error.URLError as e:
            last_err = e
            reason   = str(e.reason) if hasattr(e, "reason") else str(e)
            is_dns   = any(x in reason for x in ("getaddrinfo", "Name or service", "11001", "11002"))

            if is_dns and not _tried_doh:
                # Extract hostname from URL and try DoH resolution
                _tried_doh = True
                host = url.split("/")[2].split(":")[0]
                ip   = _resolve_doh(host)
                if ip:
                    ip_url = url.replace(f"https://{host}", f"https://{ip}", 1)
                    logger.info("[FEED] ISP DNS blocked — using DoH IP %s for %s", ip, host)
                    try:
                        return _attempt(ip_url, host, True)
                    except Exception as e2:
                        logger.warning("[FEED] DoH IP attempt failed: %s", e2)
                        last_err = e2
                else:
                    logger.error(
                        "[FEED] DNS blocked by ISP and DoH resolution also failed.\n"
                        "  ► Run:  set APEX_USE_DOH=1  then restart\n"
                        "  ► Or:   python check_network.py --fix-dns\n"
                        "  ► Or:   change Windows DNS to 1.1.1.1 / 8.8.8.8\n"
                        "  ► Or:   use a VPN"
                    )
            elif is_dns:
                logger.warning("[FEED] DNS error on attempt %d/%d: %s", attempt, retries, e)
            else:
                logger.warning("[FEED] HTTP error attempt %d/%d: %s", attempt, retries, e)

            if attempt < retries:
                delay = _RETRY_DELAY * (2 ** (attempt - 1))
                time.sleep(delay)

        except Exception as e:
            last_err = e
            logger.warning("[FEED] Error attempt %d/%d: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(_RETRY_DELAY)

    raise last_err or RuntimeError(f"Failed: {url}")


# ── Load IP cache at import time ──────────────────────────────────────────────
_load_ip_cache()

# If DoH mode, pre-resolve all exchange hosts at startup
if _USE_DOH:
    logger.info("[FEED] DoH mode active — pre-resolving exchange hostnames…")
    for _ex, _ep in _ENDPOINTS.items():
        _h = _ep["host"]
        if _h not in _DOH_CACHE:
            _ip = _resolve_doh(_h)
            if _ip:
                logger.info("[FEED] DoH: %s → %s", _h, _ip)
            else:
                logger.warning("[FEED] DoH: could not resolve %s", _h)


# ── Candle parsers ────────────────────────────────────────────────────────────
def _parse_candles_okx(raw: list) -> pd.DataFrame:
    rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]
    rows.sort(key=lambda x: x[0])
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("datetime").drop(columns=["ts"])

def _parse_candles_binance(raw: list) -> pd.DataFrame:
    rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("datetime").drop(columns=["ts"])

def _parse_candles_bybit(raw: list) -> pd.DataFrame:
    rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]
    rows.sort(key=lambda x: x[0])
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("datetime").drop(columns=["ts"])

def _parse_candles_mexc(raw: dict) -> pd.DataFrame:
    # MEXC's kline response is column-oriented (parallel arrays), not
    # row-oriented like the other three exchanges — confirmed against the
    # current API docs: {"time": [...], "open": [...], "close": [...], ...}
    # and `time` is in SECONDS, not milliseconds.
    times = raw.get("time", [])
    if not times:
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts":     times,
        "open":   raw.get("open", []),
        "high":   raw.get("high", []),
        "low":    raw.get("low", []),
        "close":  raw.get("close", []),
        "volume": raw.get("vol", []),
    })
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    df["datetime"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df.sort_values("datetime").set_index("datetime").drop(columns=["ts"])


# ── Symbol normalisation ──────────────────────────────────────────────────────
def _to_binance(s: str) -> str:
    return s.replace("-USDT-SWAP","USDT").replace("-USDT","USDT").replace("-","")

def _to_bybit(s: str) -> str:
    return _to_binance(s)

def _to_okx(s: str) -> str:
    if s.endswith("USDT") and "-" not in s:
        return f"{s[:-4]}-USDT-SWAP"
    return s

def _to_mexc(s: str) -> str:
    # MEXC contract symbols use underscores: "BTC_USDT" (vs OKX's
    # "BTC-USDT-SWAP" or Binance/Bybit's bare "BTCUSDT").
    if "_" in s:
        return s
    base = _to_binance(s)  # normalise first (strips dashes/SWAP suffix)
    if base.endswith("USDT"):
        return f"{base[:-4]}_USDT"
    return base


# ══════════════════════════════════════════════════════════════════════════════
class MarketFeed:
    """
    Unified market data — OKX / Binance / Bybit.
    DNS-bypass, retry, and automatic exchange fallback built-in.
    """

    def __init__(self, exchange: str = EXCHANGE):
        self.exchange  = exchange.lower()
        self._active   = self.exchange
        self._ep       = _ENDPOINTS[self._active]
        self._failures = 0
        self._max_fail = 3
        self._candle_failures = 0   # separate counter — see _fetch_candles note below
        self._rate_limit_hits: list = []   # timestamps of recent 429s, for adaptive throttling
        self._blocked_exchanges: set = set()  # exchanges that returned a hard geo-block (451/403)
        logger.info("[FEED] Exchange: %s%s",
                    self._active.upper(),
                    " [DoH bypass ON]" if _USE_DOH else "")

    def _bar_str(self, tf: str, exchange: str | None = None) -> str:
        m = {"okx": _OKX_BAR, "binance": _BINANCE_BAR, "bybit": _BYBIT_BAR, "mexc": _MEXC_BAR}
        return m.get(exchange or self._active, _OKX_BAR).get(tf, tf)

    def _adapt_symbol(self, symbol: str, target: str) -> str:
        if target == "okx":  return _to_okx(symbol)
        if target == "mexc": return _to_mexc(symbol)
        return _to_binance(symbol)

    def _try_fallback(self, reason: str = ""):
        """
        Advances to the NEXT exchange in a fixed, full rotation —
        [okx, binance, bybit, mexc] in that order, wrapping around —
        rather than re-deriving "the next one" from a per-exchange
        preference list every time.

        BUG THIS FIXES: the previous design picked the next exchange by
        scanning _FALLBACK_ORDER[<some exchange>] for "the first entry
        that isn't the current one", called fresh on every failure. Two
        problems with that: (1) it was keyed off self.exchange (the
        exchange fixed at construction, never updated) instead of
        self._active, so it kept re-scanning the SAME starting list no
        matter how many hops had already happened; (2) even after fixing
        that, transient failures (DNS errors, TLS handshake failures —
        which is what most real ISP/network blocking actually looks like,
        NOT a clean 451/403) were never recorded anywhere, so "the first
        non-current entry" kept flip-flopping between whichever two
        exchanges happened to alternate, and a 31-cycle real run never
        once reached mexc, the last exchange in the chain.

        Fixed by walking a single fixed rotation order via an index that
        always advances, skipping only exchanges confirmed hard-blocked
        (451/403) — NOT skipping on transient failures, since those should
        still get their turn in the rotation, just possibly fail again
        and advance further next time.
        """
        chain = ["okx", "binance", "bybit", "mexc"]
        if self._active not in chain:
            chain = [self._active] + [c for c in chain if c != self._active]
        start_idx = chain.index(self._active)

        for step in range(1, len(chain) + 1):
            candidate = chain[(start_idx + step) % len(chain)]
            if candidate in self._blocked_exchanges:
                continue
            if candidate == self._active:
                continue  # full loop back to where we started; nothing else available
            logger.warning("[FEED] %s failed (%s) — switching to %s (set APEX_EXCHANGE=%s to make permanent)",
                           self._active.upper(), reason or "repeated failures", candidate.upper(), candidate)
            self._active   = candidate
            self._ep       = _ENDPOINTS[candidate]
            self._failures = 0
            self._candle_failures = 0
            return True

        logger.error("[FEED] All exchanges failed or geo-blocked. "
                      "Check /network in the dashboard — if every exchange shows a 403/451, "
                      "this isn't a DNS issue and a VPN/different network is the only path around it.")
        return False

    @staticmethod
    def _is_hard_block(exc: Exception) -> bool:
        """451 (Unavailable for Legal Reasons) / 403 (Forbidden) mean the exchange's
        server deliberately refused to serve this IP — almost always exchange-side
        geofencing, not a transient issue. Retrying the SAME exchange won't help;
        falling back to a different one immediately (rather than waiting out the
        normal failure-count threshold) is the correct response."""
        msg = str(exc)
        return "451" in msg or "403" in msg or "Forbidden" in msg or "Legal Reasons" in msg

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        msg = str(exc)
        return "429" in msg or "Too Many Requests" in msg

    # ── Tickers ───────────────────────────────────────────────────────────────
    def get_usdt_perp_tickers(self) -> list[dict]:
        try:
            result = self._fetch_tickers(self._active)
            self._failures = 0
            return result
        except Exception as e:
            if self._is_hard_block(e):
                self._blocked_exchanges.add(self._active)
                logger.error("[FEED] %s returned a geo-block (%s) on tickers — "
                             "this is the exchange refusing your IP, not a DNS issue.",
                             self._active.upper(), e)
                if self._try_fallback(reason="geo-blocked"):
                    return self.get_usdt_perp_tickers()  # one retry on the new exchange
                return []
            self._failures += 1
            logger.error("[FEED] Ticker fetch failed (%s #%d): %s",
                         self._active.upper(), self._failures, e)
            if self._failures >= self._max_fail:
                self._try_fallback(reason="repeated ticker failures")
            return []

    def _fetch_tickers(self, exchange: str) -> list[dict]:
        ep = _ENDPOINTS[exchange]
        if exchange == "okx":     return self._tickers_okx(ep)
        if exchange == "binance": return self._tickers_binance(ep)
        if exchange == "bybit":   return self._tickers_bybit(ep)
        if exchange == "mexc":    return self._tickers_mexc(ep)
        raise ValueError(f"Unknown: {exchange}")

    def _tickers_okx(self, ep: dict) -> list[dict]:
        data = _get(ep["tickers"])
        out  = []
        for t in data.get("data", []):
            if not t["instId"].endswith("-USDT-SWAP"): continue
            try:
                price    = float(t["last"])
                vol_q    = float(t.get("volCcy24h", 0))
                vol_usdt = vol_q * price if vol_q < 1e9 else float(t.get("vol24h", 0))
                if vol_usdt < MIN_VOLUME_USD: continue
                chg = float(t.get("chgUtc8", t.get("sodUtc8", "0"))) * 100
                out.append({"symbol": t["instId"], "price": price,
                            "change_pct": chg, "volume_usd": vol_usdt})
            except (KeyError, ValueError, TypeError): continue
        out.sort(key=lambda x: x["volume_usd"], reverse=True)
        return out[:MAX_SYMBOLS_SCAN]

    def _tickers_binance(self, ep: dict) -> list[dict]:
        data = _get(ep["tickers"])
        out  = []
        for t in data:
            if not t["symbol"].endswith("USDT"): continue
            try:
                vol_usdt = float(t["quoteVolume"])
                if vol_usdt < MIN_VOLUME_USD: continue
                out.append({"symbol": t["symbol"], "price": float(t["lastPrice"]),
                            "change_pct": float(t["priceChangePercent"]), "volume_usd": vol_usdt})
            except (KeyError, ValueError, TypeError): continue
        out.sort(key=lambda x: x["volume_usd"], reverse=True)
        return out[:MAX_SYMBOLS_SCAN]

    def _tickers_bybit(self, ep: dict) -> list[dict]:
        data = _get(ep["tickers"])
        out  = []
        for t in data.get("result", {}).get("list", []):
            sym = t.get("symbol","")
            if not sym.endswith("USDT"): continue
            try:
                vol_usdt = float(t.get("turnover24h", 0))
                if vol_usdt < MIN_VOLUME_USD: continue
                out.append({"symbol": sym, "price": float(t["lastPrice"]),
                            "change_pct": float(t.get("price24hPcnt", 0)) * 100,
                            "volume_usd": vol_usdt})
            except (KeyError, ValueError, TypeError): continue
        out.sort(key=lambda x: x["volume_usd"], reverse=True)
        return out[:MAX_SYMBOLS_SCAN]

    def _tickers_mexc(self, ep: dict) -> list[dict]:
        data = _get(ep["tickers"])
        raw  = data.get("data", [])
        # MEXC's documented example response for this endpoint shows `data`
        # as a single object when called without a `symbol` filter — it's
        # genuinely ambiguous from the docs alone whether omitting `symbol`
        # returns one ticker or all of them, so this handles both shapes
        # defensively rather than assuming.
        items = raw if isinstance(raw, list) else [raw] if raw else []
        out = []
        for t in items:
            sym = t.get("symbol", "")
            if not sym.endswith("_USDT"): continue
            try:
                price    = float(t["lastPrice"])
                vol_usdt = float(t.get("amount24", 0))  # amount24 = 24h turnover in quote currency
                if vol_usdt < MIN_VOLUME_USD: continue
                out.append({"symbol": sym, "price": price,
                            "change_pct": float(t.get("riseFallRate", 0)) * 100,
                            "volume_usd": vol_usdt})
            except (KeyError, ValueError, TypeError): continue
        if len(items) <= 1:
            logger.warning("[FEED] MEXC ticker endpoint returned only %d symbol(s) without a "
                           "symbol filter — if you need full-market scanning on MEXC, this may "
                           "need per-symbol calls against /api/v1/contract/detail's symbol list "
                           "instead. Flagging rather than silently scanning 1 symbol.", len(items))
        out.sort(key=lambda x: x["volume_usd"], reverse=True)
        return out[:MAX_SYMBOLS_SCAN]

    # ── Candles ───────────────────────────────────────────────────────────────
    def get_candles(self, symbol: str, tf: str = LTF, limit: int = LTF_BARS) -> pd.DataFrame:
        """
        BUG THIS FIXES: this used to fall through to OTHER exchanges any
        time the active exchange returned an empty DataFrame for ANY
        reason — including the routine case of "this symbol just doesn't
        exist on this exchange" (e.g. you deliberately picked MEXC, but a
        symbol from MEXC's own scan results doesn't carry over verbatim,
        or a thin/new listing has no candle history yet). Confirmed from a
        real log: after switching to MEXC (which was working fine —
        "Scanning 153 symbols... Scan complete — 99 signals found"), every
        single candle-evaluation call was STILL generating DNS-failure
        noise from OKX/Binance/Bybit attempts, because those three were
        unconditionally tried as "fallback" on every miss, even though
        none of them had anything to do with why a given symbol came back
        empty on MEXC, and all three are independently known-unreachable
        on this connection.

        Now: only attempt other exchanges if the active one is in
        self._blocked_exchanges (confirmed hard-blocked) OR has exceeded
        its candle-failure budget (confirmed unreachable, not just one
        missing symbol). Otherwise an empty result for one symbol on the
        exchange you actually chose just stays empty — that's correct
        behaviour, not something to paper over by quietly querying three
        other exchanges you didn't ask for.
        """
        df = self._fetch_candles(self._active, symbol, tf, limit)
        if df is not None and not df.empty:
            self._candle_failures = 0  # a clean fetch means the active exchange is fine
            return df

        active_confirmed_down = (
            self._active in self._blocked_exchanges
            or self._candle_failures >= self._max_fail
        )
        if not active_confirmed_down:
            return pd.DataFrame()  # just this symbol — don't go hunting on other exchanges

        start_exchange = self._active
        chain = [
            ex for ex in _FALLBACK_ORDER.get(start_exchange, [])
            if ex not in self._blocked_exchanges
        ]
        for exchange in chain:
            sym = self._adapt_symbol(symbol, exchange)
            df  = self._fetch_candles(exchange, sym, tf, limit)
            if df is not None and not df.empty:
                return df
        return pd.DataFrame()

    def _fetch_candles(self, exchange: str, symbol: str, tf: str, limit: int) -> pd.DataFrame | None:
        bar = self._bar_str(tf, exchange)
        ep  = _ENDPOINTS[exchange]
        try:
            if exchange == "okx":
                url  = ep["candles"].format(symbol=symbol, bar=bar, limit=min(limit, 300))
                data = _get(url)
                raw  = data.get("data", [])
                return _parse_candles_okx(raw) if raw else pd.DataFrame()
            elif exchange == "binance":
                sym  = _to_binance(symbol)
                url  = ep["candles"].format(symbol=sym, bar=bar, limit=min(limit, 500))
                raw  = _get(url)
                return _parse_candles_binance(raw) if raw else pd.DataFrame()
            elif exchange == "bybit":
                sym  = _to_bybit(symbol)
                url  = ep["candles"].format(symbol=sym, bar=bar, limit=min(limit, 200))
                data = _get(url)
                raw  = data.get("result", {}).get("list", [])
                return _parse_candles_bybit(raw) if raw else pd.DataFrame()
            elif exchange == "mexc":
                sym  = _to_mexc(symbol)
                # MEXC's kline endpoint takes the symbol as a path segment,
                # not a query param, and has no `limit` parameter at all
                # (it returns up to 2000 points based on start/end time,
                # which we're not passing — so it returns its default
                # window, most-recent-first per the docs' "closest to
                # current time" behaviour when no start/end given).
                url  = ep["candles"].format(symbol=sym, bar=bar)
                data = _get(url)
                raw  = data.get("data", {})
                return _parse_candles_mexc(raw) if raw else pd.DataFrame()
        except Exception as e:
            # This used to fail completely silently (just `return None`), which
            # meant an exchange-side geo-block (451/403) on EVERY symbol would
            # never trip the fallback logic — only the ticker endpoint did
            # that. A single bad symbol/timeframe gap is fine to swallow
            # quietly; a hard block or a rate-limit storm is now tracked and
            # acted on here regardless of which exchange in a fallback chain
            # this particular call was trying — NOT gated to "only if this
            # is currently self._active", because get_candles() may be
            # trying several exchanges within one call (see its docstring
            # note on why the chain is snapshotted up front), and a block
            # discovered on exchange #2 or #3 in that chain is just as real
            # and worth remembering as one discovered on #1.
            if self._is_hard_block(e):
                if exchange not in self._blocked_exchanges:
                    self._blocked_exchanges.add(exchange)
                    logger.error("[FEED] %s geo-blocked candle requests (%s) — marking blocked.",
                                 exchange.upper(), e)
                # Only actually switch self._active if the exchange that
                # just failed IS the one we're currently set to use —
                # otherwise we'd be "switching to" something the chain
                # already moved past, which is meaningless.
                if exchange == self._active:
                    self._try_fallback(reason="geo-blocked on candles")
            elif self._is_rate_limit(e):
                self._rate_limit_hits.append(time.time())
                cutoff = time.time() - 60
                self._rate_limit_hits = [t for t in self._rate_limit_hits if t >= cutoff]
            elif exchange == self._active:
                self._candle_failures += 1
            logger.debug("[FEED] %s candle %s@%s: %s", exchange.upper(), symbol, tf, e)
            return None

    def recent_rate_limit_hits(self, window_sec: int = 60) -> int:
        """Used by the scanner to adaptively shrink worker count when the
        active exchange is rate-limiting us — see core/scanner.py."""
        cutoff = time.time() - window_sec
        return len([t for t in self._rate_limit_hits if t >= cutoff])

    def get_multi_tf(self, symbol: str) -> dict[str, pd.DataFrame]:
        tasks = {LTF: (LTF, LTF_BARS), HTF: (HTF, HTF_BARS), SCALP_TF: (SCALP_TF, SCALP_BARS)}
        results: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self.get_candles, symbol, tf, bars): key
                       for key, (tf, bars) in tasks.items()}
            for fut in as_completed(futures):
                key = futures[fut]
                try:    results[key] = fut.result()
                except: results[key] = pd.DataFrame()
        return results

    def active_exchange(self) -> str:
        return self._active

    def connection_status(self, exchanges: list[str] | None = None) -> dict:
        """
        Pings each exchange's ticker endpoint to check reachability.
        Pass `exchanges=["mexc"]` to check only one (used by the periodic
        health check so it doesn't ping exchanges you're not using) —
        omit it to check all four (used by the dashboard's manual
        /network button and the on-demand "all exchanges" diagnostic).
        """
        targets = exchanges if exchanges is not None else list(_ENDPOINTS.keys())
        status = {}
        for ex in targets:
            ep = _ENDPOINTS.get(ex)
            if ep is None:
                continue
            t0 = time.monotonic()
            try:
                _get(ep["tickers"], timeout=6, retries=1)
                status[ex] = {"ok": True, "ms": round((time.monotonic() - t0) * 1000),
                              "geo_blocked": False}
            except Exception as e:
                hard = self._is_hard_block(e)
                status[ex] = {
                    "ok": False, "error": str(e)[:80],
                    "geo_blocked": hard,
                    "note": ("Exchange is deliberately refusing this IP (451/403) — "
                              "this is NOT a DNS issue, DoH bypass will not fix it.")
                              if hard else None,
                }
        return status
