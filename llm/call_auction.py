import logging
from typing import Dict, Any, List
import pandas as pd

from llm.client import llm_client
from config.prompt_templates import CALL_AUCTION_SYSTEM_PROMPT, CALL_AUCTION_USER_TEMPLATE

logger = logging.getLogger(__name__)


class CallAuctionAnalyzer:
    """
    竞价观察与介入决策分析器 (09:26 触发)
    根据 09:25 集合竞价撮合数据，下达即时交易指令
    """

    @classmethod
    def run_auction_analysis(
        cls,
        trade_date: str,
        auction_df: pd.DataFrame,
        yesterday_zt_auction_yield: float = 1.5,
        predicted_sectors_summary: str = "",
        recommended_targets_summary: str = ""
    ) -> str:
        """
        分析 09:26 竞价开盘数据
        """
        logger.info(f"执行 {trade_date} 09:26 竞价观察与指令生成...")

        auction_data_text = "竞价数据为空"
        if auction_df is not None and not auction_df.empty:
            try:
                records = auction_df.head(15).to_dict(orient="records")
                lines = []
                for r in records:
                    amt_wan = round(float(r.get("amount", 0)) / 1e4, 2)
                    # 假定昨日全天成交额估算，帮 AI 计算出竞价量能占全天的百分比
                    pct_str = ""
                    lines.append(
                        f"- {r.get('name')}({r.get('code')}): 竞价开盘价 {r.get('price')}元, 竞价涨幅 {r.get('change_pct')}%, 竞价金额 {amt_wan}万"
                    )
                auction_data_text = "\n".join(lines)
            except Exception as e:
                logger.warning(f"格式化竞价文本失败: {e}")

        user_prompt = CALL_AUCTION_USER_TEMPLATE.format(
            trade_date=trade_date,
            yesterday_zt_auction_yield=round(yesterday_zt_auction_yield, 2),
            auction_data_text=auction_data_text,
            predicted_sector_auction_text=predicted_sectors_summary or "同盘前简报板块表现",
            recommended_targets_auction_text=recommended_targets_summary or "参见今日盘后复盘标的"
        )

        result = llm_client.generate(
            system_prompt=CALL_AUCTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            module="call_auction"
        )

        return result
