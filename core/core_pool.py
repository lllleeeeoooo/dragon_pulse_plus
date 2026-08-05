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
        """Calculate Pearson correlation between stock and sector index.
        审计🟡③：两序列若带日期索引则按日期对齐；否则按"最近 N 天"对齐，
        避免停牌/新股天数不足时按位置从头错位导致相关性失真。"""
        if len(stock_prices) < 5 or len(index_prices) < 5:
            return 0.0
        try:
            s_idx = getattr(stock_prices, "index", None)
            i_idx = getattr(index_prices, "index", None)
            if isinstance(s_idx, pd.DatetimeIndex) and isinstance(i_idx, pd.DatetimeIndex):
                df = pd.DataFrame({"stock": stock_prices, "index": index_prices}).dropna()
            else:
                n = min(len(stock_prices), len(index_prices))
                df = pd.DataFrame({
                    "stock": stock_prices.tail(n).reset_index(drop=True),
                    "index": index_prices.tail(n).reset_index(drop=True),
                }).dropna()
            if len(df) < 5:
                return 0.0
            corr = df["stock"].corr(df["index"])
            return float(corr) if not np.isnan(corr) else 0.0
        except Exception as e:
            logger.warning(f"Beta calculation failed: {e}")
            return 0.0

    @staticmethod
    def _get_market_index_series() -> Optional[pd.Series]:
        """获取上证指数近30日收盘序列作为市场代理（Beta 过滤的市场基准），失败返回 None（不启用过滤）"""
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol="sh000001")
            if df is not None and not df.empty:
                closes = pd.to_numeric(df["close"], errors="coerce").dropna().tail(30)
                if len(closes) >= 5:
                    return closes.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"获取上证指数序列失败: {e}")
        return None

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

        # 未显式传入板块指数时，用上证指数近30日收盘作为市场代理（Beta 过滤的市场基准）
        if board_index_series is None:
            board_index_series = cls._get_market_index_series()

        results = []
        for _, row in core_candidates.iterrows():
            beta = None
            if board_index_series is not None:
                hist_prices = row.get("history_prices")
                if hist_prices is None:
                    # 真实实现：拉取个股近30日收盘价，计算与市场指数的相关性
                    from data.fetcher import DataFetcher
                    hist_prices = DataFetcher.get_stock_daily_closes(str(row.get("code")))
                if hist_prices:
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
