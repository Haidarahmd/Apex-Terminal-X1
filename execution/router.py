"""Routes all order calls to paper or live executor based on MODE setting."""
import logging
from config.settings import MODE

logger = logging.getLogger(__name__)


class ExecutionRouter:
    def __init__(self, initial_balance: float = 10_000.0):
        self.mode = MODE
        if MODE == "paper":
            from execution.paper_executor import PaperExecutor
            self._ex = PaperExecutor(initial_balance)
            logger.info("[ROUTER] Paper mode — simulated fills")
        elif MODE == "live":
            from execution.live_executor import LiveExecutor
            self._ex = LiveExecutor()
            logger.info("[ROUTER] LIVE mode — real orders will be sent!")
        else:
            raise ValueError(f"Unknown MODE: '{MODE}'. Use 'paper' or 'live'.")

    def get_account(self) -> dict:
        return self._ex.get_account()

    def place_order(self, **kwargs) -> dict:
        return self._ex.place_order(**kwargs)

    def close_position(self, pos_id: str, price: float, reason: str = "manual") -> dict:
        return self._ex.close_position(pos_id, price, reason)

    def close_partial(self, pos_id: str, close_qty: float, price: float) -> float:
        if hasattr(self._ex, "close_partial"):
            return self._ex.close_partial(pos_id, close_qty, price)
        return 0.0

    def update_prices(self, prices: dict) -> list:
        if hasattr(self._ex, "update_prices"):
            return self._ex.update_prices(prices)
        return []

    def get_open_positions(self) -> list:
        if hasattr(self._ex, "get_open_positions"):
            return self._ex.get_open_positions()
        return []

    def get_closed_trades(self) -> list:
        if hasattr(self._ex, "get_closed_trades"):
            return self._ex.get_closed_trades()
        return []

    def update_sl(self, pos_id: str, new_sl: float):
        if hasattr(self._ex, "update_sl"):
            self._ex.update_sl(pos_id, new_sl)

    def reset_state(self, initial_balance: float = 10_000.0):
        """Reset paper account — no-op in live mode."""
        if hasattr(self._ex, "reset_state"):
            self._ex.reset_state(initial_balance)
