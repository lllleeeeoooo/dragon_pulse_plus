"""
集合竞价阶段监控：
- 09:15-09:25 每30秒采集全市场快照
- 09:24-09:25 结合昨日数据生成大盘走势预判 + 热门板块延续/退潮预判
"""
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

# 模块级缓存：竞价预判摘要，供 09:26 LLM 竞价指令读取
_auction_prediction_cache: str = ""


class _MonitorAuctionMixin:
    def _check_auction_phase(self):
        """
        集合竞价窗口（09:15-09:25）监控：
        每30秒采集一次全市场快照，追踪竞价量能趋势。
        09:25时生成竞价预判摘要并推送。
        """
        now = datetime.datetime.now()

        try:
            spot_df = self._DF.get_realtime_spot()
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
        """生成竞价预判摘要：量能趋势 + 大盘走势预判 + 板块延续/退潮预判"""
        snapshots = self._auction_snapshots
        if not snapshots:
            return

        # =====================================================================
        # 1. 竞价量能趋势
        # =====================================================================
        first_amt = snapshots[0]["total_amount_yi"]
        last_amt = snapshots[-1]["total_amount_yi"]
        if last_amt > first_amt * 1.5:
            amt_trend = "放量"
        elif last_amt < first_amt * 0.7:
            amt_trend = "缩量"
        else:
            amt_trend = "平稳"

        first_high = snapshots[0]["high_open_count"]
        last_high = snapshots[-1]["high_open_count"]
        first_avg = snapshots[0]["avg_change"]
        last_avg = snapshots[-1]["avg_change"]

        # =====================================================================
        # 2. 获取昨日数据（大盘 + 情绪周期 + 热门板块）
        # =====================================================================
        yesterday_data = self._get_yesterday_context()

        # =====================================================================
        # 3. 大盘走势预判
        # =====================================================================
        market_prediction = self._predict_market_trend(
            last_avg, last_high, amt_trend, yesterday_data
        )

        # =====================================================================
        # 4. 板块延续/退潮预判
        # =====================================================================
        sector_predictions = self._predict_sector_continuation(
            spot_df, yesterday_data
        )

        # =====================================================================
        # 5. 竞价龙头
        # =====================================================================
        top_stocks = ""
        if not spot_df.empty and "change_pct" in spot_df.columns:
            top = spot_df.nlargest(5, "change_pct")
            top_lines = []
            for _, r in top.iterrows():
                top_lines.append(f"{r.get('name','')}({r.get('code','')}) +{r.get('change_pct',0):.1f}%")
            top_stocks = " / ".join(top_lines)

        # =====================================================================
        # 6. 组装推送
        # =====================================================================
        yest_idx = yesterday_data.get("index_line", "")
        yest_cycle = yesterday_data.get("cycle_line", "")

        body = (
            f"📊 竞价量能: {amt_trend}({first_amt}→{last_amt}亿)\n"
            f"   高开>5%: {first_high}→{last_high}只 | 均涨幅: {first_avg}%→{last_avg}%\n"
            f"{yest_idx}"
            f"{yest_cycle}"
            f"\n📈 大盘预判: {market_prediction}\n"
        )

        if sector_predictions:
            body += f"\n🔥 板块预判:\n{sector_predictions}"

        body += f"\n🏆 竞价龙头: {top_stocks}"

        logger.info(f"竞价预判摘要:\n{body}")
        # 缓存供 09:26 LLM 竞价指令读取
        global _auction_prediction_cache
        _auction_prediction_cache = (
            f"大盘预判: {market_prediction}\n"
            f"板块预判:\n{sector_predictions}" if sector_predictions else ""
            f"竞价龙头: {top_stocks}"
        )
        bark_notifier.send(
            title=f"📊 [竞价预判] {market_prediction[:12]}",
            body=body,
            group="竞价指令",
            level="timeSensitive"
        )

    # =====================================================================
    # 昨日数据获取
    # =====================================================================

    def _get_yesterday_context(self) -> dict:
        """获取昨日大盘、情绪周期、热门板块数据用于竞价预判"""
        import datetime as _dt
        result = {}

        # --- 昨日大盘指数 ---
        try:
            from database import MarketIndexManager
            idx = MarketIndexManager.get_latest()
            if idx:
                sh = idx.get("sh_change_pct", 0)
                sz = idx.get("sz_change_pct", 0)
                gem = idx.get("gem_change_pct", 0)
                result["sh_change"] = sh
                result["index_line"] = (
                    f"\n📉 昨日大盘: 上证{sh:+.2f}% 深证{sz:+.2f}% 创业板{gem:+.2f}%"
                )
        except Exception:
            pass

        # --- 昨日情绪周期 ---
        try:
            from database import SentimentManager
            recent = SentimentManager.get_recent_sentiments(days_lookback=1)
            if recent:
                s = recent[0]
                cycle = s.get("cycle_stage", "")
                si = s.get("sentiment_index", 0)
                height = s.get("height", 0)
                result["cycle"] = cycle
                result["sentiment_index"] = si
                result["height"] = height
                result["cycle_line"] = (
                    f"\n📊 昨日情绪: {cycle} 情绪{si}分 最高{height}板"
                )
        except Exception:
            pass

        # --- 昨日热门板块 ---
        try:
            from database import SectorStrengthManager
            sectors = SectorStrengthManager.get_hot_sectors(top_n=5)
            if sectors:
                result["hot_sectors"] = [
                    {"name": s["sector"], "zt_count": s["zt_count"], "accel": s["accel"]}
                    for s in sectors
                ]
        except Exception:
            pass

        return result

    # =====================================================================
    # 大盘走势预判
    # =====================================================================

    def _predict_market_trend(self, avg_change: float, high_count: int,
                               amt_trend: str, yesterday: dict) -> str:
        """
        结合竞价数据和昨日情绪周期，预判今日大盘走势。

        规则：
        - 放量高开 + 冰点/启动 → 高开高走
        - 放量高开 + 高潮 → 警惕高开低走
        - 缩量低开 + 退潮 → 低开低走
        - 缩量低开 + 冰点 → 低开高走可期
        - 其他 → 中性震荡
        """
        cycle = yesterday.get("cycle", "")
        sh_yest = yesterday.get("sh_change", 0)
        is_strong_open = avg_change >= 0.5 and high_count >= 20 and amt_trend == "放量"
        is_weak_open = avg_change <= -0.3

        # 强开盘
        if is_strong_open:
            if cycle == "冰点":
                return "高开高走概率大 🔥（冰点后放量高开是反转信号，积极做多）"
            elif cycle == "启动":
                return "高开高走概率大 ✅（启动期放量确认，顺势追龙头）"
            elif cycle == "发酵":
                return "高开震荡走高 ✅（发酵期中继，持股待涨）"
            elif cycle == "高潮":
                return "⚠️ 警惕高开低走（高潮末期，高开往往是出货窗口，不宜追高）"
            elif cycle == "退潮":
                return "⚠️ 高开可能是诱多（退潮期反弹即卖点，谨慎追涨）"
            else:
                return "高开偏强 ✅（放量高开+高开>20只，积极跟随）"

        # 弱开盘
        if is_weak_open:
            if cycle == "冰点":
                return "低开高走可期 🔄（冰点低开往往是最后一跌，关注反转）"
            elif cycle == "退潮":
                return "低开低走风险 ⚠️（退潮确认，防守为主）"
            elif cycle == "高潮":
                return "低开低走风险 ⚠️（高潮转退潮信号，减仓观望）"
            elif cycle == "发酵":
                return "低开关注修复 🔄（发酵期分歧，关注弱转强标的）"
            else:
                return "低开偏弱 ⚠️（谨慎参与，等待方向确认）"

        # 中性开盘
        if sh_yest > 0 and cycle in ("冰点", "启动"):
            return "震荡偏强 ✅（昨日收阳+上升周期，回踩是买点）"
        elif sh_yest < -0.5 and cycle in ("高潮", "退潮"):
            return "震荡偏弱 ⚠️（昨日收阴+下降周期，反弹减仓）"
        else:
            return "中性震荡 ➡️（无明确方向，等待09:30确认）"

    # =====================================================================
    # 板块延续/退潮预判
    # =====================================================================

    def _predict_sector_continuation(self, spot_df: pd.DataFrame,
                                      yesterday: dict) -> str:
        """
        检测昨日热门板块成分股今日竞价表现，预判板块延续性。

        规则：
        - 成分股高开率 > 50% → 板块延续（资金继续攻击）
        - 成分股高开率 < 20% → 一日游风险（资金撤离）
        - 中间状态 → 分歧，待开盘确认
        """
        hot_sectors = yesterday.get("hot_sectors", [])
        if not hot_sectors or spot_df.empty:
            return ""

        lines = []
        for s in hot_sectors[:3]:  # 只看前 3 个热门板块
            sector_name = s["name"]
            # 从竞价快照中筛选该板块成分股（通过涨停池缓存中的 industry 匹配）
            zt_df = self._zt_pool_cache
            if zt_df is None or zt_df.empty or "industry" not in zt_df.columns:
                continue

            # 找到该板块昨日涨停的股票代码
            sector_codes = set(
                zt_df[zt_df["industry"].astype(str) == sector_name]["code"].astype(str)
            )
            if not sector_codes:
                continue

            # 在今日竞价快照中查找这些代码
            spot_codes = set(spot_df["code"].astype(str))
            matched = sector_codes & spot_codes
            if len(matched) < 2:
                continue

            # 计算高开比例
            sector_spot = spot_df[spot_df["code"].astype(str).isin(matched)]
            high_open_count = int((sector_spot["change_pct"] > 2.0).sum())
            high_open_rate = high_open_count / len(sector_spot) * 100 if len(sector_spot) > 0 else 0

            yesterday_count = s["zt_count"]
            accel = s.get("accel", 0)
            accel_str = f" 加速+{accel}" if accel > 0 else (f" 减速{accel}" if accel < 0 else "")

            if high_open_rate > 50:
                tag = "🔥 延续"
                detail = f"高开率{high_open_rate:.0f}%，资金继续攻击"
            elif high_open_rate < 20:
                tag = "⚠️ 一日游风险"
                detail = f"高开率仅{high_open_rate:.0f}%，资金撤离"
            else:
                tag = "➡️ 分歧"
                detail = f"高开率{high_open_rate:.0f}%，待开盘确认"

            lines.append(
                f"  {tag} [{sector_name}] 昨{yesterday_count}家涨停{accel_str} | {detail}"
            )

        return "\n".join(lines) if lines else ""


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

        range_match = re.search(r'([+-]?\d+\.?\d*)%?\s*[~～至到]\s*([+-]?\d+\.?\d*)%?', req)
        if range_match:
            return float(range_match.group(1)) <= open_change_pct <= float(range_match.group(2))

        gt_match = re.search(r'[>≥]\s*([+-]?\d+\.?\d*)%?', req)
        if gt_match:
            return open_change_pct >= float(gt_match.group(1))

        if "高开" in req:
            return open_change_pct >= 2.0
        if "平开" in req or "低开" in req:
            return open_change_pct <= 1.5

        return True
