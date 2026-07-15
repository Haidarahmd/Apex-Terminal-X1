"""
APEX Self-Learner — online meta-optimiser for strategy weights + indicator params.

Every LEARNING_INTERVAL_CYCLES engine cycles:
  1. Reads recent closed trades from the journal.
  2. Computes per-strategy expectancy (win_rate × avg_RR) — both globally and,
     new, per market regime (TRENDING_UP / TRENDING_DOWN / RANGING /
     BREAKOUT), since a strategy that's mediocre overall can still be the
     best performer specifically in the regime a given candidate is in right
     now, and vice versa.
  3. Rebalances STRATEGY_WEIGHTS proportionally to expectancy.
  4. Perturbs indicator params with a Gaussian random walk.
  5. Persists everything to disk — survives restarts.

New vs v5 trading bot:
  - Sigma decays over time (exploration → exploitation)
  - Weight floor prevents any strategy from being zeroed out
  - Per-symbol weight modulation based on symbol scorer data
  - Per-regime weight modulation (get_weights(regime=...)) once a regime has
    enough closed trades to be statistically meaningful; falls back to the
    global blend otherwise so a thin regime bucket can't wildly overweight
    one strategy off a handful of trades.
"""
import json
import logging
import math
import os
import random
from pathlib import Path

from config.settings import (
    LEARNING_LOOKBACK, PARAM_EXPLORE_SIGMA, PARAMS_FILE,
    STRATEGY_WEIGHTS, DEFAULT_PARAMS,
)
from journal.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

_WEIGHT_FLOOR = 0.05   # no strategy ever drops below 5%
_WEIGHT_CAP   = 0.60   # no strategy dominates above 60%

_REGIME_MIN_TRADES = 8     # minimum closed trades in a regime bucket before trusting it at all
_REGIME_BLEND_FULL = 30    # trade count at which a regime bucket gets full weight (vs. blended with global)

PARAM_SPACE = {
    "ema_trend":      (50,  400, 200),
    "macd_fast":      (5,   20,   12),
    "macd_slow":      (15,  50,   26),
    "macd_signal":    (5,   15,    9),
    "atr_period":     (7,   28,   14),
    "rsi_period":     (7,   21,   14),
    "rsi_ob":         (60,  80,   70),
    "rsi_os":         (20,  40,   30),
    "bb_period":      (10,  50,   20),
    "bb_std":         (1.5, 3.0,  2.0),
    "lookback":       (10,  40,   20),
    "scalp_ema_fast": (5,   13,    8),
    "scalp_ema_slow": (13,  34,   21),
    "stoch_period":   (7,   21,   14),
}


class SelfLearner:
    def __init__(self, journal: TradeJournal):
        self.journal            = journal
        self._cycle             = 0
        self._weights           = dict(STRATEGY_WEIGHTS)
        self._weights_by_regime: dict[str, dict] = {}   # regime -> {strategy: weight}
        self._regime_trade_counts: dict[str, int] = {}  # regime -> n trades used for its bucket
        self._params             = dict(DEFAULT_PARAMS)
        self._sigma              = PARAM_EXPLORE_SIGMA
        self._load()

    def tick(self, cycle: int):
        from config.settings import LEARNING_INTERVAL_CYCLES, LEARNING_ENABLED
        if not LEARNING_ENABLED:
            return
        self._cycle = cycle
        if cycle % LEARNING_INTERVAL_CYCLES != 0:
            return
        self._update_weights()
        self._explore_params()
        self._save()

    def get_weights(self, regime: str | None = None) -> dict:
        """Global weights if regime is None / unknown / too thin on data.
        Otherwise blends the regime-specific weights in proportionally to how
        much data that regime bucket has, up to _REGIME_BLEND_FULL trades
        (full trust), so a regime with only just-over-the-minimum trades
        nudges the global weights rather than replacing them outright.

        If a UI weight override is active (set via /settings/weights API),
        it takes precedence over both learned and regime-blended weights,
        but is still normalised so the total always sums to 1.
        """
        # UI override — normalise and return directly
        override = getattr(self, "_weight_override", None)
        if override and isinstance(override, dict) and sum(override.values()) > 0:
            total = sum(override.values())
            return {k: round(v / total, 4) for k, v in override.items()}

        if not regime or regime not in self._weights_by_regime:
            return dict(self._weights)
        n = self._regime_trade_counts.get(regime, 0)
        if n < _REGIME_MIN_TRADES:
            return dict(self._weights)
        regime_w = self._weights_by_regime[regime]
        blend = min(1.0, n / _REGIME_BLEND_FULL)
        combined = {
            strat: blend * regime_w.get(strat, self._weights.get(strat, 0))
                   + (1 - blend) * self._weights.get(strat, 0)
            for strat in self._weights
        }
        total = sum(combined.values()) or 1.0
        return {k: round(v / total, 4) for k, v in combined.items()}

    def get_params(self) -> dict:
        return dict(self._params)

    # ── Weight update ─────────────────────────────────────────────────────────
    @staticmethod
    def _expectancy_weights(trades: list[dict], base_strategies: dict) -> dict | None:
        """Shared expectancy → normalised-weight computation, used both for
        the global blend and for each per-regime bucket."""
        by_strat: dict[str, list[float]] = {}
        for t in trades:
            strat = t.get("strategy", "")
            if not strat:
                continue
            pnl = float(t.get("pnl", 0))
            by_strat.setdefault(strat, []).append(pnl)

        expectancies: dict[str, float] = {}
        for strat, pnls in by_strat.items():
            wins  = [p for p in pnls if p > 0]
            losses= [p for p in pnls if p < 0]
            wr    = len(wins) / len(pnls) if pnls else 0
            aw    = sum(wins) / max(1, len(wins))
            al    = abs(sum(losses) / max(1, len(losses)))
            expectancies[strat] = wr * aw - (1 - wr) * al

        if not expectancies:
            return None

        min_e = min(expectancies.values())
        shifted = {s: e - min_e + 0.01 for s, e in expectancies.items()}
        total   = sum(shifted.values())

        new_weights = {}
        for strat in base_strategies:
            raw = shifted.get(strat, 0.01) / total
            new_weights[strat] = max(_WEIGHT_FLOOR, min(_WEIGHT_CAP, raw))

        s = sum(new_weights.values())
        return {k: round(v / s, 4) for k, v in new_weights.items()}

    def _update_weights(self):
        trades = self.journal.load_recent(LEARNING_LOOKBACK)
        if len(trades) < 5:
            return

        global_w = self._expectancy_weights(trades, self._weights)
        if global_w:
            self._weights = global_w
            logger.info("[LEARNER] Updated global weights: %s", self._weights)

        # Per-regime buckets — same expectancy math, just grouped by the
        # regime each trade was entered in (see journal "regime" column).
        by_regime: dict[str, list[dict]] = {}
        for t in trades:
            regime = t.get("regime", "")
            if not regime:
                continue
            by_regime.setdefault(regime, []).append(t)

        for regime, regime_trades in by_regime.items():
            self._regime_trade_counts[regime] = len(regime_trades)
            if len(regime_trades) < _REGIME_MIN_TRADES:
                continue
            regime_w = self._expectancy_weights(regime_trades, self._weights)
            if regime_w:
                self._weights_by_regime[regime] = regime_w
        if by_regime:
            logger.info("[LEARNER] Regime buckets updated: %s",
                        {r: self._regime_trade_counts.get(r, 0) for r in by_regime})

    # ── Param exploration ─────────────────────────────────────────────────────
    def _explore_params(self):
        # Sigma decays 1% per cycle (exploitation creeps in over time)
        self._sigma *= 0.99
        self._sigma  = max(self._sigma, 0.01)

        for param, (lo, hi, default) in PARAM_SPACE.items():
            cur = self._params.get(param, default)
            perturbation = random.gauss(0, self._sigma * (hi - lo))
            new_val      = cur + perturbation
            new_val      = max(lo, min(hi, new_val))
            # Keep integers as integers
            if isinstance(default, int):
                new_val = int(round(new_val))
            else:
                new_val = round(new_val, 3)
            self._params[param] = new_val

        logger.debug("[LEARNER] Params perturbed (sigma=%.4f): %s", self._sigma, self._params)

    # ── Persistence ───────────────────────────────────────────────────────────
    def _save(self):
        data = {
            "weights": self._weights,
            "weights_by_regime": self._weights_by_regime,
            "regime_trade_counts": self._regime_trade_counts,
            "params": self._params,
            "sigma": self._sigma,
        }
        try:
            with open(PARAMS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("[LEARNER] Save failed: %s", e)

    def _load(self):
        if not Path(PARAMS_FILE).exists():
            return
        try:
            with open(PARAMS_FILE) as f:
                data = json.load(f)
            self._weights             = data.get("weights", self._weights)
            self._weights_by_regime   = data.get("weights_by_regime", self._weights_by_regime)
            self._regime_trade_counts = data.get("regime_trade_counts", self._regime_trade_counts)
            self._params  = data.get("params",  self._params)
            self._sigma   = data.get("sigma",   self._sigma)
            logger.info("[LEARNER] Loaded from disk — weights: %s", self._weights)
        except Exception as e:
            logger.warning("[LEARNER] Load failed: %s", e)
