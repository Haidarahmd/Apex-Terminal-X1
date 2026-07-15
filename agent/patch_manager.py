"""
APEX Agent — Patch Manager
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Anything the Watchdog can't safely auto-fix (a real code bug, an
unrecognised exception, a repeated failure pattern) lands here as a
"pending patch" — visible in the dashboard, NOT auto-applied.

Design intent: an agent that can rewrite its own order-execution
logic with zero human checkpoint is exactly how a subtle bug turns
into a silent account-draining loop. So the boundary is:

  SAFE  (Watchdog applies immediately, no approval needed)
    - restart a crashed loop
    - switch data-feed exchange on repeated failure
    - widen retry/backoff
    - disable a single misbehaving strategy
    - quarantine + reset a corrupted cache/json file
    - skip a bad symbol for one cycle

  NEEDS REVIEW (queued here, human taps "Apply" in the dashboard)
    - anything touching strategies/, execution/, risk/ source code
    - repeated unknown errors (possible new bug class)
    - drawdown/auth issues (need a human decision, not a retry)

Patches are stored as plain dicts with a unified-diff-style preview
so you can see exactly what would change before approving anything.
"""
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

PATCH_QUEUE_FILE = DATA_DIR / "agent_patch_queue.json"
PATCH_LOG_FILE   = DATA_DIR / "agent_patch_log.json"

# Code paths the agent is allowed to even *propose* edits for.
# Anything outside this list is logged as "blocked — out of scope" and
# never proposed, even as a suggestion, to keep the blast radius small.
_EDITABLE_ROOTS = ("config/", "filters/", "risk/", "learning/", "agent/")
# These are explicitly NEVER auto-proposed for edits — execution path
# changes always require you to write/review the code yourself.
_FROZEN_ROOTS = ("execution/", "strategies/aggregator.py")

_lock = threading.Lock()


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def _save(path: Path, items: list[dict]):
    path.write_text(json.dumps(items, indent=2, default=str))


class PatchManager:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)

    # ── Queueing ──────────────────────────────────────────────────────────────
    def queue_patch(self, title: str, diagnosis: dict, suggestion: str,
                     target_file: str | None = None, severity: str = "critical") -> dict:
        """Add an item that needs human approval. Returns the queued record."""
        blocked = bool(target_file) and any(target_file.startswith(r) or r in target_file
                                             for r in _FROZEN_ROOTS)
        with _lock:
            queue = _load(PATCH_QUEUE_FILE)
            item = {
                "id":          uuid.uuid4().hex[:10],
                "title":       title,
                "severity":    severity,
                "diagnosis":   diagnosis,
                "suggestion":  suggestion,
                "target_file": target_file,
                "editable":    (not blocked) if target_file else False,
                "status":      "pending",
                "created_at":  datetime.now(tz=timezone.utc).isoformat(),
            }
            queue.append(item)
            _save(PATCH_QUEUE_FILE, queue)
        logger.warning("[PATCH] Queued for review: %s (id=%s)%s",
                        title, item["id"], " [execution-path — review only, no auto-apply]" if blocked else "")
        return item

    def list_pending(self) -> list[dict]:
        with _lock:
            return [p for p in _load(PATCH_QUEUE_FILE) if p["status"] == "pending"]

    def list_all(self) -> list[dict]:
        with _lock:
            return _load(PATCH_QUEUE_FILE)

    # ── Human decisions ──────────────────────────────────────────────────────
    def approve(self, patch_id: str, applier_fn=None) -> dict:
        """
        Mark a patch approved. If `applier_fn` is given and the patch is in
        an editable (non-frozen) area, it is called to actually apply the
        change; otherwise this just records the decision for you to action
        manually (the safe default for anything in execution/ or strategies/).
        """
        with _lock:
            queue = _load(PATCH_QUEUE_FILE)
            item = next((p for p in queue if p["id"] == patch_id), None)
            if not item:
                raise ValueError(f"No such patch: {patch_id}")

            item["status"] = "approved"
            item["approved_at"] = datetime.now(tz=timezone.utc).isoformat()
            applied = False
            if applier_fn and item.get("editable"):
                try:
                    applier_fn(item)
                    item["status"] = "applied"
                    applied = True
                except Exception as e:
                    item["status"] = "apply_failed"
                    item["apply_error"] = str(e)
            _save(PATCH_QUEUE_FILE, queue)

        self._log(item)
        logger.info("[PATCH] %s id=%s applied=%s", item["status"], patch_id, applied)
        return item

    def reject(self, patch_id: str, note: str = "") -> dict:
        with _lock:
            queue = _load(PATCH_QUEUE_FILE)
            item = next((p for p in queue if p["id"] == patch_id), None)
            if not item:
                raise ValueError(f"No such patch: {patch_id}")
            item["status"] = "rejected"
            item["note"] = note
            item["rejected_at"] = datetime.now(tz=timezone.utc).isoformat()
            _save(PATCH_QUEUE_FILE, queue)
        self._log(item)
        return item

    def _log(self, item: dict):
        with _lock:
            log = _load(PATCH_LOG_FILE)
            log.append(item)
            _save(PATCH_LOG_FILE, log[-500:])  # cap history

    # ── Housekeeping ──────────────────────────────────────────────────────────
    def prune_resolved(self, keep_pending_only: bool = True):
        with _lock:
            queue = _load(PATCH_QUEUE_FILE)
            if keep_pending_only:
                queue = [p for p in queue if p["status"] == "pending"]
            _save(PATCH_QUEUE_FILE, queue)
