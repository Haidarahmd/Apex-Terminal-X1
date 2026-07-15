"""
APEX Agent — Watchdog
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Supervises the TradingEngine. Wraps each cycle in error handling,
sends every exception through diagnostics.diagnose(), and either:

  • applies a SAFE fix immediately (see ACTIONS below) and keeps going
  • or escalates to PatchManager for your review (CODE_BUG, AUTH,
    unknown errors, or anything repeating past its retry budget)

It also runs periodic health checks (data feed reachable? exchange
auth valid? disk OK? journal file not corrupted?) independent of
whether the engine has thrown an error yet, so problems are caught
before they cause a missed trade or a stuck position.

This is the "self-healing" half of the agent. The "self-improving"
half is learning/self_learner.py, which already exists and is left
untouched here — the watchdog supervises *reliability*, the learner
optimises *strategy performance*. Keeping those concerns separate
means a bug in one can't quietly corrupt the other.
"""
import logging
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone

from agent.diagnostics import diagnose, Severity, Category
from agent.patch_manager import PatchManager
from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

# How many times the same category can be auto-fixed within the window
# before the watchdog gives up auto-fixing and escalates instead.
_RETRY_BUDGET = 5
_RETRY_WINDOW_SEC = 600  # 10 minutes


class Watchdog:
    def __init__(self, engine, notifier=None):
        self.engine = engine
        self.notifier = notifier or getattr(engine, "notifier", None)
        self.patches = PatchManager()

        self._failure_log: deque = deque(maxlen=500)
        self._category_hits: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self._disabled_strategies: set[str] = set()
        self._paused_symbols: dict[str, float] = {}   # symbol -> resume_ts
        self._consecutive_crashes = 0
        self._last_health_check = 0.0
        self._health_interval = 120  # seconds
        self._running = False

        # Safe auto-fix actions, keyed by diagnosis.fix_action
        self._actions = {
            "enable_doh_and_retry":   self._fix_enable_doh,
            "backoff_and_retry":      self._fix_backoff,
            "switch_exchange_on_geo_block": self._fix_switch_exchange,
            "skip_symbol_this_cycle": self._fix_skip_symbol,
            "quarantine_and_reset_file": self._fix_quarantine_file,
            "halt_live_trading":      self._fix_halt_live,
            "queue_patch_review":     self._fix_queue_review,
        }

    # ── Public entry point: replaces engine.run() ───────────────────────────
    def run_forever(self, poll_interval: int | None = None):
        from config.settings import POLL_INTERVAL
        interval = poll_interval or POLL_INTERVAL
        self._running = True
        logger.info("[WATCHDOG] Supervising engine. poll=%ds", interval)

        while self._running:
            cycle_start = time.monotonic()
            try:
                self.engine.process_cycle()
                self._consecutive_crashes = 0
            except KeyboardInterrupt:
                logger.info("[WATCHDOG] Stopped by user.")
                self._running = False
                break
            except Exception as e:
                self._handle_exception(e)

            self._maybe_health_check()
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.5, interval - elapsed))

    def stop(self):
        self._running = False

    # ── Exception handling ──────────────────────────────────────────────────
    def _handle_exception(self, exc: Exception):
        diagnosis = diagnose(exc=exc)
        self._record(diagnosis)

        logger.error("[WATCHDOG] %s | %s", diagnosis.category.value, diagnosis.message)
        logger.debug("[WATCHDOG] Traceback:\n%s", traceback.format_exc())

        over_budget = self._over_retry_budget(diagnosis.category.value)

        if diagnosis.auto_fixable and not over_budget:
            self._apply_fix(diagnosis)
        else:
            self._escalate(diagnosis, exc)

        self._consecutive_crashes += 1
        if self._consecutive_crashes >= 8:
            self._escalate_crash_loop()

    def _record(self, diagnosis):
        self._failure_log.append(diagnosis.to_dict())
        self._category_hits[diagnosis.category.value].append(time.time())

    def _over_retry_budget(self, category: str) -> bool:
        hits = self._category_hits[category]
        cutoff = time.time() - _RETRY_WINDOW_SEC
        recent = [h for h in hits if h >= cutoff]
        return len(recent) > _RETRY_BUDGET

    def _apply_fix(self, diagnosis):
        action = self._actions.get(diagnosis.fix_action)
        if not action:
            self._escalate(diagnosis, None)
            return
        try:
            action(diagnosis)
            logger.info("[WATCHDOG] Auto-fix applied: %s", diagnosis.fix_action)
        except Exception as e:
            logger.error("[WATCHDOG] Auto-fix itself failed (%s): %s", diagnosis.fix_action, e)
            self._escalate(diagnosis, e)

    def _escalate(self, diagnosis, exc):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else diagnosis.raw
        target_file = self._guess_target_file(tb)
        self.patches.queue_patch(
            title=diagnosis.message[:120],
            diagnosis=diagnosis.to_dict(),
            suggestion=self._build_suggestion(diagnosis, tb),
            target_file=target_file,
            severity=diagnosis.severity.value,
        )
        if self.notifier:
            try:
                self.notifier.error(f"[AGENT] {diagnosis.category.value}: {diagnosis.message}")
            except Exception:
                pass

    def _escalate_crash_loop(self):
        logger.critical("[WATCHDOG] %d consecutive crashes — escalating as crash loop.",
                         self._consecutive_crashes)
        self.patches.queue_patch(
            title="Engine is crash-looping (8+ consecutive cycle failures)",
            diagnosis={"category": "crash_loop", "severity": "fatal"},
            suggestion=("The engine has failed 8+ cycles in a row. This usually means a "
                        "single bad input (corrupted state file, bad strategy params) is "
                        "crashing every cycle rather than a transient network blip. "
                        "Recommended: check agent_patch_log.json for the repeating error, "
                        "fix the root cause, then restart the process."),
            target_file=None, severity="fatal",
        )
        if self.notifier:
            try:
                self.notifier.error("[AGENT] Engine crash-looping — manual check needed.")
            except Exception:
                pass
        self._consecutive_crashes = 0  # avoid spamming every cycle

    def _guess_target_file(self, tb: str) -> str | None:
        if not tb:
            return None
        for line in reversed(tb.splitlines()):
            if "apex_terminal" in line and ".py" in line:
                try:
                    path = line.split('"')[1]
                    return path.split("apex_terminal" + "/")[-1]
                except Exception:
                    continue
        return None

    def _build_suggestion(self, diagnosis, tb: str) -> str:
        if diagnosis.category == Category.CODE_BUG:
            return (f"Traceback below points to a likely code bug. Review the file/line, "
                    f"write a fix, and either patch it directly or use the dashboard's "
                    f"'apply' action if the file is in an editable area.\n\n{tb[-1500:]}")
        if diagnosis.category == Category.EXCHANGE_AUTH:
            return ("Live trading has been halted (see halt_live_trading). Check that your "
                    "API key/secret/passphrase env vars are correct, not expired, and that "
                    "your IP is whitelisted on the exchange if required. Restart after fixing.")
        return f"Unrecognised pattern — needs manual triage.\n\n{tb[-1500:]}" if tb else diagnosis.message

    # ── SAFE auto-fix actions ───────────────────────────────────────────────
    def _fix_enable_doh(self, diagnosis):
        import os
        os.environ["APEX_USE_DOH"] = "1"
        logger.warning("[WATCHDOG] DNS blocked — enabled DoH bypass for this process. "
                        "If this exchange is also IP-geofenced (not just DNS-filtered), "
                        "DoH alone won't fix it — see /agent/status for exchange reachability.")

    def _fix_switch_exchange(self, diagnosis):
        # The feed itself (data/feed.py) already detects 451/403 per-request
        # and falls back internally — this handler covers the case where a
        # geo-block exception still bubbles all the way up to the engine
        # cycle (e.g. it came from somewhere other than the feed, such as
        # the live executor's own account/order calls), and makes sure the
        # next cycle isn't stuck retrying the same blocked exchange.
        feed = getattr(self.engine, "feed", None)
        if feed is None:
            return
        current = feed.active_exchange()
        if hasattr(feed, "_blocked_exchanges"):
            feed._blocked_exchanges.add(current)
        switched = feed._try_fallback(reason="geo-block detected at engine level") \
            if hasattr(feed, "_try_fallback") else False
        if switched:
            logger.warning("[WATCHDOG] %s is geo-blocked — switched active exchange to %s",
                           current.upper(), feed.active_exchange().upper())
        else:
            logger.error("[WATCHDOG] %s is geo-blocked and no fallback exchange is available "
                        "(all configured exchanges are blocked or already tried). "
                        "A VPN or different network is the only remaining option — "
                        "this is your call to make, not something APEX automates.")

    def _fix_backoff(self, diagnosis):
        mult = diagnosis.fix_args.get("multiplier", 1.5)
        time.sleep(min(30, 2 * mult))

    def _fix_skip_symbol(self, diagnosis):
        # No-op placeholder: the engine's own try/except around per-symbol
        # work already skips bad symbols; this exists so repeated data
        # gaps for ONE symbol don't get miscounted as engine-wide crashes.
        pass

    def _fix_quarantine_file(self, diagnosis):
        import json
        import re
        m = re.search(r"data_store/([\w\-.]+\.json)", diagnosis.raw or "")
        if not m:
            return
        bad_file = DATA_DIR / m.group(1)
        if bad_file.exists():
            quarantine = DATA_DIR / f"{bad_file.name}.corrupt.{int(time.time())}"
            bad_file.rename(quarantine)
            logger.warning("[WATCHDOG] Quarantined corrupted file %s -> %s", bad_file, quarantine)

    def _fix_halt_live(self, diagnosis):
        from config import settings
        settings.MODE = "paper"
        logger.critical("[WATCHDOG] Exchange auth failed — forced MODE to 'paper' to stop "
                         "any further live orders. Fix credentials and restart to resume live.")

    def _fix_queue_review(self, diagnosis):
        self.patches.queue_patch(
            title=diagnosis.message[:120],
            diagnosis=diagnosis.to_dict(),
            suggestion="Auto-classified as needing review; no safe auto-fix exists for this pattern.",
            target_file=None, severity=diagnosis.severity.value,
        )

    # ── Health checks (proactive, not just reactive) ────────────────────────
    def _maybe_health_check(self):
        now = time.time()
        if now - self._last_health_check < self._health_interval:
            return
        self._last_health_check = now
        self.run_health_check()

    def run_health_check(self) -> dict:
        from agent.healthcheck import run_all_checks
        results = run_all_checks(self.engine)
        for r in results:
            if not r["ok"]:
                logger.warning("[WATCHDOG] Health check failed: %s — %s", r["name"], r["detail"])
        return {"ts": datetime.now(tz=timezone.utc).isoformat(), "checks": results}

    # ── Status for the dashboard ────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "recent_failures":      list(self._failure_log)[-30:],
            "category_counts":      {k: len(v) for k, v in self._category_hits.items()},
            "consecutive_crashes":  self._consecutive_crashes,
            "pending_patches":      self.patches.list_pending(),
        }
