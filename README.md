# ⚡ APEX Terminal v2.0

**The most powerful open-source crypto trading bot — broker-agnostic, zero MT5 (by default).**

Built from the best of RAZOR Terminal (scanner UI) and Trading Bot v5 (multi-strategy engine), then massively upgraded — and now with a self-healing Watchdog agent and TradingView account/webhook integration.

---

## ⚠️ Read this before you run it live

This is self-hosted, non-custodial software: **you** run it, on **your** machine/VPS, with **your own** exchange API keys. It never takes custody of anyone else's funds and has no pooled-account or copy-trading-with-custody features — that's deliberate.

If you plan to let other people use this (not just yourself):
- **Self-hosted / bring-your-own-keys** (each person runs their own copy, with their own API keys, on their own machine) is the model this is built for, similar to how Freqtrade or Hummingbot are distributed. This carries a much lower regulatory bar than what follows.
- **Custodial / pooled / "I trade on your behalf" / hosted-as-a-service with their keys on your server** crosses into investment management territory. In Nigeria this is regulated by the SEC, and operating it without registration is a real legal exposure for you — not a formality. Get local legal advice before charging anyone money for this in any custodial form.
- Nothing in this codebase performs account pooling or fund custody, and that's intentional. Don't add it without understanding what licence it would require.

This software can lose money. Past backtest/paper performance does not predict live results. Start in paper mode, understand every parameter in `config/settings.py`, and only risk capital you can afford to lose.

---

## What's New in v2.0

| Feature | v1.0 | **v2.0** |
|---|---|---|
| Reliability | Crashes kill the process | **Watchdog agent** — auto-fixes network/rate-limit/data issues, queues code-level bugs for your approval, never silently patches `execution/` |
| Live position tracking | `get_open_positions`/`update_sl` silently no-op'd | Fully implemented for OKX, Binance, Bybit, MEXC (real position list, SL updates, partial closes) |
| Live `close_position` | Raised `NotImplementedError` | Implemented for all 4 exchanges |
| Take-profit | Single TP, all-or-nothing exit | **TP1/TP2/TP3 ladder** — partial closes at each level with SL ratcheted up after each (breakeven after TP1, TP1's price after TP2) |
| Exchange options | OKX, Binance, Bybit | + **MEXC** — included specifically because it remains accessible where OKX has formally exited (Nigeria) and Binance/Bybit are commonly IP-geofenced |
| TradingView | — | Webhook receiver (Pine alerts → trades) + real broker account-state panel (Deriv, MT5 bridge) |
| Geo-blocking (Nigeria/ISP DNS filtering) | DoH bypass existed but undocumented | Watchdog auto-enables it on DNS failure; instant exchange fallback on 451/403 (no longer silently swallowed); `/agent/status` and `/network` show you whether it's a DNS issue (fixable) or exchange-side geofencing (not fixable by DNS bypass — see below) |
| Trade visibility | Silent rejection reasons (DEBUG-only logs) | **Per-cycle diagnostic summary** — candidates evaluated, exact rejection reason per candidate, visible in the Agent tab and `/state` |
| Dashboard | 6 tabs | + **Agent tab**: watchdog health, pending patch queue, connected accounts, last-cycle "why no trades" breakdown |

## What's New vs v5 + RAZOR (original v1.0 comparison)

| Feature | Trading Bot v5 | RAZOR Terminal | **APEX Terminal** |
|---|---|---|---|
| Exchange | MetaTrader5 only | OKX (read-only) | OKX · Binance · Bybit (live trading) |
| Strategies | 4 | Scanner only | 4 strategies + weighted aggregator |
| Signal score | Basic | 10 indicators | 16 indicators + FVG + S/R breakout |
| HTF alignment | ✓ | ✗ | ✓ (4H bias filter) |
| Regime detection | ✗ | Basic | 5 regimes (BREAKOUT/TREND/RANGING) |
| Volatility Gate | ✓ | ✗ | ✓ |
| Conflict filter | ✗ | ✗ | ✓ (blocks ambiguous signals) |
| Partial TP | ✓ | ✗ | ✓ |
| Trailing Stop | ✓ | ✗ | ✓ + breakeven lock |
| Self-learner | ✓ | ✗ | ✓ + sigma decay (explore→exploit) |
| Symbol Scorer | ✓ | ✗ | ✓ + position size scaling |
| News filter | ✓ | ✗ | ✓ + live ForexFactory RSS |
| Correlation filter | ✓ | ✗ | ✓ |
| Telegram | ✓ | ✗ | ✓ + daily summary |
| Web UI | Basic | Pro dark terminal | Full dashboard + scanner + backtest |
| Backtest | ✓ | ✗ | ✓ + equity curve chart |
| REST API | ✗ | ✓ | ✓ (full CRUD + manual trades) |
| MT5 dependency | Required | ✗ | **Zero — removed completely** |

---

## Quick Start

### 1. Install dependencies
```bash
pip install numpy pandas
```

### 2. Run (paper trading, OKX, with web UI)
```bash
cd apex_terminal
python main.py
# Open http://localhost:8080
```

### 3. One-shot scan (no engine needed)
```bash
python main.py --scan
```

### 4. Backtest
```bash
python main.py --backtest BTC-USDT-SWAP --strategy macd_ema --bars 500
python main.py --backtest BTCUSDT --exchange binance --strategy breakout
```

### 5. Live trading
```bash
export APEX_MODE=live
export APEX_EXCHANGE=okx
export OKX_API_KEY=...
export OKX_API_SECRET=...
export OKX_PASSPHRASE=...
python main.py --mode live
# The Watchdog supervises by default. Add --no-watchdog to disable it
# (e.g. if you're running under systemd with its own restart policy).
```

### 6. ISP DNS blocked (Nigeria and similar)
```bash
python check_network.py          # diagnoses DNS-filtering vs exchange-side geofencing
export APEX_USE_DOH=1             # only needed if you're running with --no-watchdog;
                                   # the Watchdog enables this automatically on DNS failure otherwise
python main.py
```

### 7. TradingView webhook + broker account state
```bash
export APEX_TV_WEBHOOK_SECRET=choose-a-long-random-string   # required, no default
export DERIV_API_TOKEN=...        # optional — shows Deriv balance in the Agent tab
python main.py
# TradingView alert webhook URL: http://your-server:8080/tradingview/webhook
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `APEX_MODE` | `paper` | `paper` or `live` |
| `APEX_EXCHANGE` | `okx` | `okx`, `binance`, `bybit`, `mexc` |
| `APEX_RISK_PCT` | `0.01` | Risk per trade (1%) |
| `APEX_MAX_DD` | `0.05` | Daily drawdown halt (5%) |
| `APEX_MAX_POS` | `5` | Max concurrent positions |
| `APEX_MIN_VOL` | `1000000` | Min 24h volume USD for scanner |
| `APEX_SCAN_WORKERS` | `6` | Concurrent scan workers (lowered from 12 — see Nigeria/geo-blocking section; auto-throttles further if rate-limited) |
| `APEX_USE_DOH` | `0` | DNS-over-HTTPS bypass for ISP DNS filtering — auto-enabled by the Watchdog on DNS failure |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID |
| `OKX_API_KEY` | — | OKX API key (live only) |
| `OKX_API_SECRET` | — | OKX API secret |
| `OKX_PASSPHRASE` | — | OKX passphrase |
| `BINANCE_API_KEY` | — | Binance API key (live only) |
| `BINANCE_API_SECRET` | — | Binance API secret |
| `BYBIT_API_KEY` | — | Bybit API key (live only) |
| `BYBIT_API_SECRET` | — | Bybit API secret |
| `MEXC_API_KEY` | — | MEXC API key (live only) — requires KYC-verified account for trading permissions |
| `MEXC_API_SECRET` | — | MEXC API secret |
| `APEX_TV_WEBHOOK_SECRET` | — | **Required** to accept TradingView webhooks at all — requests without a matching secret are rejected |
| `DERIV_API_TOKEN` | — | Deriv account API token (read-only is enough) — for the account-state panel |
| `DERIV_APP_ID` | `1089` (public test ID) | Register your own free app_id at api.deriv.com for production use |
| `MT5_BRIDGE_MODE` | — (disabled) | `native` (Windows + `MetaTrader5` package) or `http` (your own bridge) — for Exness/MT5 account state |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | — | For `MT5_BRIDGE_MODE=native` |
| `MT5_BRIDGE_URL` | — | For `MT5_BRIDGE_MODE=http` |

---

## Architecture

```
apex_terminal/
├── main.py                  ← Entry point (CLI args)
├── webapp_server.py         ← REST API + static file server
├── index.html               ← Full trading terminal UI
├── config/settings.py       ← All parameters
├── data/feed.py             ← Exchange data (OKX/Binance/Bybit)
├── indicators/core.py       ← Pure numpy/pandas indicators
├── strategies/
│   ├── macd_ema.py          ← MACD + EMA trend follow
│   ├── rsi_reversal.py      ← RSI + Bollinger reversal
│   ├── breakout.py          ← ATR S/R breakout + FVG
│   ├── scalp.py             ← 15m EMA cross scalp
│   └── aggregator.py        ← Weighted vote combiner
├── core/
│   ├── engine.py            ← Main trading loop
│   └── scanner.py           ← Multi-symbol scanner (200+ pairs)
├── risk/
│   ├── position_sizing.py   ← ATR-based risk sizing
│   ├── drawdown_guard.py    ← Daily DD halt
│   ├── correlation_filter.py← Blocks correlated entries
│   ├── trailing_stop.py     ← Breakeven + dynamic trail
│   └── tp_ladder.py         ← TP1/TP2/TP3 ladder (40%/35%/rest, SL ratchets up each level)
├── execution/
│   ├── paper_executor.py    ← Full simulated trading
│   ├── live_executor.py     ← OKX/Binance/Bybit/MEXC REST
│   └── router.py            ← Mode switcher
├── filters/
│   ├── session.py           ← Trading session gate
│   ├── news.py              ← High-impact news blackout
│   └── volatility_gate.py   ← ATR flatness filter
├── learning/
│   ├── self_learner.py      ← Weight + param optimiser
│   └── symbol_scorer.py     ← Per-symbol performance tracker
├── journal/trade_journal.py ← CSV trade log + analytics
├── backtest/backtester.py   ← Walk-forward backtester
├── agent/                   ← Watchdog: self-healing supervisor
│   ├── watchdog.py            ← Wraps the engine loop, catches & classifies errors
│   ├── diagnostics.py         ← Error → category → safe-fix mapping
│   ├── healthcheck.py         ← Proactive checks (disk, feed, auth, drawdown)
│   └── patch_manager.py       ← Queues anything risky for YOUR approval
├── integrations/tradingview/
│   ├── webhook_server.py      ← Receives TradingView Pine alerts as trade signals
│   ├── account_state.py       ← Aggregates all connected broker adapters
│   └── broker_adapters/       ← deriv.py, mt5_bridge.py, crypto_exchange.py
└── utils/
    ├── telegram.py          ← Rich Telegram alerts
    └── equity_tracker.py    ← Daily baseline persistence
```

---

## 🛡️ The Watchdog Agent (self-healing)

`python main.py` now runs the engine **under supervision** by default. Every cycle is wrapped: if it throws, the Watchdog classifies the failure and reacts.

**Auto-fixed immediately, no approval needed (safe by construction):**
- DNS/network errors → enables the DNS-over-HTTPS bypass for the rest of the session
- Rate limits → backs off and retries
- Data gaps for one symbol → skipped, doesn't count as an engine-wide failure
- Corrupted local JSON cache → quarantined (renamed, not deleted) and the engine carries on with a fresh file
- Exchange auth failure → live trading is force-switched to paper mode immediately, so a bad/expired key can't keep firing failed (or worse, partially-failed) live orders

**Escalated to the Patch Queue — visible in the dashboard's Agent tab, nothing applied without you clicking Approve:**
- Anything that looks like a real code bug (KeyError, TypeError, etc.)
- Unrecognised errors the agent has never seen before
- Anything that would touch `execution/` — order-placement code is permanently excluded from auto-patching, full stop. You write/review changes there yourself.
- 8+ consecutive cycle failures (crash-loop detection)

Check `/agent/status` (or the Agent tab) for the live failure log and pending patches. `/agent/healthcheck` runs the proactive checks (disk space, feed reachability, live auth, journal integrity, drawdown state) on demand.

Run with `--no-watchdog` to go back to the old unsupervised loop if you specifically want crashes to propagate (e.g. running under `systemd` with its own restart policy).

**Why it's not a fully autonomous code-rewriting agent:** an agent that can edit its own order-execution logic with no checkpoint is exactly how a subtle bug becomes a silent, unattended account drain. The boundary here — safe runtime fixes auto-applied, anything touching real trading logic queued for a human — is there to protect your capital, not as a policy box-tick.

---

## 🎯 "Why aren't any trades executing?" — the Agent tab will tell you

**The headline bug, found from a real log showing `no_strategy_agreement` as the rejection reason on every single one of 150+ consecutive cycles, with S+/A graded candidates every time:** `macd_ema` (30% of total strategy weight, the single largest), `rsi_reversal` (25%), and `breakout` (25%) — 80% of the total weight — each have an internal minimum-bars check that requires 205–231 bars of 1H history before they'll even attempt to evaluate the market. The engine and scanner were only fetching **150 bars**. That meant these three strategies returned `None` on literally every single call, unconditionally, regardless of what the market was doing — confirmed directly by testing the exact same strategy code with 150 vs. the corrected bar count and checking whether it got past its own internal length-check gate at all (it didn't, with 150; it does, with enough). Only `scalp` (20% weight) could ever fire, and 20% alone can never clear the aggregator's 38% pass threshold — so **zero trades could ever be entered, on any symbol, under any market condition**, until this was fixed. This wasn't "the market disagreeing" despite the log's wording — it was three-quarters of the decision-making logic being silently and permanently disabled by an off-by-80-bars fetch size. Fixed by raising `LTF_BARS` from 150 to 260 (`config/settings.py`), comfortably covering the strictest requirement with margin, and routing both the engine and scanner through that single shared constant instead of separate hardcoded literals so they can't drift out of sync again.

A second, smaller bug shared the same root cause: the scanner's own S+/A grading used a 200-period EMA that also couldn't compute with only 150 bars, and silently fell back to comparing price against itself (`le200 = price`) instead of a real EMA — which meant the "Above EMA200" bullish score bonus could never fire either, quietly biasing every grade the entire time this was running, with no error anywhere. Fixed to skip that specific check cleanly when the EMA genuinely can't be computed (e.g. a brand-new listing with limited history), rather than comparing against a meaningless stand-in.

Separately from that headline bug, this used to be genuinely hard to diagnose in general: the scanner could report "100 signals found" while zero trades happened, because every gate between a scan result and an actual order (session filter, correlation filter, volatility gate, strategy agreement, R:R check, position sizing) logged its rejection at `DEBUG` level only — invisible at the default `INFO` log level, and not surfaced anywhere in the dashboard.

Two more real things were fixed here, not just logging cosmetics:
1. **A redundant, failure-prone re-fetch.** The scanner already fetches and analyses 1H+4H candles to grade each symbol — `_evaluate_signals` was then discarding that and fetching the *same* candles again from scratch (plus a third 15m timeframe) before deciding whether to trade. Every extra fetch was another independent chance for a network blip or geo-block to silently drop that candidate. This is now visible as its own rejection reason (`candle_fetch_failed`) instead of just vanishing.
2. **No visibility into which gate rejected what.** The Agent tab now shows a "Last Cycle — Why No Trades?" card: how many S+/A candidates were evaluated, how many were entered, and a breakdown of every rejection reason with counts (`session_inactive×4, candle_fetch_failed×12, no_strategy_agreement×3`, etc.) — also available at `/state` under `last_cycle_summary` for scripting/monitoring.

If you're seeing `candle_fetch_failed` as the dominant reason, that's a strong signal you're hitting network/geo-blocking issues during evaluation specifically, even if the scanner's own (separately-timed) fetches happened to succeed — see the geo-blocking section below. If you're seeing `no_strategy_agreement` as the dominant reason on every single cycle with no exceptions ever, across many cycles, that's worth treating as suspicious in the same way this bug was — genuine multi-strategy agreement happening literally zero times out of hundreds of attempts is a sign something structural is blocking it, not normal market behaviour.

---

## 🌍 Nigeria / ISP geo-blocking — what this can and can't fix

There are two different problems that get lumped together as "Binance/Bybit/OKX is blocked in Nigeria," and they need different fixes:

1. **ISP-level DNS filtering** — your internet provider's DNS server refuses to resolve `binance.com` etc., but the exchange itself doesn't care where you connect from. **This is what `APEX_USE_DOH=1` and the Watchdog's auto-DoH-bypass fix.** Run `python check_network.py` to check which case you're in.
2. **Exchange-side IP geofencing / compliance blocking** — the exchange's own servers reject connections (HTTP 451 "Unavailable for Legal Reasons", or 403 "Forbidden") as a deliberate regulatory decision. **No DNS trick fixes this** — your DNS resolved correctly and the exchange's server still said no on purpose. The only paths around it are a VPN/residential proxy (which risks violating the exchange's Terms of Service and can get an account frozen — this is your call to make, not something this codebase tries to automate) or using a different exchange that does serve Nigeria.

**Already on a VPN and still getting 451/403?** Two common reasons: (a) the VPN isn't actually covering this process — confirm with `curl https://ifconfig.me` while connected; if it shows your real ISP IP, a browser-extension-only VPN or split-tunneling is the culprit, not APEX. (b) your VPN's specific exit IP is itself on the exchange's blocklist — exchanges actively blocklist known VPN/datacenter ranges precisely because people use VPNs for this. Try a different server location in your VPN app before assuming anything else is broken. `check_network.py` now tests for and reports this exact scenario (DNS fine + exchange still refusing) instead of stopping after the DNS check.

**Seeing `getaddrinfo failed` / DNS errors on every exchange, not clean 451/403s?** That's a different problem from exchange-side geofencing — it means your network can't resolve any of these domains at all (ISP-level DNS filtering, or the DoH bypass IP itself being unreachable, which also showed up as `<urlopen error timed out>` and `SSLV3_ALERT_HANDSHAKE_FAILURE` in one real run). If the DoH bypass IPs themselves are timing out or failing TLS handshakes, the blocking is happening below the DNS layer — at the IP/connection level — and switching exchanges within APEX won't fix it, since the new exchange's domain will hit the same wall. At that point a working VPN (verified per the steps above) is the actual fix, not an APEX setting.

`/network` in the dashboard now tells you explicitly which case you're in — each exchange's status includes a `geo_blocked: true/false` flag, with a note when it's case 2 so you're not left guessing from a raw error string.

**As of this version, case 2 is handled automatically rather than just diagnosed.** A 451/403 on either the ticker feed or candle requests now marks that exchange as blocked and instantly switches the active exchange to the next one in the fallback chain (OKX → Binance → Bybit → MEXC, and equivalent orderings for the others) — no waiting for a failure-count threshold, since a deliberate geo-block isn't going to resolve itself by retrying the same exchange. This was a real gap in the previous version: candle-fetch errors were being swallowed silently and never triggered fallback, so OKX returning 451 on every symbol could (and did, per a real run) go undetected cycle after cycle while still technically "completing" the scan with partial data. Check the log for `geo-blocked` to see this happening; `/agent/status` shows it under the `exchange_geo_block` category.

If ALL your configured exchanges end up geo-blocked, APEX will say so clearly rather than looping forever — at that point a VPN or different network really is the only remaining option.

---

## 🇳🇬 MEXC — an exchange that's actually accessible from Nigeria

OKX has formally withdrawn Nigerian retail service (naira deposits, P2P, and most account functions disabled since August 2024 — withdrawals only). Binance and Bybit are commonly IP-geofenced for Nigerian users too. **MEXC has consistently remained fully accessible**, with an official, documented futures REST API — which is why it's offered here as a fourth exchange option, not as a workaround or a scraped/unofficial integration.

**A real bug, found from an actual run's log and now fixed:** the automatic exchange-fallback rotation was keyed off the wrong piece of state and could get stuck oscillating between just two exchanges forever, never reaching the third or fourth option no matter how many cycles passed — confirmed against a 31-cycle log where it bounced OKX→Binance→Bybit→Binance→Bybit... and never once tried MEXC. The rotation now correctly advances through the full chain (`okx → binance → bybit → mexc`, wrapping around) on every failure, transient or hard-blocked, so MEXC genuinely gets tried if you have it configured and the others are down.

**A second real bug, also found from a log, also fixed:** once you switch your active exchange (e.g. to MEXC, in the dashboard's Settings tab or `APEX_EXCHANGE`), two things were still quietly pinging the exchanges you'd moved away from:
1. `get_candles` treated ANY empty result on the active exchange — including the routine case of "this symbol just doesn't exist on MEXC's listing" — as a reason to go check OKX/Binance/Bybit too, generating constant DNS-failure noise from exchanges you deliberately weren't using anymore. It now only widens to other exchanges if the active one has had a genuine string of *real* connection failures (not just an empty/missing-symbol response), confirmed via its own failure counter.
2. The periodic health check (every ~120s) called `connection_status()` with no filter, which pings *all four* configured exchanges unconditionally regardless of which one is active. It now checks only the active exchange by default, and only widens to checking the others if that one specific check actually fails — at which point "is anything else reachable" is genuinely useful information, not noise.

Net effect: once you've switched to a working exchange, the log goes quiet for the ones you're not using — verified end-to-end with zero calls made to non-active exchanges during a full scan + signal-evaluation cycle.

```bash
export APEX_EXCHANGE=mexc
export MEXC_API_KEY=...
export MEXC_API_SECRET=...
python main.py --mode live
```

**Before you can trade:**
1. Create a MEXC account and **complete KYC verification** — this is mandatory. You can create an API key before KYC clears, but futures order-placement permissions on that key won't activate until verification is done.
2. Create the API key at MEXC → API Management, with futures trading permission enabled.
3. Same risk/position-sizing math applies as OKX/Binance/Bybit (`config/settings.py` → `RISK_PER_TRADE`, the TP1/TP2/TP3 ladder, drawdown guard) — MEXC isn't a separate, lesser-protected code path.

**Honest caveats specific to MEXC, so nothing here is oversold:**
- MEXC runs brief scheduled futures-API maintenance windows fairly often (typically under 35 minutes, announced in advance on their status page). During one, order placement/cancellation fails while account/position queries keep working — APEX's diagnostics recognise this pattern specifically (`exchange_rate_limit` category, auto-retried with backoff) rather than treating it as a real error or a code bug.
- The market-data ticker endpoint's behaviour when scanning *all* symbols at once (vs. one specific symbol) wasn't fully unambiguous in MEXC's public documentation at the time this was built. The feed code handles both possible response shapes defensively and logs a warning if it looks like only one symbol came back — **test this specifically against your own API key with a quick scan before relying on it for full-market scanning**, since this is the one piece I couldn't verify with full confidence from documentation alone.
- MEXC contract symbols use underscores (`BTC_USDT`) rather than the dash/SWAP format OKX uses or the bare concatenation Binance/Bybit use — handled automatically by the symbol converter, but worth knowing if you're reading raw logs.

### Rate limits (the other half of a noisy log)
Scanning 180+ symbols with many concurrent workers from a single IP is enough to trip most exchanges' per-IP rate limits on its own, independent of any geo-blocking — you'll see `HTTP 429` for this, not 451/403. Default concurrent scan workers is now `6` (was `12`), and the scanner adaptively drops to half or a third of that for the rest of the session if it detects 4+ or 10+ rate-limit hits in the last 60 seconds. Override the baseline with `APEX_SCAN_WORKERS` if your connection/exchange tolerates more or needs fewer.

---

## 📡 TradingView Integration

TradingView has no public API for reading a logged-in user's account balance — that's a web-app feature, not an exposed endpoint. Scraping it would mean storing your TradingView session credentials outside TradingView's own auth flow, which is a real account-security risk, so this integration deliberately doesn't do that. Instead:

### 1. Webhook signals (TradingView → APEX trades)
Any Pine Script strategy/indicator alert can POST to APEX:
```
POST http://your-server:8080/tradingview/webhook
Content-Type: application/json

{
  "secret":   "{{set APEX_TV_WEBHOOK_SECRET to match}}",
  "symbol":   "BTCUSDT",
  "side":     "buy",
  "price":    {{close}},
  "sl":       49000,
  "tp":       53000,
  "strategy": "my_pine_strategy"
}
```
**You must set `APEX_TV_WEBHOOK_SECRET`** — without it, the endpoint refuses every request, since TradingView webhooks are otherwise unauthenticated and anyone who finds your URL could inject fake trades. Run behind HTTPS (a reverse proxy or Cloudflare Tunnel) rather than exposing the raw port, since the secret travels in the request body.

### 2. Real account state (balance, currency, demo/live, privileges)
Pulled from each broker's own API, shown together in the dashboard's Agent tab → Connected Accounts:

| Adapter | What it needs | Notes |
|---|---|---|
| `crypto_exchange` | Already configured (your OKX/Binance/Bybit keys) | Just surfaces the same account your live executor uses |
| `deriv` | `DERIV_API_TOKEN` (+ optional `DERIV_APP_ID`) | Deriv is available to Nigerian users and has a real documented WebSocket API. Read-only — get a token at app.deriv.com → Settings → API token |
| `mt5_bridge` | `MT5_BRIDGE_MODE=native` (Windows + `pip install MetaTrader5`) or `MT5_BRIDGE_MODE=http` (your own bridge) | Covers Exness/HFM/FXTM and most retail forex brokers — they run on MT4/MT5, which has no public REST balance API of its own. Off by default; keeps APEX's "zero MT5" core promise intact unless you opt in. |

`GET /tradingview/account` returns all three, including the ones you haven't configured yet (with a clear "what to set" message instead of just disappearing).

---

## Scanner Scoring (16 Indicators)

Each symbol is scored 0–100 for BUY and SELL separately:

| Indicator | Weight |
|---|---|
| RSI 1H (deeply OS/OB) | +22 |
| S/R Breakout / Breakdown | +24 |
| MACD crossover (below zero = stronger) | +20 |
| Bollinger Band touch | +15 |
| 4H Alignment (HTF bias) | +16 |
| EMA stack (9/20/50 aligned) | +14 |
| Volume Surge (1.4×) | +11 |
| S/R Breakout Regime | +15 |
| Stochastic OS/OB | +8 |
| Fair Value Gap (FVG) | +8 |
| 24h momentum | +8 |
| Above/below EMA200 | +5 |

Confidence = score + HTF bonus/penalty + volume + regime + RSI overextension check + MACD histogram strength.

Grades: **S+** ≥65 · **A** ≥50 · **B** ≥38

---

## Strategies

### 1. MACD + EMA (30% weight)
- EMA(200) trend on 4H
- MACD crossover on 1H
- RSI must not be overextended

### 2. RSI Reversal (25% weight)
- RSI exits oversold/overbought zones
- Must touch Bollinger Band
- HTF RSI must agree

### 3. Breakout (25% weight)
- N-bar S/R breakout with volume confirmation
- FVG detection for institutional flow
- EMA trend alignment required

### 4. Scalp (20% weight)
- 15m EMA 8/21 crossover
- MACD histogram momentum
- HTF EMA bias filter

---

## Risk Management

- **Position sizing**: `risk_usd / stop_distance_pct` — never more than `RISK_PER_TRADE`% of equity
- **Drawdown guard**: halts new trades when daily DD > 5%
- **Correlation filter**: blocks correlated pairs (BTC+ETH, DOGE+SHIB, etc.)
- **Trailing stop**: takes over once TP2 has fired (so it never undercuts the planned TP1/TP2 SL ratchets below), steps at 0.5×ATR
- **Take-profit ladder (TP1 / TP2 / TP3)**: replaces the old single-TP design —
  - **TP1** at 1×ATR profit → closes 40% of the position, SL moves to breakeven
  - **TP2** at 2×ATR profit → closes 35% of what's left, SL moves up to TP1's price
  - **TP3** at 3×ATR profit (same distance as the old single TP) → closes everything remaining
  - Levels fire in order even if price gaps straight through more than one in a single tick (e.g. a stale price feed during a network blip). Live exchanges only support one native TP trigger per position, so TP3 is set as the exchange-side safety net while TP1/TP2 partial closes are managed by APEX in software while it's running — see "Known Limitations" below for what that means if APEX is offline when a level is hit.
- **News blackout**: 30-min window around high-impact events

---

## Self-Learning

Every 20 cycles the learner:
1. Reads last 50 closed trades
2. Computes per-strategy expectancy (win_rate × avg_win − loss_rate × avg_loss)
3. Rebalances strategy weights proportionally (floor 5%, cap 60%)
4. Perturbs indicator params with Gaussian noise (sigma decays 1%/cycle)
5. Saves to `data_store/learned_params.json`

---

## Known Limitations (read before going live)

- **Live SL/TP fill price is estimated, not exact.** When a live position's SL/TP fills on the exchange's own servers, APEX detects this by noticing the position disappeared from the next position-list poll — it doesn't currently call each exchange's trade-history endpoint to get the exact fill price, so the logged PnL for that trade is a close estimate using the last known mark price, not the exact executed price. Good enough for the journal/learner's purposes; if you need exact fill accounting, extend `core/engine.py::_detect_live_closes` to call OKX `/api/v5/trade/fills`, Binance `/fapi/v1/userTrades`, or Bybit `/v5/execution/list`.
- **Live position metadata (ATR, strategy name) lives in memory.** If APEX restarts while you have open live positions, the trailing-stop/partial-TP logic loses track of which strategy/ATR opened them until you take a fresh position. Existing SL/TP orders on the exchange remain in force regardless — your position is never unprotected, just temporarily un-trailed.
- **The MT5 bridge needs you to provide the bridge.** `MT5_BRIDGE_MODE=native` requires Windows; there's no official MT5 SDK for Linux/Mac. If APEX itself runs on a Linux VPS, use `MT5_BRIDGE_MODE=http` and run a small script on a Windows machine/VPS that posts your MT5 account_info() to a local endpoint — this isn't bundled because the exact setup depends on your MT5 environment.
- **The Watchdog auto-fixes runtime behaviour, not source code.** It will never silently rewrite anything in `execution/`. Real bugs land in the Patch Queue with a suggested diff for you to review and apply yourself.
- **Automatic exchange fallback on geo-block applies to market data, not live positions.** If OKX returns 451/403 on tickers/candles, the data feed switches to Binance/Bybit/MEXC automatically — but if you have an open LIVE position on OKX and OKX itself becomes unreachable, APEX does not (and should not) silently try to move that position to a different exchange; that's a real position with real money on a specific exchange, and moving it requires a decision only you can make (e.g. manually closing it via OKX's own app if APEX can't reach OKX at all).
- **TP1/TP2 partial closes only fire while APEX is running.** Exchanges support exactly one native take-profit trigger per position, so the exchange-side safety net is set to TP3 (the final target). If APEX is offline when price reaches TP1 or TP2, those partial exits simply don't happen — price runs to TP3 or SL with no partial profit-taking along the way, same as the old single-TP design behaved. This isn't a bug to "fix" so much as an inherent limit of exchange order APIs; if you need TP1/TP2 to fire with APEX offline, that would require native conditional multi-leg orders most exchanges don't expose via API.
- **MEXC's market-data ticker endpoint behaviour without a symbol filter wasn't fully unambiguous in the public docs at build time.** `data/feed.py::_tickers_mexc` handles both possible response shapes (a list of all symbols, or a single symbol object) and logs a warning if only one symbol comes back — test this against your own API key before relying on full-market scanning on MEXC specifically.
- **Strategies need real history to evaluate.** `LTF_BARS=260` (see "Why aren't any trades executing?" above) covers every strategy's minimum bar requirement with margin under normal conditions, but a brand-new exchange listing with less than ~230 hours of trading history simply won't have enough candles yet — `macd_ema`/`rsi_reversal`/`breakout` will correctly skip it (not enter the gate at all) until it accumulates enough history, same as any of these strategies would on a real chart.

---

*No MetaTrader5 required for the core engine. No hardcoded paths. No broker lock-in. Self-healing, TradingView-aware, and honest about what it can't do for you.*
