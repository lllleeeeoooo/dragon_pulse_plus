import logging
from typing import Dict, Any, Optional
import pandas as pd

logger = logging.getLogger(__name__)


class EmotionVector:
    """
    情绪多维向量评分类
    计算短线情绪的 6 个维度：
    1. 高度 (Height)：市场最高连板天数
    2. 宽度 (Breadth)：涨停家数 - 跌停家数
    3. 反馈 (Yield)：昨日涨停个股今日开盘平均溢价率 (最重要: 节点赚钱效应)
    4. 力度 (Force)：全市场封单资金 / 市场总成交额
    5. 承接 (Support)：炸板率 (炸板数 / (涨停数 + 炸板数))
    6. 破规胆量 (YidongBravery)：触发监管异动后，资金次日敢于继续封板/强行突破的晋级率
    """

    @staticmethod
    def calculate(
        zt_df: pd.DataFrame,
        zhaban_df: pd.DataFrame,
        dt_df: pd.DataFrame,
        total_market_amount: float = 1e12,
        yesterday_zt_avg_premium: float = 1.5,
        yidong_stocks_next_day_promoted_rate: float = 50.0  # 触异动后次日强行晋级率 (%)
    ) -> Dict[str, Any]:
        """
        根据盘后或盘中抓取的涨跌停数据计算 5D 情绪向量
        """
        zt_count = len(zt_df) if zt_df is not None and not zt_df.empty else 0
        zhaban_count = len(zhaban_df) if zhaban_df is not None and not zhaban_df.empty else 0
        dt_count = len(dt_df) if dt_df is not None and not dt_df.empty else 0

        # 1. 高度 (Height)
        height = 0
        if zt_count > 0 and "lbc" in zt_df.columns:
            try:
                # 转换连板数为数值
                height = int(pd.to_numeric(zt_df["lbc"], errors="coerce").fillna(1).max())
            except Exception as e:
                logger.warning(f"解析最高连板数失败: {e}")
                height = 1

        # 2. 宽度 (Breadth)
        breadth = zt_count - dt_count

        # 3. 反馈 (Yield)
        yield_rate = float(yesterday_zt_avg_premium)

        # 4. 力度 (Force)
        seal_force_ratio = 0.0
        if zt_count > 0 and "seal_amount" in zt_df.columns:
            try:
                total_seal_amount = pd.to_numeric(zt_df["seal_amount"], errors="coerce").fillna(0).sum()
                if total_market_amount > 0:
                    seal_force_ratio = round((total_seal_amount / total_market_amount) * 100, 2)
            except Exception as e:
                logger.warning(f"计算封单力度失败: {e}")

        # 5. 承接/炸板率 (Support)
        # 真炸板 = 炸板池中不在涨停池的股票（扣除炸板后回封成功的）
        true_zhaban = zhaban_count
        if zt_df is not None and not zt_df.empty and "code" in zt_df.columns and \
           zhaban_df is not None and not zhaban_df.empty and "code" in zhaban_df.columns:
            zt_codes = set(zt_df["code"].astype(str))
            zhaban_codes = set(zhaban_df["code"].astype(str))
            true_zhaban = len(zhaban_codes - zt_codes)
        total_seal_attempts = zt_count + true_zhaban
        zhaban_rate = round((true_zhaban / total_seal_attempts * 100), 2) if total_seal_attempts > 0 else 0.0

        # 6. 破规胆量维度 (YidongBravery)
        yidong_bravery = float(yidong_stocks_next_day_promoted_rate)

        # 7. 综合情绪指数得分 (0 ~ 100分)
        # 权重设定：反馈 25%, 破规胆量 20%, 宽度 20%, 高度 15%, 承接 10%, 力度 10%
        # height得分：分段线性，低板敏感高板平缓
        # 1板=15, 2板=30, 3板=50, 4板=65, 5板=78, 6板=88, 7板=95, 8板+=100
        _height_map = {0: 0, 1: 15, 2: 30, 3: 50, 4: 65, 5: 78, 6: 88, 7: 95}
        score_height = _height_map.get(height, 100) if height <= 7 else 100
        score_breadth = max(min((breadth + 40) * 1.0, 100), 0)
        # yield得分：以0%为中性锚点(50分), -3%=0分, +4%=100分
        score_yield = max(min((yield_rate + 3) * (100 / 7), 100), 0) if yield_rate < 0 else max(min(50 + yield_rate * (50 / 4), 100), 50)
        score_support = max(100 - zhaban_rate * 2.5, 0)
        score_force = min(seal_force_ratio * 500, 100)
        score_bravery = min(max(yidong_bravery, 0), 100)

        sentiment_index = round(
            score_yield * 0.25 +
            score_bravery * 0.20 +
            score_breadth * 0.20 +
            score_height * 0.15 +
            score_support * 0.10 +
            score_force * 0.10,
            1
        )

        return {
            "height": height,
            "breadth": breadth,
            "zt_count": zt_count,
            "dt_count": dt_count,
            "zhaban_count": zhaban_count,
            "yield_rate": yield_rate,
            "seal_force_ratio": seal_force_ratio,
            "zhaban_rate": zhaban_rate,
            "yidong_bravery": yidong_bravery,
            "sentiment_index": sentiment_index
        }

    @staticmethod
    def compute_intraday_sentiment(height: int, zt_count: int, dt_count: int,
                                     zhaban_rate: float, premium: float) -> float:
        """
        盘中快速情绪分计算（与 _classify_intraday_style 和 _log_startup_report 共用）。
        """
        from config.settings import settings
        # yield得分：0%=50分(中性锚点)
        score_premium = max(min((premium + 3) * (100 / 7), 100), 0) if premium < 0 else max(min(50 + premium * (50 / 4), 100), 50)
        # height得分：分段线性
        _hm = {0: 0, 1: 15, 2: 30, 3: 50, 4: 65, 5: 78, 6: 88, 7: 95}
        score_height = _hm.get(height, 100) if height <= 7 else 100
        score_breadth = max(min(((zt_count - dt_count) + 40) * 1.0, 100), 0)
        score_support = max(100 - zhaban_rate * 2.5, 0)
        return round(
            score_premium * settings.PREMIUM_WEIGHT +
            score_breadth * settings.BREADTH_WEIGHT +
            score_height * settings.HEIGHT_WEIGHT +
            score_support * settings.SUPPORT_WEIGHT, 1
        )


