import logging
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np

from config.settings import settings

logger = logging.getLogger(__name__)


class ActiveCorePool:
    """
    Dynamic core pool maintenance.
    Selects top volume and market cap stocks from a sector as core leaders.

    Beta correlation requires board_index_series (sector index history).
    Currently akshare does not provide a convenient API for this,
    so filtering is based on volume + market cap dimensions.
    Enable real Beta filtering by passing board_index_series when available.
    """

    @staticmethod
    def calculate_beta(stock_prices: pd.Series, index_prices: pd.Series) -> float:
        """Calculate Pearson correlation between stock and sector index."""
        if len(stock_prices) < 5 or len(index_prices) < 5:
            return 0.0
        try:
            df = pd.DataFrame({"stock": stock_prices, "index": index_prices}).dropna()
            if len(df) < 5:
                return 0.0
            corr = df["stock"].corr(df["index"])
            return float(corr) if not np.isnan(corr) else 0.0
        except Exception as e:
            logger.warning(f"Beta calculation failed: {e}")
            return 0.0

    @classmethod
    def filter_core_leaders(
        cls,
        board_cons_df: pd.DataFrame,
        board_index_series: Optional[pd.Series] = None
    ) -> List[Dict[str, Any]]:
        """
        Filter core leaders from sector constituents.
        :param board_cons_df: DataFrame with columns: code, name, amount
        :param board_index_series: Optional sector index close price series for Beta filtering
        """
        if board_cons_df is None or board_cons_df.empty:
            return []

        df = board_cons_df.copy()

        df["amount_billion"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0) / 1e8
        df["market_cap_billion"] = pd.to_numeric(
            df.get("total_market_cap", pd.Series(dtype=float)), errors="coerce"
        ).fillna(0) / 1e8

        amount_filtered = df[df["amount_billion"] >= settings.CORE_POOL_MIN_AMOUNT]

        if amount_filtered.empty:
            amount_filtered = df.sort_values(by="amount_billion", ascending=False).head(settings.CORE_POOL_TOP_AMOUNT)

        top_amount_codes = set(
            df.sort_values(by="amount_billion", ascending=False).head(settings.CORE_POOL_TOP_AMOUNT)["code"]
        )
        top_cap_codes = set(
            df.sort_values(by="market_cap_billion", ascending=False).head(settings.CORE_POOL_TOP_MARKET_CAP)["code"]
        )

        core_candidates = amount_filtered[
            amount_filtered["code"].isin(
                top_amount_codes | (top_cap_codes if df["market_cap_billion"].sum() > 0 else top_amount_codes)
            )
        ]

        results = []
        for _, row in core_candidates.iterrows():
            beta = None
            if board_index_series is not None:
                hist_prices = row.get("history_prices")
                if hist_prices is not None:
                    beta = cls.calculate_beta(pd.Series(hist_prices), board_index_series)

            if beta is not None and beta < settings.CORE_POOL_MIN_BETA:
                continue

            results.append({
                "code": str(row.get("code")),
                "name": str(row.get("name")),
                "price": float(row.get("price", 0.0)),
                "change_pct": float(row.get("change_pct", 0.0)),
                "amount_billion": round(float(row["amount_billion"]), 2),
                "market_cap_billion": round(float(row["market_cap_billion"]), 2),
                "beta": round(beta, 2) if beta is not None else None,
                "is_active_core": True
            })

        return results
