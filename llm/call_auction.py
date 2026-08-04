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
        recommended_targets_summary: str = "",
        recommended_targets: List[Dict[str, Any]] = None,
        yesterday_zt_targets: List[Dict[str, Any]] = None,
        auction_prediction: str = ""
    ) -> str:
        """
        分析 09:26 竞价开盘数据。

        :param auction_df:            09:26 全市场快照（含竞价撮合价/涨幅/成交额/量比）
        :param recommended_targets:   昨日复盘推荐标的（含 code），逐只给出其真实竞价数据
                                      —— 这是 LLM 判断"是否符合买入条件"的关键输入。
        :param yesterday_zt_targets:  昨日连板/首板标的（含 code/lbc），逐只给出其真实竞价表现。
        :param auction_prediction:    规则引擎竞价预判（大盘走势+板块延续性），
                                      来自 monitor_auction._send_auction_summary 的缓存。
        """
        logger.info(f"执行 {trade_date} 09:26 竞价观察与指令生成...")

        # ---- 1. 昨日连板/首板标的的真实竞价数据（模板槽位：昨日连板与首板标的竞价表现）----
        auction_data_text = "竞价数据为空"
        if auction_df is not None and not auction_df.empty:
            try:
                lines = []
                for t in (yesterday_zt_targets or []):
                    code = str(t.get("code", "")).strip()
                    if not code:
                        continue
                    match = auction_df[auction_df["code"].astype(str) == code]
                    if not match.empty:
                        r = match.iloc[0]
                        amt_wan = round(float(r.get("amount", 0)) / 1e4, 2)
                        lbc = t.get("lbc", 1)
                        tag = f"{lbc}连板" if lbc >= 2 else "首板"
                        lines.append(
                            f"- [昨涨停-{tag}] {t.get('name', code)}({code}): 竞价涨幅 {r.get('change_pct')}%, "
                            f"竞价金额 {amt_wan}万, 量比 {r.get('volume_ratio', '?')}"
                        )
                    else:
                        lines.append(f"- [昨涨停] {t.get('name', code)}({code}): 快照未找到竞价数据")
                if not lines:
                    # 兜底：高开 Top15 作为市场环境（无昨涨停结构化数据时）
                    df_sorted = auction_df.sort_values(by="change_pct", ascending=False).head(15)
                    for _, r in df_sorted.iterrows():
                        amt_wan = round(float(r.get("amount", 0)) / 1e4, 2)
                        lines.append(
                            f"- {r.get('name')}({r.get('code')}): 竞价涨幅 {r.get('change_pct')}%, 竞价金额 {amt_wan}万"
                        )
                auction_data_text = "\n".join(lines)
            except Exception as e:
                logger.warning(f"格式化连板/首板竞价文本失败: {e}")

        # ---- 2. 昨日复盘推荐标的的真实竞价数据（模板槽位：昨日复盘推荐标的竞价情况）----
        rec_auction_lines = []
        for rec in (recommended_targets or []):
            code = str(rec.get("code", "")).strip()
            name = rec.get("name", code)
            req = rec.get("open_requirement", "") or ""
            if not code:
                continue
            match = auction_df[auction_df["code"].astype(str) == code] if auction_df is not None and not auction_df.empty else None
            if match is not None and not match.empty:
                r = match.iloc[0]
                amt_wan = round(float(r.get("amount", 0)) / 1e4, 2)
                rec_auction_lines.append(
                    f"- {name}({code}) 要求[{req}] → 实际竞价涨幅 {r.get('change_pct')}%, "
                    f"竞价金额 {amt_wan}万, 量比 {r.get('volume_ratio', '?')}"
                )
            else:
                rec_auction_lines.append(f"- {name}({code}) 要求[{req}] → 快照未找到竞价数据")
        recommended_targets_auction_text = "\n".join(rec_auction_lines) if rec_auction_lines else (recommended_targets_summary or "暂无推荐标的")

        user_prompt = CALL_AUCTION_USER_TEMPLATE.format(
            trade_date=trade_date,
            yesterday_zt_auction_yield=round(yesterday_zt_auction_yield, 2),
            auction_data_text=auction_data_text,
            predicted_sector_auction_text=predicted_sectors_summary or "同盘前简报板块表现",
            recommended_targets_auction_text=recommended_targets_auction_text,
            auction_prediction=auction_prediction or "暂无规则预判数据"
        )

        result = llm_client.generate(
            system_prompt=CALL_AUCTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            module="call_auction"
        )

        return result
