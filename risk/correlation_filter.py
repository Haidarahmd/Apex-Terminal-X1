"""
Correlation Filter — blocks correlated entries.
Crypto: prevents opening BTC+ETH simultaneously, etc.
Groups are auto-detected from symbol names.

Two layers:
  1. Static tag groups (original behaviour) — a fast, always-available
     baseline for known-correlated pairs (BTC/ETH, meme coins, metals, etc.)
  2. Dynamic returns correlation (new) — computed from each symbol's recent
     % price changes (fed in by the scanner every cycle). Catches pairs that
     aren't in the hardcoded list but are currently moving together (e.g.
     two alts that happen to be correlated in the current regime), and stops
     mattering automatically if/when they decorrelate again. Falls back
     silently to the static check alone when there isn't enough overlapping
     return history yet (cold start, illiquid symbol, etc).
"""
import math

# Default correlation groups (extend as needed)
CORRELATION_GROUPS = [
    ["BTC", "ETH"],       # Top crypto — highly correlated
    ["BNB", "SOL"],       # Alt L1 block
    ["DOGE", "SHIB"],     # Meme coins
    ["XAU", "XAG"],       # Metals
    ["EUR", "GBP"],       # EUR/GBP basket
]

DYNAMIC_CORR_THRESHOLD = 0.75   # |correlation| above this blocks the entry
DYNAMIC_CORR_MIN_SAMPLES = 20   # need at least this many overlapping return points


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < DYNAMIC_CORR_MIN_SAMPLES:
        return None
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = math.sqrt(var_a * var_b)
    if denom == 0:
        return None
    return cov / denom


class CorrelationFilter:
    def __init__(self, groups: list[list[str]] | None = None,
                 dynamic_threshold: float = DYNAMIC_CORR_THRESHOLD):
        self._groups = groups or CORRELATION_GROUPS
        self._open: set[str] = set()
        self.dynamic_threshold = dynamic_threshold

    def register_open(self, symbol: str):
        self._open.add(symbol)

    def register_close(self, symbol: str):
        self._open.discard(symbol)

    def allowed(self, symbol: str, returns_cache: dict[str, list[float]] | None = None) -> bool:
        """Returns False if a correlated symbol is already open — checked via
        the static tag groups first (cheap, always available), then via
        dynamic returns correlation if returns_cache has enough history for
        both symbols."""
        if not self._static_allowed(symbol):
            return False
        if returns_cache and not self._dynamic_allowed(symbol, returns_cache):
            return False
        return True

    def _static_allowed(self, symbol: str) -> bool:
        sym_up = symbol.upper()
        for group in self._groups:
            # Check if this symbol belongs to any group member
            sym_in_group = any(tag in sym_up for tag in group)
            if not sym_in_group:
                continue
            # Check if any open position shares the same group
            for open_sym in self._open:
                open_up = open_sym.upper()
                open_in_group = any(tag in open_up for tag in group)
                if open_in_group:
                    return False
        return True

    def _dynamic_allowed(self, symbol: str, returns_cache: dict[str, list[float]]) -> bool:
        sym_returns = returns_cache.get(symbol)
        if not sym_returns:
            return True
        for open_sym in self._open:
            open_returns = returns_cache.get(open_sym)
            if not open_returns:
                continue
            corr = _pearson(sym_returns, open_returns)
            if corr is not None and abs(corr) > self.dynamic_threshold:
                return False
        return True

    @property
    def open_symbols(self) -> set[str]:
        return set(self._open)
