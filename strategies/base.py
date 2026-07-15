"""Base class for all APEX strategies."""
from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None) -> dict | None:
        """
        Returns dict with keys: side ('buy'|'sell'), price, atr, strategy
        or None if no signal.
        """
        ...
