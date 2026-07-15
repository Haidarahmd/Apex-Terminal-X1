"""
APEX Network Diagnostic + DNS Fix Tool
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run this FIRST if the bot shows "getaddrinfo failed" or network errors.

Usage:
  python check_network.py
  python check_network.py --fix-dns        ← applies DNS fix automatically
  python check_network.py --proxy http://127.0.0.1:7890
"""
import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request


EXCHANGES = {
    "okx":     ("www.okx.com",      "https://www.okx.com/api/v5/market/tickers?instType=SWAP"),
    "binance": ("fapi.binance.com", "https://fapi.binance.com/fapi/v1/ping"),
    "bybit":   ("api.bybit.com",    "https://api.bybit.com/v5/market/time"),
}

# Fallback IPs resolved via Google DoH (DNS-over-HTTPS)
# These are looked up fresh each run so they stay current
GOOGLE_DOH = "https://dns.google/resolve?name={}&type=A"

DNS_SERVERS = ["8.8.8.8", "1.1.1.1", "9.9.9.9"]


def col(text, code): return f"\033[{code}m{text}\033[0m"
def green(t):  return col(t, "32")
def red(t):    return col(t, "31")
def yellow(t): return col(t, "33")
def bold(t):   return col(t, "1")
def cyan(t):   return col(t, "36")


# ── DNS-over-HTTPS resolver ────────────────────────────────────────────────────
def resolve_via_doh(hostname: str, dns_server: str = "8.8.8.8") -> str | None:
    """
    Resolve hostname using Google/Cloudflare DNS-over-HTTPS.
    Bypasses ISP DNS completely.
    """
    urls = [
        f"https://dns.google/resolve?name={hostname}&type=A",
        f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
        f"https://doh.opendns.com/dns-query?name={hostname}&type=A",
    ]
    ctx = ssl.create_default_context()
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/dns-json", "User-Agent": "APEX-Diag/1.1"}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())
            answers = data.get("Answer", [])
            for a in answers:
                if a.get("type") == 1:  # A record
                    return a["data"]
        except Exception:
            continue
    return None


def resolve_via_socket_override(hostname: str) -> str | None:
    """Try to resolve using explicit DNS server via socket (Windows workaround)."""
    # On Windows, we can't change the DNS server per-query easily,
    # but we can try getaddrinfo with a numeric hint
    try:
        results = socket.getaddrinfo(hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
        if results:
            return results[0][4][0]
    except Exception:
        pass
    return None


# ── HTTP with IP injection ─────────────────────────────────────────────────────
def http_get_with_ip(url: str, ip: str, proxy: str | None = None) -> tuple[bool, int, int | None, str]:
    """
    Make HTTPS request using a pre-resolved IP (bypasses DNS entirely).
    Injects the Host header so SNI/TLS still works.
    Returns (success, ms, http_status_or_None, error_summary).
    """
    from urllib.parse import urlparse
    parsed   = urlparse(url)
    hostname = parsed.netloc
    port     = parsed.port or 443
    path     = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    # Replace hostname with IP in URL
    ip_url = url.replace(f"https://{hostname}", f"https://{ip}:{port}", 1)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False   # we supply IP, not hostname

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            ip_url,
            headers={
                "Host":       hostname,
                "User-Agent": "APEX-Diag/1.1",
            }
        )
        with opener.open(req, timeout=10) as r:
            r.read(256)
            return True, round((time.monotonic() - t0) * 1000), r.status, ""
    except urllib.error.HTTPError as e:
        # This is the case the old version completely discarded: the
        # connection succeeded fine and the SERVER answered — it just
        # answered with a refusal. 451/403 here means "I can see you and
        # I am choosing not to serve you", which is a fundamentally
        # different problem from a timeout or connection failure, and
        # needs a different fix (not a DNS/IP one).
        return False, round((time.monotonic() - t0) * 1000), e.code, str(e)
    except Exception as e:
        return False, round((time.monotonic() - t0) * 1000), None, str(e)


def check_direct_http():
    """
    Straightforward HTTPS request through normal DNS resolution — no IP
    substitution, no DoH. This is the check that was MISSING: if your ISP's
    DNS resolves the exchange's hostname fine (the common case, especially
    if you're behind a VPN or your ISP doesn't filter DNS specifically),
    the old version of this script declared "APEX should run normally" and
    stopped — without ever checking whether the exchange's server itself
    still refuses the connection with a 451/403. That's exactly the
    scenario this checks for.
    """
    print(bold("\n── 1b. Direct HTTPS to each exchange (normal DNS) ──"))
    geo_blocked = {}
    reachable = {}
    for name, (host, url) in EXCHANGES.items():
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "APEX-Diag/1.1"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read(256)
                ms = round((time.monotonic() - t0) * 1000)
                print(f"  {green('✓')} {name.upper():<10} {ms:>5} ms  HTTP {r.status}")
                reachable[name] = ms
        except urllib.error.HTTPError as e:
            ms = round((time.monotonic() - t0) * 1000)
            if e.code in (451, 403):
                print(f"  {yellow('⚠')} {name.upper():<10} {ms:>5} ms  HTTP {e.code} — "
                      f"{yellow('exchange is deliberately refusing this IP (geo-block, NOT a DNS issue)')}")
                geo_blocked[name] = e.code
            else:
                print(f"  {red('✗')} {name.upper():<10} {ms:>5} ms  HTTP {e.code}: {e.reason}")
        except Exception as e:
            ms = round((time.monotonic() - t0) * 1000)
            print(f"  {red('✗')} {name.upper():<10} {ms:>5} ms  {type(e).__name__}: {e}")
    return reachable, geo_blocked


# ── Diagnostic steps ──────────────────────────────────────────────────────────
def check_dns_standard():
    print(bold("\n── 1. Standard DNS (ISP resolver) ──────────────"))
    results = {}
    for name, (host, url) in EXCHANGES.items():
        try:
            ip = socket.gethostbyname(host)
            print(f"  {green('✓')} {name.upper():<10} {host} → {ip}")
            results[name] = ip
        except socket.gaierror as e:
            print(f"  {red('✗')} {name.upper():<10} {host} → BLOCKED by ISP DNS ({e})")
            results[name] = None
    return results


def check_dns_doh():
    print(bold("\n── 2. DNS-over-HTTPS (bypasses ISP) ────────────"))
    resolved = {}
    for name, (host, url) in EXCHANGES.items():
        ip = resolve_via_doh(host)
        if ip:
            print(f"  {green('✓')} {name.upper():<10} {host} → {ip}  {cyan('(via DoH)')}")
            resolved[name] = (host, ip, url)
        else:
            print(f"  {red('✗')} {name.upper():<10} {host} → Could not resolve via DoH either")
            resolved[name] = None
    return resolved


def check_http_with_ips(resolved: dict, proxy: str | None):
    print(bold("\n── 3. HTTP connectivity (using resolved IPs) ────"))
    ok = {}
    geo_blocked = {}
    for name, entry in resolved.items():
        if entry is None:
            print(f"  {red('✗')} {name.upper():<10} Skipped — no IP resolved")
            continue
        host, ip, url = entry
        success, ms, status, err = http_get_with_ip(url, ip, proxy)
        if success:
            print(f"  {green('✓')} {name.upper():<10} {ms:>5} ms  via {ip}")
            ok[name] = (host, ip)
        elif status in (451, 403):
            print(f"  {yellow('⚠')} {name.upper():<10} {ms:>5} ms  HTTP {status} — "
                  f"{yellow('exchange is deliberately refusing this IP (geo-block)')}")
            geo_blocked[name] = (host, ip, status)
        else:
            print(f"  {red('✗')} {name.upper():<10} {ms:>5} ms  IP reachable but HTTP failed"
                  f"{f' (HTTP {status})' if status else ''}")
    return ok, geo_blocked


def print_fix_instructions(isp_blocked: bool, doh_ok: dict, http_ok: dict,
                            direct_geo_blocked: dict, doh_geo_blocked: dict):
    print(bold("\n── Diagnosis ────────────────────────────────────"))

    all_geo_blocked = {**direct_geo_blocked, **doh_geo_blocked}

    if not isp_blocked and not all_geo_blocked:
        print(green("  ✓ DNS resolves fine AND the exchange(s) answered normally. APEX should run normally."))
        return

    if not isp_blocked and all_geo_blocked:
        # This is the case the old script could never reach: DNS is totally
        # fine, but the exchange's own server is refusing the connection.
        blocked_names = ", ".join(k.upper() for k in all_geo_blocked)
        verb = "is" if len(all_geo_blocked) == 1 else "are"
        print(red(f"  ✗ DNS is fine — but {blocked_names} {verb} returning HTTP 451/403."))
        print(f"    This means the exchange's own server SEES your request and is choosing")
        print(f"    to refuse it (compliance/geo-fencing), not a DNS or routing problem.")
        print(f"    {bold('No DNS fix, DoH bypass, or APEX_USE_DOH setting can fix this.')}")
        print()
        print(bold("  ► If you are already using a VPN and seeing this:"))
        print("      1. Confirm the VPN is ACTUALLY routing this traffic. Run:")
        print(cyan("           curl https://ifconfig.me"))
        print("         while the VPN shows 'Connected'. If that prints your real ISP IP")
        print("         (not the VPN's), the VPN is not covering this terminal/Python process —")
        print("         common with browser-extension-only VPNs or split-tunneling defaults.")
        print("      2. If it DOES show a foreign IP and you still get 451/403, that specific")
        print("         VPN exit IP is likely on the exchange's own blocklist — exchanges")
        print("         actively blocklist known VPN/datacenter IP ranges. Try switching to a")
        print("         different server location in your VPN app (try 2-3 different countries).")
        print("      3. If no VPN server location works, that exchange may be blocking the")
        print("         entire IP range your VPN provider uses. Consider a different exchange")
        print("         or VPN provider with residential/dedicated IPs.")
        print()
        remaining = [k for k in EXCHANGES if k not in all_geo_blocked]
        if remaining:
            print(bold(f"  ► EASIER FIX — switch exchanges:"))
            print(f"      {', '.join(r.upper() for r in remaining)} did not return a geo-block in this test.")
            print(cyan(f"      set APEX_EXCHANGE={remaining[0]}"))
            print(cyan(f"      python main.py"))
        print()
        return

    print(red("  ✗ ISP is blocking DNS resolution for all crypto exchanges."))
    print(f"    Google IP (8.8.8.8) resolves fine → internet is UP, just DNS-filtered.")
    print()

    if http_ok:
        working = list(http_ok.keys())
        print(green(f"  ✓ Good news: exchanges ARE reachable via IP ({', '.join(w.upper() for w in working)})"))
        print(f"    APEX has built-in DNS bypass — it will use the IPs directly.")
        print()
        print(bold("  ► IMMEDIATE FIX — run APEX with DNS bypass enabled:"))
        print()
        print(cyan(f"      set APEX_USE_DOH=1"))
        print(cyan(f"      set APEX_EXCHANGE={'binance' if 'binance' in working else working[0]}"))
        print(cyan(f"      python main.py"))
        print()
        print(bold("  ► PERMANENT FIX — change Windows DNS settings:"))
        print("      1. Open Network Adapter settings")
        print("      2. Right-click your connection → Properties")
        print("      3. IPv4 → Properties → Use the following DNS:")
        print(cyan("         Preferred:  1.1.1.1   (Cloudflare)"))
        print(cyan("         Alternate:  8.8.8.8   (Google)"))
        print("      4. Click OK — takes effect immediately, no reboot needed")
        print()
        print(bold("  ► ALTERNATIVE — use a VPN:"))
        print("      Any VPN (ProtonVPN free tier works) will bypass the ISP DNS block.")
        print("      Note: this fixes DNS-filtering specifically. If the exchange ALSO")
        print("      geo-blocks by IP (see section 1b above), a VPN is required either way —")
        print("      DNS bypass alone won't get you past an IP-level block.")
    else:
        print(red("  ✗ Exchanges are NOT reachable even via IP."))
        print("    This means deep packet inspection (DPI) is blocking HTTPS to exchange IPs.")
        print()
        print(bold("  ► REQUIRED: Use a VPN"))
        print("      Free options: ProtonVPN, Windscribe, TunnelBear")
        print("      Or use a SOCKS5 proxy:")
        print(cyan("      set HTTPS_PROXY=socks5://127.0.0.1:1080"))

    if doh_geo_blocked:
        blocked_names = ", ".join(k.upper() for k in doh_geo_blocked)
        print()
        print(yellow(f"  ⚠ Also note: even via DoH-resolved IPs, {blocked_names} returned 451/403."))
        print(f"    That exchange will keep blocking you regardless of DNS fix — see VPN guidance above.")


def write_ip_cache(resolved: dict):
    """Write resolved IPs to a file APEX can load at startup."""
    cache = {}
    for name, entry in resolved.items():
        if entry:
            host, ip, url = entry
            cache[host] = ip
    if cache:
        cache_path = os.path.join(os.path.dirname(__file__), "data_store", "dns_cache.json")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
        print(green(f"\n  ✓ IP cache written to data_store/dns_cache.json"))
        print(f"    APEX will load this automatically on next start.")
        return cache_path
    return None


def main():
    parser = argparse.ArgumentParser(description="APEX Network Diagnostic")
    parser.add_argument("--proxy",    default=os.getenv("HTTPS_PROXY", ""))
    parser.add_argument("--fix-dns",  action="store_true", help="Write DNS IP cache for APEX")
    parser.add_argument("--exchange", default=None)
    args = parser.parse_args()

    global EXCHANGES
    if args.exchange:
        EXCHANGES = {k: v for k, v in EXCHANGES.items() if k == args.exchange.lower()}

    proxy = args.proxy or None

    print(bold("APEX Terminal — Network Diagnostic v1.2"))
    print(f"Python: {sys.version.split()[0]} | Platform: {sys.platform}")

    # Step 1: Standard DNS
    isp_results = check_dns_standard()
    isp_blocked = any(v is None for v in isp_results.values())

    # Step 1b: Direct HTTPS through normal DNS — catches exchange-side
    # geo-blocking even when DNS resolves perfectly fine, which the old
    # version of this script never checked for at all.
    direct_reachable, direct_geo_blocked = check_direct_http()

    # Step 2: DoH bypass
    doh_results = check_dns_doh()
    doh_ok      = {k: v for k, v in doh_results.items() if v}

    # Step 3: HTTP with IPs
    http_ok = {}
    doh_geo_blocked = {}
    if doh_ok:
        http_ok, doh_geo_blocked = check_http_with_ips(doh_results, proxy)

    # Advice
    print_fix_instructions(isp_blocked, doh_ok, http_ok, direct_geo_blocked, doh_geo_blocked)

    # Write cache if requested or if ISP blocked
    if (args.fix_dns or isp_blocked) and doh_ok:
        write_ip_cache(doh_results)

    print()
    all_geo_blocked = {**direct_geo_blocked, **doh_geo_blocked}
    if all_geo_blocked and not isp_blocked:
        verb = "is" if len(all_geo_blocked) == 1 else "are"
        print(yellow(f"  STATUS: DNS is fine, but {', '.join(k.upper() for k in all_geo_blocked)} "
                     f"{verb} geo-blocking this IP. A VPN with a non-blocklisted exit IP is required —"))
        print(yellow(f"          DoH/DNS settings will not help here."))
    elif http_ok:
        print(green("  STATUS: APEX CAN run — use  set APEX_USE_DOH=1  before starting."))
    elif doh_ok:
        print(yellow("  STATUS: DNS bypass works but HTTP is blocked. Use a VPN."))
    else:
        print(red("  STATUS: Complete connectivity failure. Use a VPN or fix DNS."))
    print()


if __name__ == "__main__":
    main()
