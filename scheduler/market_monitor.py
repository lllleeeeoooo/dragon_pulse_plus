import time
import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from core.strategies import StrategyAnalyzer
from core.holding_monitor import HoldingMonitor
from core.trade_calendar import is_trading_day
from llm.sell_advisor import DynamicSellAdvisor
from notifier.bark import bark_notifier
from database.services import HoldingManager, RecommendationManager

logger = logging.getLogger(__name__)


class MarketMonitor:
    """
    盘中 15 秒实时轮询监控引擎
    结合数据库读取实时持仓与推荐跟踪标的
    """

    def __init__(self):
        self.is_running = False
        # 缓存：市场级涨停/炸板数据每 60 秒刷新一次，避免每轮询都调 API
        self._zt_pool_cache: Optional[pd.DataFrame] = None
        self._zhaban_pool_cache: Optional[pd.DataFrame] = None
        self._pool_cache_time: float = 0.0
        self._pool_cache_interval: float = 60.0  # 秒

        # 缓存：个股历史均线数据 (每个交易日只算一次，盘中不变)
        self._ma_cache: Dict[str, Dict[str, Any]] = {}
        self._ma_cache_date: str = ""

        # 去重：当日已推送过的股票代码（防止每 15 秒重复推送同一个股）
        self._alerted_burst_codes: set = set()
        self._alerted_zhaban_codes: set = set()
        self._auto_bought_codes: set = set()
        self._emotion_top_alerted_today: bool = False
        self._alerted_sell_signals: Dict[str, set] = {}  # code -> {signal_type, ...}
        self._alert_date: str = ""

    def _reset_daily_state(self):
        """新交易日重置所有去重集合"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if self._alert_date != today:
            self._alerted_burst_codes.clear()
            self._alerted_zhaban_codes.clear()
            self._auto_bought_codes.clear()
            self._emotion_top_alerted_today = False
            self._alerted_sell_signals.clear()
            self._alert_date = today
            self._ma_cache.clear()
            logger.info(f"新交易日 {today}，去重状态已重置")

    def is_trading_time(self) -> bool:
        """
        判断当前时间是否处于交易时间 (09:30-11:30, 13:00-15:00)
        """
        now = datetime.datetime.now()
        if now.weekday() >= 5:
            return False

        current_time = now.time()
        m_start = datetime.time(9, 30)
        m_end = datetime.time(11, 30)
        a_start = datetime.time(13, 0)
        a_end = datetime.time(15, 0)

        return (m_start <= current_time <= m_end) or (a_start <= current_time <= a_end)

    def _refresh_pool_cache(self):
        """刷新涨停池/炸板池缓存（每 60 秒一次）"""
        now = time.time()
        if now - self._pool_cache_time < self._pool_cache_interval:
            return  # 缓存未过期
        try:
            today_str = datetime.datetime.now().strftime("%Y%m%d")
            self._zt_pool_cache = DataFetcher.get_zt_pool(date_str=today_str)
            self._zhaban_pool_cache = DataFetcher.get_zhaban_pool(date_str=today_str)
            self._pool_cache_time = now
            logger.debug("涨停/炸板池缓存已刷新")
        except Exception as e:
            logger.warning(f"刷新涨停/炸板池缓存失败: {e}")

    def _check_fund_inflow_alert(self, hot_codes: list = None):
        """
        大单抱团监控（修复 #3+#4）：对点火异动个股逐只查询主力资金流向。
        新版 akshare 的 stock_individual_fund_flow 需要指定个股代码，
        因此仅对当前已触发的点火标的做资金校验。
        """
        if not hot_codes:
            return

        for code in hot_codes[:3]:  # 最多查 3 只，避免 API 调用过频
            try:
                market = "sh" if str(code).startswith(("6", "5")) else "sz"
                fund_df = DataFetcher.get_individual_fund_flow(stock_code=code, market=market)
                if fund_df is None or fund_df.empty:
                    continue

                # 取最新一行的主力净流入
                latest = fund_df.iloc[-1]
                main_inflow = float(latest.get("main_net_inflow") or latest.get("主力净流入-净额") or 0)
                close = float(latest.get("close") or latest.get("收盘价") or 0)
                name = str(latest.get("name") or latest.get("名称") or code)

                if main_inflow > 50000000:  # 主力净流入 > 5000 万
                    logger.info(f"主力合力扫货: {name}({code}) 主力净流入 {main_inflow/1e8:.2f}亿")
            except Exception as e:
                logger.debug(f"查询 {code} 资金流向失败: {e}")

    def _get_market_max_lbc(self) -> int:
        """从缓存的涨停池中提取全市场最高连板数"""
        zt_df = self._zt_pool_cache
        if zt_df is not None and not zt_df.empty and "lbc" in zt_df.columns:
            try:
                return int(pd.to_numeric(zt_df["lbc"], errors="coerce").fillna(1).max())
            except Exception:
                pass
        return 0

    def _get_market_zhaban_rate(self) -> float:
        """从缓存的涨停/炸板池计算全市场炸板率"""
        zt_df = self._zt_pool_cache
        zhaban_df = self._zhaban_pool_cache
        zt_count = len(zt_df) if zt_df is not None and not zt_df.empty else 0
        zhaban_count = len(zhaban_df) if zhaban_df is not None and not zhaban_df.empty else 0
        total = zt_count + zhaban_count
        if total > 0:
            return round((zhaban_count / total) * 100, 2)
        return 0.0

    def _get_ma_prices(self, code: str) -> Dict[str, Any]:
        """获取个股的关键均线价格，按交易日缓存（MA5/MA10/MA20 盘中不变）"""
        today = datetime.datetime.now().strftime("%Y%m%d")

        # 新的交易日清空缓存
        if self._ma_cache_date != today:
            self._ma_cache = {}
            self._ma_cache_date = today

        if code not in self._ma_cache:
            try:
                ma_data = DataFetcher.get_stock_ma_prices(code, lookback=30)
            except Exception as e:
                logger.warning(f"获取 {code} 均线数据失败: {e}")
                ma_data = {"ma5": None, "ma10": None, "ma20": None}
            self._ma_cache[code] = ma_data

        return self._ma_cache[code]

    def run_polling_loop(self):
        """
        盘中轮询主循环。交易时段每 15 秒检测，非交易时空转等待。
        is_trading_time() 只是一个时间比对，空转时 CPU 开销可忽略。
        """
        self.is_running = True
        logger.info(f"启动盘中实时轮询监控引擎 (轮询间隔: {settings.MONITOR_INTERVAL_SECONDS}秒)...")

        while self.is_running:
            try:
                if not is_trading_day():
                    time.sleep(60)  # 非交易日每分钟检查一次，不浪费
                    continue

                if self.is_trading_time():
                    self._check_realtime_market()
                else:
                    # 非交易时间清缓存，为下一交易日做准备
                    self._zt_pool_cache = None
                    self._zhaban_pool_cache = None
                    self._pool_cache_time = 0.0
            except Exception as e:
                logger.error(f"盘中轮询异常: {e}")

            time.sleep(settings.MONITOR_INTERVAL_SECONDS)

    def _check_realtime_market(self):
        """
        单次轮询检测逻辑
        """
        # 交易日切换时重置去重状态
        self._reset_daily_state()

        # 0. 刷新涨停/炸板池缓存（内部自带 60 秒间隔控制）
        self._refresh_pool_cache()

        # 1. 获取全市场快照
        spot_df = DataFetcher.get_realtime_spot()
        if spot_df.empty:
            return

        # 2. 计算全市场风控参数（来自真实涨停池数据）
        market_max_lbc = self._get_market_max_lbc()
        market_zhaban_rate = self._get_market_zhaban_rate()

        # 3. 从数据库读取当前持仓与待观察推荐标的
        active_holdings = HoldingManager.get_active_holdings()
        pending_recs = RecommendationManager.get_pending_recommendations()
        pending_codes = {r["code"] for r in pending_recs}

        # 4. 扫描全市场点火异动个股 (放量 + 涨幅在 3%~9% 之间，排除已涨停的)
        burst_df = spot_df[
            (spot_df["volume_ratio"] >= settings.VOL_BURST_THRESHOLD) &
            (spot_df["change_pct"] >= settings.PRICE_BURST_THRESHOLD) &
            (spot_df["change_pct"] < settings.PRICE_BURST_MAX)
        ]

        if not burst_df.empty:
            for _, row in burst_df.head(3).iterrows():
                code = str(row["code"])
                name = str(row["name"])
                price = float(row["price"])
                change_pct = float(row["change_pct"])
                vol_ratio = float(row["volume_ratio"])

                # 当日已推送过该股的点火预警，跳过
                if code in self._alerted_burst_codes:
                    continue

                # 如果该异动股票正好是数据库中复盘推荐的标的
                is_recommended = code in pending_codes

                # 判断是否为一字板（无法买入）
                pre_close = float(row.get("pre_close", price))
                is_one_word_board = (price >= pre_close * 1.095) and (float(row.get("low", price)) == price)

                # 判断是否为高质量点火（非推荐但量价极强，也值得自动买入）
                amt_billion = float(row.get("amount", 0)) / 1e8
                is_high_quality_burst = (
                    not is_recommended and
                    change_pct >= 5.0 and         # 涨幅 >= 5%
                    vol_ratio >= 5.0 and           # 量比 >= 5 倍
                    amt_billion >= 2.0 and         # 成交额 >= 2 亿
                    change_pct < 9.0               # 还未封板
                )

                should_buy = (is_recommended or is_high_quality_burst) and not is_one_word_board
                if should_buy and code not in self._auto_bought_codes:
                    ai_holdings = HoldingManager.get_active_holdings(holding_type="AI_AUTO")
                    if not any(h["code"] == code for h in ai_holdings):
                        self._auto_bought_codes.add(code)
                        buy_reason = "复盘推荐" if is_recommended else "高质量点火"
                        HoldingManager.add_holding(
                            code=code,
                            name=name,
                            cost_price=price,
                            holding_type="AI_AUTO",
                            strategy=f"AI自动跟进({buy_reason})"
                        )
                        bark_notifier.send(
                            title=f"🤖 [AI 自动买入] {name}({code})",
                            body=f"{buy_reason}标的 {name}({code}) 触发买入信号 (现价:{price}元, +{change_pct}%, 量比{vol_ratio}倍, 成交{amt_billion:.1f}亿)，已自动纳入 AI 持仓追踪！",
                            group="AI自动持仓",
                            level="timeSensitive"
                        )

                # 计算全市场总成交额（用于抱团战法等市场总量判断）
                total_amt = float(spot_df["amount"].sum()) if "amount" in spot_df.columns else 1e12
                index_pct = float(spot_df["change_pct"].mean()) if "change_pct" in spot_df.columns else 0.0

                tags = StrategyAnalyzer.identify_tags(
                    stock_code=code,
                    stock_name=name,
                    change_pct=change_pct,
                    turnover_rate=float(row.get("turnover_rate", 0.0)),
                    market_total_amount=total_amt,
                    index_change_pct=index_pct
                )

                # 质量过滤：只有推荐标的或高质量点火才推送通知，杂毛不打扰
                is_quality = is_recommended or is_high_quality_burst
                if not is_quality:
                    self._alerted_burst_codes.add(code)  # 标记已处理，但静默跳过
                    continue

                trigger_title = "🔥 [复盘推荐股异动触发]" if is_recommended else "⚡ [高质量点火]"

                alert_msg = DynamicSellAdvisor.format_alert_message(
                    trigger_type=trigger_title,
                    stock_code=code,
                    stock_name=name,
                    current_price=price,
                    change_pct=change_pct,
                    volume_ratio=vol_ratio,
                    strategy_tag=",".join(tags),
                    detail_info=f"量比:{vol_ratio}倍，瞬间成交放大突破。"
                )

                bark_notifier.send(
                    title=f"{trigger_title} {name}({code}) +{change_pct}%",
                    body=alert_msg,
                    group="盘中异动",
                    level="timeSensitive" if is_recommended else "active"
                )
                self._alerted_burst_codes.add(code)  # 当日不再重复推送

        # 4.5 大单抱团监控：对点火标的验证主力资金（修复 #3 + #4）
        burst_codes = [str(row["code"]) for _, row in burst_df.head(3).iterrows()] if not burst_df.empty else []
        self._check_fund_inflow_alert(burst_codes)

        # 5. 全市场高位连板股"炸板"监控（基于真实涨停池对比）
        self._check_zhaban_alert(spot_df)

        # 6. 全市场情绪到顶预警（全局层面，每日仅推送一次）
        if (not self._emotion_top_alerted_today and
                market_max_lbc >= settings.EMOTION_TOP_MAX_LBC and
                market_zhaban_rate > settings.EMOTION_TOP_ZHABAN_RATE):
            self._emotion_top_alerted_today = True
            bark_notifier.send(
                title="🚨 [情绪到顶预警] 全市场退潮风险",
                body=f"全市场最高连板已达 {market_max_lbc} 板极值且炸板率高达 {market_zhaban_rate}%，市场处于高潮末端/退潮期，建议逢高落袋为安。",
                group="卖出提醒",
                level="timeSensitive"
            )

        # 7. 从数据库持仓表监控卖出/止损条件
        for holding in active_holdings:
            code = holding["code"]
            stock_data = spot_df[spot_df["code"] == code]
            if not stock_data.empty:
                row = stock_data.iloc[0]
                curr_price = float(row["price"])
                is_zt = float(row["change_pct"]) >= 9.8

                # 实时计算并更新持仓最新价格与收益率 (%)
                HoldingManager.update_holding_profit_rate(code, curr_price)

                # 如果盘中封过涨停，更新数据库状态
                if is_zt and not holding.get("was_limit_up_today", False):
                    HoldingManager.update_was_limit_up(code, True)
                    holding["was_limit_up_today"] = True  # 本地同步，避免重复调用

                # 获取真实的 MA5 均线价格
                ma_data = self._get_ma_prices(code)
                ma5_price = ma_data.get("ma5") or (curr_price * 0.97)

                # 真实分时 VWAP（从逐笔成交计算），失败时用 OHLC 均值兜底
                vwap = DataFetcher.get_intraday_vwap(code)
                if vwap <= 0:
                    vwap = (float(row.get("open", curr_price)) +
                            curr_price +
                            float(row.get("high", curr_price)) +
                            float(row.get("low", curr_price))) / 4.0

                signals = HoldingMonitor.check_sell_signals(
                    stock_code=code,
                    stock_name=holding.get("name", code),
                    current_price=curr_price,
                    cost_price=holding.get("cost_price", curr_price),
                    avg_分时_price=vwap,
                    ma5_price=ma5_price,
                    is_limit_up=is_zt,
                    was_limit_up_today=holding.get("was_limit_up_today", False),
                    market_max_lbc=market_max_lbc if market_max_lbc > 0 else 5,
                    market_zhaban_rate=market_zhaban_rate if market_zhaban_rate > 0 else 20.0
                )

                for sig in signals:
                    sig_type = sig["type"]
                    # 去重：同股同类型卖出信号当日仅推送一次
                    if code not in self._alerted_sell_signals:
                        self._alerted_sell_signals[code] = set()
                    if sig_type in self._alerted_sell_signals[code]:
                        continue
                    self._alerted_sell_signals[code].add(sig_type)

                    # 若为 AI 自动持仓触发离场卖出信号，系统自动执行平仓操作！
                    if holding.get("holding_type") == "AI_AUTO" and sig["level"] in ("CRITICAL", "HIGH"):
                        HoldingManager.close_holding(code=code, holding_type="AI_AUTO")
                        bark_notifier.send(
                            title=f"🤖 [AI 自动卖出平仓] {holding.get('name')}({code})",
                            body=f"AI持仓标的 {holding.get('name')} 触发卖出信号 ({sig['type']})，已自动清仓离场！",
                            group="AI自动持仓",
                            level="timeSensitive"
                        )
                    else:
                        bark_notifier.send(
                            title=f"🚨 [{sig['type']}] {holding.get('name')}",
                            body=sig["reason"],
                            group="卖出提醒",
                            level="timeSensitive"
                        )

    def _check_zhaban_alert(self, spot_df: pd.DataFrame):
        """
        基于涨停池缓存检测真正的炸板：
        今日曾封板（在涨停池中）的标的，若当前快照中涨幅已回落至 < 9.5% 且放量，
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
            if spot["change_pct"] < 7.0 and spot["volume_ratio"] > 2.0:
                logger.warning(f"盘中高位分歧炸板预警: {name}({code}) 当前涨幅: {spot['change_pct']}%")
                bark_notifier.send(
                    title=f"💥 [炸板提醒] {name}({code})",
                    body=f"涨停池标的 {name}({code}) 当前仅涨 {spot['change_pct']}%，高位炸板分歧！量比 {spot['volume_ratio']} 倍，建议立即检查是否止盈/止损。",
                    group="炸板提醒",
                    level="timeSensitive"
                )
                self._alerted_zhaban_codes.add(code)
