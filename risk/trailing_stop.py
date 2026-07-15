"""
APEX Trailing Stop Manager — paper-trading compatible.
Updates stop-loss on open positions tracked in the paper executor.
For live execution, override _send_sl to call your broker API.
"""
import logging
import math

logger = logging.getLogger(__name__)

_MIN_MOVE_FRACTION = 0.5


def _round_to_tick(value: float, tick_size: float) -> float:
    if tick_size <= 0:
        return value
    return round(math.floor(value / tick_size) * tick_size, 10)


class TrailingStopManager:
    def __init__(self, activation_atr: float = 1.0, step_atr: float = 0.5):
        self.activation_atr = activation_atr
        self.step_atr       = step_atr
        self._be_locked: set[str] = set()

    def update(self, position: dict, atr_val: float) -> tuple[bool, float]:
        """
        Returns (updated: bool, new_sl: float).
        Position dict keys: id, side, entry_price, current_price, sl, tick_size
        """
        if atr_val <= 0:
            return False, position.get("sl", 0)

        pos_id      = position["id"]
        side        = position["side"]
        entry_price = position["entry_price"]
        cur_price   = position["current_price"]
        current_sl  = position.get("sl", 0.0)
        tick_size   = position.get("tick_size", 0.00001)

        activation_dist = atr_val * self.activation_atr
        step_dist       = atr_val * self.step_atr
        min_move        = step_dist * _MIN_MOVE_FRACTION

        if side == "buy":
            profit_dist = cur_price - entry_price
            if profit_dist < activation_dist:
                return False, current_sl

            # Lock breakeven
            be_price = entry_price + tick_size
            if pos_id not in self._be_locked and current_sl < be_price:
                self._be_locked.add(pos_id)
                logger.info("[TRAIL] BUY %s — locking BE SL=%.8f", pos_id, be_price)
                return True, be_price

            new_sl = _round_to_tick(cur_price - step_dist, tick_size)
            if new_sl - current_sl < min_move or new_sl <= current_sl:
                return False, current_sl
            logger.info("[TRAIL] BUY %s — SL %.8f → %.8f", pos_id, current_sl, new_sl)
            return True, new_sl

        else:  # sell
            profit_dist = entry_price - cur_price
            if profit_dist < activation_dist:
                return False, current_sl

            be_price = entry_price - tick_size
            if pos_id not in self._be_locked and (current_sl == 0 or current_sl > be_price):
                self._be_locked.add(pos_id)
                logger.info("[TRAIL] SELL %s — locking BE SL=%.8f", pos_id, be_price)
                return True, be_price

            new_sl = _round_to_tick(cur_price + step_dist, tick_size)
            if current_sl != 0 and (current_sl - new_sl < min_move or new_sl >= current_sl):
                return False, current_sl
            logger.info("[TRAIL] SELL %s — SL %.8f → %.8f", pos_id, current_sl, new_sl)
            return True, new_sl
