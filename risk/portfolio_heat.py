"""
APEX Portfolio Heat Tracker — caps AGGREGATE risk across all open positions.

Why this exists: MAX_OPEN_POSITIONS caps position COUNT, and
CorrelationFilter blocks specific known-correlated PAIRS — but neither one
caps total risk-at-stake. Five positions in symbols that aren't in any
correlation group can still all be "risk-on crypto alts" that move together
in a market-wide flush, so nominal per-trade risk of 1% each can behave like
5% correlated risk in exactly the conditions where it matters most.

This tracks actual risk-usd committed at entry (post adaptive-risk sizing,
so it reflects what's really at stake, not the flat config value) and blocks
new entries once the total would exceed MAX_PORTFOLIO_HEAT_PCT of equity —
independent of, and in addition to, the correlation-group and position-count
caps.
"""
import logging

logger = logging.getLogger(__name__)


class PortfolioHeatTracker:
    def __init__(self, max_heat_pct: float):
        self.max_heat_pct = max_heat_pct
        self._risk_usd: dict[str, float] = {}   # symbol -> risk_usd committed at entry

    def register_open(self, symbol: str, risk_usd: float):
        self._risk_usd[symbol] = max(0.0, risk_usd)

    def register_close(self, symbol: str):
        self._risk_usd.pop(symbol, None)

    def total_heat_usd(self) -> float:
        return sum(self._risk_usd.values())

    def allowed(self, equity: float, new_risk_usd: float) -> bool:
        """Returns False if adding new_risk_usd would push total committed
        risk above max_heat_pct of equity."""
        if equity <= 0:
            return False
        projected = self.total_heat_usd() + max(0.0, new_risk_usd)
        cap = equity * self.max_heat_pct
        if projected > cap:
            logger.info("[HEAT] Blocked — projected heat %.2f > cap %.2f (%.1f%% of equity %.2f)",
                        projected, cap, self.max_heat_pct * 100, equity)
            return False
        return True

    def summary(self, equity: float) -> dict:
        total = self.total_heat_usd()
        return {
            "total_risk_usd": round(total, 2),
            "cap_usd":         round(equity * self.max_heat_pct, 2),
            "heat_pct_of_equity": round(total / equity * 100, 2) if equity > 0 else 0.0,
            "positions": dict(self._risk_usd),
        }
