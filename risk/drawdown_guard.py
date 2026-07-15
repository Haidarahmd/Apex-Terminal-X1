"""Daily drawdown guard — halts trading when session loss exceeds limit."""


class DrawdownGuard:
    def __init__(self, max_dd_pct: float):
        self.max_dd = max_dd_pct

    def allowed(self, equity: float, start_equity: float) -> bool:
        if start_equity <= 0:
            return False
        return self.current_dd(equity, start_equity) < self.max_dd

    def current_dd(self, equity: float, start_equity: float) -> float:
        if start_equity <= 0:
            return 0.0
        return max(0.0, (start_equity - equity) / start_equity)
