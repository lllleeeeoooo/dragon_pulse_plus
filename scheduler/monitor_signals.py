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


class _MonitorSignalsMixin:
    def _check_sector_linkage(self):
        """
        板块联动监控：检测涨停池中同板块涨停家数的加速变化。
        当某板块涨停家数从1→2或更高时，触发联动预警。
        """
        zt_df = self._zt_pool_cache
        if zt_df is None or zt_df.empty or "industry" not in zt_df.columns:
            return

        # 按行业分组计数
        sectors = zt_df["industry"].dropna().astype(str)
        sectors = sectors[sectors.str.len() > 0]
        current_counts = sectors.value_counts().to_dict()
        self._current_sector_counts = current_counts

        # 与上一轮对比
        for sector, count in current_counts.items():
            if sector in self._alerted_sector_names:
                continue

            prev_count = self._prev_sector_counts.get(sector, 0)
            delta = count - prev_count

            # 触发条件：达到最小涨停家数 且 较上轮有增量
            should_alert = (
                count >= settings.SECTOR_LINKAGE_MIN_COUNT and
                delta >= settings.SECTOR_LINKAGE_ACCEL_DELTA
            )

            if should_alert:
                self._alerted_sector_names.add(sector)
                # 提取该板块涨停的股票信息
                sector_stocks = zt_df[zt_df["industry"].astype(str) == sector]
                stock_details = []
                for _, row in sector_stocks.iterrows():
                    name = str(row.get("name", ""))
                    code = str(row.get("code", ""))
                    lbc = int(row.get("lbc", 1))
                    tag = f"{lbc}板" if lbc >= 2 else "首板"
                    stock_details.append(f"{name}({code}){tag}")

                stocks_text = " / ".join(stock_details[:6])
                logger.info(f"板块联动预警: [{sector}] 涨停{count}家(+{delta}) -> {stocks_text}")
                bark_notifier.send(
                    title=f"🔗 [板块联动] {sector} {count}家涨停",
                    body=(
                        f"板块【{sector}】涨停加速至{count}家(较上轮+{delta})！\n"
                        f"成分: {stocks_text}\n"
                        f"关注该板块内未涨停的核心标的补涨机会。"
                    ),
                    group="板块联动",
                    level="timeSensitive" if count >= 3 else "active"
                )

        # 更新缓存
        self._prev_sector_counts = current_counts


    def _check_seal_decay(self):
        """
        封单衰减监控：对比前后两轮涨停池封单金额。
        若某只持仓股的封单缩减超过70%，发出预警。
        """
        zt_df = self._zt_pool_cache
        if zt_df is None or zt_df.empty or "seal_amount" not in zt_df.columns:
            return

        # 构建当前封单映射
        current_seals: Dict[str, float] = {}
        for _, row in zt_df.iterrows():
            code = str(row.get("code", ""))
            seal = float(pd.to_numeric(row.get("seal_amount", 0), errors="coerce") or 0)
            if code and seal > 0:
                current_seals[code] = seal

        # 有前一轮数据时做对比
        if self._prev_seal_amounts:
            active_holdings = HoldingManager.get_active_holdings()
            holding_codes = {h["code"] for h in active_holdings}

            for code, prev_seal in self._prev_seal_amounts.items():
                if code in self._alerted_seal_decay_codes:
                    continue
                if code not in holding_codes:
                    continue
                curr_seal = current_seals.get(code, 0)
                if prev_seal > 0 and curr_seal > 0:
                    decay_ratio = (prev_seal - curr_seal) / prev_seal
                    if decay_ratio >= 0.7:
                        name = ""
                        match = zt_df[zt_df["code"].astype(str) == code]
                        if not match.empty:
                            name = str(match.iloc[0].get("name", code))
                        self._alerted_seal_decay_codes.add(code)
                        logger.warning(f"封单衰减预警: {name}({code}) 封单从{prev_seal/1e8:.1f}亿缩至{curr_seal/1e8:.1f}亿，衰减{decay_ratio*100:.0f}%")
                        bark_notifier.send(
                            title=f"⚠️ [封单衰减] {name}({code})",
                            body=f"封单从 {prev_seal/1e8:.1f}亿 缩至 {curr_seal/1e8:.1f}亿（衰减{decay_ratio*100:.0f}%），炸板风险急剧上升，建议减仓！",
                            group="卖出提醒",
                            level="timeSensitive"
                        )

        # 更新缓存
        self._prev_seal_amounts = current_seals


    def _check_fund_inflow_alert(self, hot_codes: list = None, cap_map: dict = None):
        """
        大单抱团监控（修复 #3+#4）：对点火异动个股逐只查询主力资金流向。
        新版 akshare 的 stock_individual_fund_flow 需要指定个股代码，
        因此仅对当前已触发的点火标的做资金校验。

        阈值按流通市值动态分级：max(FUND_INFLOW_MIN, 流通市值 * FUND_INFLOW_CAP_RATIO)
        小盘股 ≈2000万起，大盘股随市值递增，避免一刀切。
        """
        if not hot_codes:
            return
        cap_map = cap_map or {}

        for code in hot_codes[:3]:  # 最多查 3 只，避免 API 调用过频
            try:
                market = "sh" if str(code).startswith(("6", "5")) else "sz"
                fund_df = self._DF.get_individual_fund_flow(stock_code=code, market=market)
                if fund_df is None or fund_df.empty:
                    continue

                # 取最新一行的主力净流入
                latest = fund_df.iloc[-1]
                main_inflow = float(latest.get("main_net_inflow") or latest.get("主力净流入-净额") or 0)
                close = float(latest.get("close") or latest.get("收盘价") or 0)
                name = str(latest.get("name") or latest.get("名称") or code)

                # 按流通市值计算动态阈值（单位统一为元）
                circ_cap = float(cap_map.get(str(code), 0))
                if circ_cap > 0:
                    dynamic_threshold = max(
                        settings.FUND_INFLOW_MIN * 1e4,        # 绝对底线 万元→元
                        circ_cap * settings.FUND_INFLOW_CAP_RATIO  # 流通市值比例
                    )
                else:
                    # 市值缺失时退守绝对阈值
                    dynamic_threshold = settings.FUND_INFLOW_MIN * 1e4

                if main_inflow > dynamic_threshold:
                    cap_desc = f" 流通市值:{circ_cap/1e8:.0f}亿" if circ_cap > 0 else ""
                    logger.info(f"主力合力扫货: {name}({code}) 主力净流入 {main_inflow/1e8:.2f}亿 "
                                f"(阈值 {dynamic_threshold/1e8:.2f}亿{cap_desc})")
            except Exception as e:
                logger.debug(f"查询 {code} 资金流向失败: {e}")


    def _is_bad_intraday_pattern(self, code: str) -> bool:
        """
        检测个股分时形态是否不利于买入。
        冲高回落/放量滞涨/天地板 → 返回True（不宜买入）。
        为避免API频繁调用，仅在成交额>5亿的标的上检测。
        """
        try:
            patterns = self._DF.detect_intraday_patterns(code)
            bad_patterns = {"冲高回落", "放量滞涨", "天地板", "尾盘砸盘"}
            if bad_patterns & set(patterns):
                logger.debug(f"{code} 分时形态不佳: {patterns}，跳过买入")
                return True
        except Exception:
            pass
        return False


    def _is_daily_loss_breaker_triggered(self, ai_holdings: list) -> bool:
        """检查AI持仓当日总亏损是否触发熔断"""
        if not ai_holdings:
            return False
        total_profit = sum(h.get("profit_rate", 0) for h in ai_holdings)
        avg_profit = total_profit / len(ai_holdings)
        if avg_profit <= settings.DAILY_LOSS_CIRCUIT_BREAKER:
            import scheduler.monitor_core as _mcore
            if not getattr(self, '_circuit_breaker_alerted', False):
                self._circuit_breaker_alerted = True
                _mcore._circuit_breaker_alerted = True
                logger.warning(f"AI持仓亏损熔断：平均收益率 {avg_profit:.2f}% <= {settings.DAILY_LOSS_CIRCUIT_BREAKER}%，停止自动买入")
                bark_notifier.send(
                    title="🛑 [亏损熔断] AI自动买入已暂停",
                    body=f"AI持仓平均亏损 {avg_profit:.2f}% 触发熔断阈值({settings.DAILY_LOSS_CIRCUIT_BREAKER}%)，今日不再自动买入。",
                    group="风控提醒",
                    level="timeSensitive"
                )
            return True
        return False


    def _check_zhaban_alert(self, spot_df: pd.DataFrame):
        """
        基于涨停池缓存检测真正的炸板：
        今日曾封板（在涨停池中）的标的，若当前快照中涨幅已回落至 < 7% 且放量，
        说明该股已炸板，发出预警。
        """
        zt_pool = self._zt_pool_cache
        if zt_pool is None or zt_pool.empty:
            return

        # 获取当前快照的 code -> 最新价/涨跌幅 映射
        spot_map = {}
        if spot_df is not None and not spot_df.empty:
            for _, srow in spot_df.iterrows():
                spot_map[str(srow["code"])] = {
                    "price": float(srow["price"]),
                    "change_pct": float(srow["change_pct"]),
                    "volume_ratio": float(srow.get("volume_ratio", 0))
                }

        for _, zrow in zt_pool.iterrows():
            code = str(zrow.get("code", ""))
            name = str(zrow.get("name", ""))
            if code in self._alerted_zhaban_codes or code not in spot_map:
                continue

            spot = spot_map[code]
            # 在涨停池中但当前涨幅回落 < 7% 且放量 → 炸板
            if spot["change_pct"] < settings.ZHABAN_ALERT_CHANGE and spot["volume_ratio"] > settings.ZHABAN_ALERT_VOL_RATIO:
                logger.warning(f"盘中高位分歧炸板预警: {name}({code}) 当前涨幅: {spot['change_pct']}%")
                bark_notifier.send(
                    title=f"💥 [炸板提醒] {name}({code})",
                    body=f"涨停池标的 {name}({code}) 当前仅涨 {spot['change_pct']}%，高位炸板分歧！量比 {spot['volume_ratio']} 倍，建议立即检查是否止盈/止损。",
                    group="炸板提醒",
                    level="timeSensitive"
                )
                self._alerted_zhaban_codes.add(code)

