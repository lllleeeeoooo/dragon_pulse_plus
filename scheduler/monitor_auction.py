import time
import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from core.strategies import StrategyAnalyzer, MarketStyle
from core.holding_monitor import HoldingMonitor
from core.trade_calendar import is_trading_day
from llm.sell_advisor import DynamicSellAdvisor
from notifier.bark import bark_notifier
from database.services import HoldingManager, RecommendationManager

logger = logging.getLogger(__name__)

class _MonitorAuctionMixin:
    def _check_auction_phase(self):
        """
        集合竞价窗口（09:15-09:25）监控：
        每30秒采集一次全市场快照，追踪竞价量能趋势。
        09:25时生成竞价预判摘要并推送。
        """
        now = datetime.datetime.now()

        try:
            spot_df = DataFetcher.get_realtime_spot()
            if spot_df.empty:
                return

            # 采集竞价快照摘要
            high_open = spot_df[spot_df["change_pct"] >= 5.0] if "change_pct" in spot_df.columns else pd.DataFrame()
            low_open = spot_df[spot_df["change_pct"] <= -5.0] if "change_pct" in spot_df.columns else pd.DataFrame()

            total_amount = float(spot_df["amount"].sum()) if "amount" in spot_df.columns else 0
            avg_change = float(spot_df["change_pct"].mean()) if "change_pct" in spot_df.columns else 0

            snapshot = {
                "time": now.strftime("%H:%M:%S"),
                "total_amount_yi": round(total_amount / 1e8, 1),
                "high_open_count": len(high_open),
                "low_open_count": len(low_open),
                "avg_change": round(avg_change, 2),
            }
            self._auction_snapshots.append(snapshot)
            logger.debug(f"竞价采集 {snapshot['time']}: 高开{snapshot['high_open_count']}只 低开{snapshot['low_open_count']}只 均涨{snapshot['avg_change']}%")

            # 09:24-09:25推送竞价预判（只发一次）
            if now.minute >= 24 and not self._auction_summary_sent:
                self._auction_summary_sent = True
                self._send_auction_summary(spot_df)

        except Exception as e:
            logger.warning(f"竞价监控异常: {e}")


    def _send_auction_summary(self, spot_df: pd.DataFrame):
        """生成并推送竞价预判摘要"""
        snapshots = self._auction_snapshots
        if not snapshots:
            return

        # 量能趋势：对比首次和末次采集的成交额
        first_amt = snapshots[0]["total_amount_yi"]
        last_amt = snapshots[-1]["total_amount_yi"]
        amt_trend = "放量" if last_amt > first_amt * 1.5 else ("缩量" if last_amt < first_amt * 0.7 else "平稳")

        # 高开股趋势
        first_high = snapshots[0]["high_open_count"]
        last_high = snapshots[-1]["high_open_count"]
        high_trend = f"{first_high}→{last_high}只"

        # 均涨幅变化
        first_avg = snapshots[0]["avg_change"]
        last_avg = snapshots[-1]["avg_change"]

        # 找出竞价涨幅最高的标的（潜在龙头）
        top_stocks = ""
        if not spot_df.empty and "change_pct" in spot_df.columns:
            top = spot_df.nlargest(5, "change_pct")
            top_lines = []
            for _, r in top.iterrows():
                top_lines.append(f"{r.get('name','')}({r.get('code','')}) +{r.get('change_pct',0):.1f}%")
            top_stocks = " / ".join(top_lines)

        # 情绪预判
        if last_avg >= 1.0 and last_high >= 20:
            mood = "偏强，关注强势股能否维持高开"
        elif last_avg <= -0.5 or snapshots[-1]["low_open_count"] >= 20:
            mood = "偏弱，注意低开风险，谨慎参与"
        else:
            mood = "中性震荡，等待09:30方向确认"

        body = (
            f"竞价量能: {amt_trend}({first_amt}→{last_amt}亿)\n"
            f"高开>5%: {high_trend}\n"
            f"均涨幅: {first_avg}%→{last_avg}%\n"
            f"情绪预判: {mood}\n"
            f"竞价龙头: {top_stocks}"
        )

        logger.info(f"竞价预判摘要: {body}")
        bark_notifier.send(
            title=f"📊 [竞价预判] {'偏强' if last_avg >= 0.5 else '偏弱' if last_avg <= -0.3 else '中性'}",
            body=body,
            group="竞价指令",
            level="timeSensitive"
        )


    @staticmethod
    def _check_open_requirement(open_change_pct: float, requirement: str) -> bool:
        """
        验证开盘涨幅是否满足推荐标的的 open_requirement。
        解析格式如 "+3%~+6%", "高开>3%", "平开或低开" 等。
        """
        import re
        req = requirement.strip()
        if not req:
            return True

        # 匹配 "+3%~+6%" 或 "-1%~+2%" 格式
        range_match = re.search(r'([+-]?\d+\.?\d*)%?\s*[~～至到]\s*([+-]?\d+\.?\d*)%?', req)
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            return low <= open_change_pct <= high

        # 匹配 ">3%" 或 ">=2%" 格式
        gt_match = re.search(r'[>≥]\s*([+-]?\d+\.?\d*)%?', req)
        if gt_match:
            threshold = float(gt_match.group(1))
            return open_change_pct >= threshold

        # 匹配 "高开" 关键词
        if "高开" in req:
            return open_change_pct >= 2.0

        # 匹配 "平开" 或 "低开"
        if "平开" in req or "低开" in req:
            return open_change_pct <= 1.5

        # 无法解析的条件，放行
        return True


