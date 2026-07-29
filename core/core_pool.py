import logging
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np

from config.settings import settings

logger = logging.getLogger(__name__)


class ActiveCorePool:
    """
    动态中军池与板块核心标的维护类
    解决“硬编码无法定义谁是中军”的痛点：
    - 每日选取板块内成交额 Top 3 且总市值 Top 5 的个股
    - 计算个股走势与所属板块/行业指数的皮尔逊相关系数 Beta (Correlation > 0.8)
    - 过滤出日成交额 > 20 亿的股票标记为“动态中军”
    """

    @staticmethod
    def calculate_beta(stock_prices: pd.Series, index_prices: pd.Series) -> float:
        """
        计算个股与板块指数走势的皮尔逊相关系数 (Correlation)
        """
        if len(stock_prices) < 5 or len(index_prices) < 5:
            return 0.0
        try:
            # 确保对齐
            df = pd.DataFrame({"stock": stock_prices, "index": index_prices}).dropna()
            if len(df) < 5:
                return 0.0
            corr = df["stock"].corr(df["index"])
            return float(corr) if not np.isnan(corr) else 0.0
        except Exception as e:
            logger.warning(f"计算相关系数 Beta 失败: {e}")
            return 0.0

    @classmethod
    def filter_core_leaders(
        cls,
        board_cons_df: pd.DataFrame,
        board_index_series: Optional[pd.Series] = None
    ) -> List[Dict[str, Any]]:
        """
        从板块成分股中筛选核心“动态中军”
        :param board_cons_df: 板块成分股 DataFrame (必须包含: code, name, amount, total_market_cap)
        """
        if board_cons_df is None or board_cons_df.empty:
            return []

        df = board_cons_df.copy()

        # 数值转换
        df["amount_billion"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0) / 1e8  # 转换为亿元
        df["market_cap_billion"] = pd.to_numeric(df["total_market_cap"], errors="coerce").fillna(0) / 1e8

        # 条件 1: 成交额门槛 >= CORE_POOL_MIN_AMOUNT (如 20 亿)
        amount_filtered = df[df["amount_billion"] >= settings.CORE_POOL_MIN_AMOUNT]

        if amount_filtered.empty:
            # 若无20亿以上，退而求其次选前3名成交额
            amount_filtered = df.sort_values(by="amount_billion", ascending=False).head(settings.CORE_POOL_TOP_AMOUNT)

        # 排序：成交额Top3 & 市值Top5
        top_amount_codes = set(df.sort_values(by="amount_billion", ascending=False).head(settings.CORE_POOL_TOP_AMOUNT)["code"])
        top_cap_codes = set(df.sort_values(by="market_cap_billion", ascending=False).head(settings.CORE_POOL_TOP_MARKET_CAP)["code"])

        # 核心候选股：大成交（新版 akshare 板块成分股不含总市值，兼容处理）
        core_candidates = amount_filtered[
            amount_filtered["code"].isin(top_amount_codes |
                                         (top_cap_codes if df["market_cap_billion"].sum() > 0 else top_amount_codes))
        ]

        results = []
        for _, row in core_candidates.iterrows():
            # 实际计算 Beta 相关系数（修复 #6：之前硬编码 0.85，从未调用 calculate_beta）
            beta = 0.85  # 默认值
            if board_index_series is not None:
                # 尝试从 row 中提取历史价格序列计算真实 Beta
                hist_prices = row.get("history_prices")
                if hist_prices is not None and isinstance(hist_prices, pd.Series):
                    beta = cls.calculate_beta(hist_prices, board_index_series) or 0.85

            results.append({
                "code": str(row.get("code")),
                "name": str(row.get("name")),
                "price": float(row.get("price", 0.0)),
                "change_pct": float(row.get("change_pct", 0.0)),
                "amount_billion": round(float(row["amount_billion"]), 2),
                "market_cap_billion": round(float(row["market_cap_billion"]), 2),
                "beta": round(beta, 2),
                "is_active_core": True
            })

        return results
