import time
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from core.emotion_index import EmotionVector
from core.strategies import StrategyAnalyzer, MarketStyle
from core.holding_monitor import HoldingMonitor
from core.trade_calendar import is_trading_day, get_previous_trading_day
from llm.sell_advisor import DynamicSellAdvisor
from notifier.bark import bark_notifier
from database.services import HoldingManager, RecommendationManager

logger = logging.getLogger(__name__)

# LLM 异动润色后台线程池：LLM 调用(拉分时+生成)可能耗时数十秒，
# 放在后台线程执行，避免阻塞 15 秒盘中轮询导致卖出/炸板监控停摆。
_llm_alert_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm-alert")

# 全局变量（global 语句写入此模块命名空间，dashboard 直接从此读取）
_current_market_style_global: Dict[str, str] = {}
_monitor_running: bool = False
_last_monitor_cycle: str = ""
_circuit_breaker_alerted: bool = False
_index_breaker_alerted: bool = False


class _MonitorCoreMixin:
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
        self._consistency_alerted_today: bool = False
        self._pending_sell_codes: set = set()  # 跌停中待卖出的股票
        self._alerted_sell_signals: Dict[str, set] = {}  # code -> {signal_type, ...}
        self._alert_date: str = ""
        # 市场风格缓存（供 API 查询）
        self._current_market_style: Dict[str, str] = {}
        self._last_logged_style: str = ""
        # 昨日涨停溢价缓存，每 2 分钟刷新
        self._premium_cache: Dict[str, Any] = {}
        self._premium_cache_time: float = 0.0
        # 封单衰减监控：记录上一轮涨停池封单金额 {code: seal_amount}
        self._prev_seal_amounts: Dict[str, float] = {}
        self._alerted_seal_decay_codes: set = set()
        # 板块联动监控：跟踪每个行业涨停家数变化
        self._prev_sector_counts: Dict[str, int] = {}
        self._alerted_sector_names: set = set()
        self._current_sector_counts: Dict[str, int] = {}
        # 情绪周期：每日加载一次昨日周期阶段
        self._cycle_phase: str = ""
        self._cycle_stance: Dict[str, Any] = {}
        # 竞价监控：09:15-09:25采集数据
        self._auction_snapshots: List[Dict[str, Any]] = []
        self._auction_summary_sent: bool = False
        # 任务记录计数器（每 4 轮≈60 秒更新一次）
        self._job_record_counter: int = 0
        # 分时形态检测缓存 {code: (是否不佳, 时间戳)}，TTL 过期重拉以跟上盘中走势
        self._pattern_cache: Dict[str, tuple] = {}
        # 个股行业缓存（当日），供板块集中度控制，避免反复查库
        self._industry_cache: Dict[str, str] = {}


    def _reset_daily_state(self):
        """新交易日重置所有去重集合，输出盘前启动日志"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if self._alert_date != today:
            self._alerted_burst_codes.clear()
            self._alerted_zhaban_codes.clear()
            # 重启/换日后从数据库重建当日已买入集合（避免盘中重启导致 MAX_DAILY_BUYS 计数清零）
            self._auto_bought_codes = self._load_today_ai_bought_codes()
            self._emotion_top_alerted_today = False
            self._consistency_alerted_today = False
            self._pending_sell_codes.clear()
            self._alerted_sell_signals.clear()
            self._alert_date = today
            self._ma_cache.clear()
            self._pattern_cache.clear()
            self._industry_cache.clear()
            self._startup_logged = False  # 允许下一天输出启动日志
            global _circuit_breaker_alerted
            self._circuit_breaker_alerted = False
            _circuit_breaker_alerted = False
            self._prev_seal_amounts.clear()
            self._alerted_seal_decay_codes.clear()
            self._prev_sector_counts.clear()
            self._alerted_sector_names.clear()
            self._current_sector_counts.clear()
            self._llm_alert_calls_today = 0
            self._auction_snapshots.clear()
            self._auction_summary_sent = False
            global _index_breaker_alerted
            self._index_breaker_alerted = False
            _index_breaker_alerted = False
            self._promotion_rate_cache = None
            HoldingManager.reset_all_limit_up_flags()
            # 只过期"上一交易日之前"的推荐；昨日复盘生成的推荐(trade_date=上一交易日)今日仍有效
            RecommendationManager.expire_old_recommendations(before_date=get_previous_trading_day())
            # 加载昨日情绪周期阶段
            self._load_cycle_phase()
            logger.info(f"新交易日 {today}，去重状态已重置")


    def _log_startup_report(self, spot_df: pd.DataFrame):
        """每日首次轮询输出完整启动报告"""
        if getattr(self, '_startup_logged', False):
            return
        self._startup_logged = True
        today = self._alert_date

        # --- 流动性基准 ---
        bl = self._DF.get_adaptive_baseline()
        baseline = bl["ma_amount"]
        spot_total = self._DF.get_market_total_amount() if not spot_df.empty else 0
        now_amount = spot_total / 1e8  # 此刻累计(亿)
        est = self._DF.estimate_today_amount(spot_total)
        estimated_today = est["estimated"] if est.get("estimated", 0) > 0 else now_amount
        k = MarketStyle._capacity_factor(estimated_today, baseline)

        # --- 持仓概况 ---
        holdings = HoldingManager.get_active_holdings()
        ai_count = sum(1 for h in holdings if h.get("holding_type") == "AI_AUTO")
        manual_count = len(holdings) - ai_count

        # --- 涨跌停池 ---
        zt_df = self._zt_pool_cache
        zhaban_df = self._zhaban_pool_cache
        zt_count = len(zt_df) if zt_df is not None and not zt_df.empty else 0
        # 真炸板 = 在炸板池但当前不在涨停池（口径与风格判定/炸板率一致）
        zhaban_count = self._compute_true_zhaban_count(zt_df, zhaban_df)

        # --- 涨跌分布 ---
        up_count = 0
        down_count = 0
        if not spot_df.empty:
            up_count = int((spot_df["change_pct"] > 0).sum())
            down_count = int((spot_df["change_pct"] < 0).sum())

        # --- 昨日涨停溢价 ---
        premium_data = self._DF.get_yesterday_zt_premium()
        self._premium_cache = premium_data
        self._premium_cache_time = time.time()

        # --- 市场风格预判 ---
        market_max_lbc = self._get_market_max_lbc()
        market_zhaban_rate = self._get_market_zhaban_rate()
        dt_count = int(spot_df.apply(
            lambda r: type(self)._is_limit_down(str(r["code"]), float(r["change_pct"])), axis=1).sum())
        emotion = {
            "height": market_max_lbc, "zt_count": zt_count, "dt_count": dt_count,
            "zhaban_rate": market_zhaban_rate, "zhaban_count": zhaban_count,
            "breadth": zt_count - dt_count, "yield_rate": premium_data.get("intraday_premium", 0),
            "seal_force_ratio": 0, "yidong_bravery": 50,
        }
        score_premium = EmotionVector._score_premium(emotion["yield_rate"])
        score_height = EmotionVector._score_height(market_max_lbc)
        score_breadth = EmotionVector._score_breadth(zt_count - dt_count)
        score_support = EmotionVector._score_support(market_zhaban_rate)
        emotion["sentiment_index"] = round(
            score_premium * settings.PREMIUM_WEIGHT + score_breadth * settings.BREADTH_WEIGHT +
            score_height * settings.HEIGHT_WEIGHT + score_support * settings.SUPPORT_WEIGHT, 1)
        style = MarketStyle.classify(emotion, market_amount=estimated_today, baseline=baseline)

        # --- 输出启动报告 ---
        logger.info("=" * 60)
        logger.info(f"  交易日 {today} 盘中监控启动")
        logger.info("=" * 60)
        logger.info(f"  [流动性] 昨日全天:{baseline:.0f}亿 | 此刻累计:{now_amount:.0f}亿 | 容量因子K={k:.2f}")
        logger.info(f"  [涨跌停] 涨停:{zt_count} | 跌停:{dt_count} | 炸板:{zhaban_count}({market_zhaban_rate}%)")
        logger.info(f"  [涨跌比] 上涨:{up_count} | 下跌:{down_count} | 平盘:{len(spot_df)-up_count-down_count}")
        logger.info(f"  [溢价]  开盘溢价:{premium_data.get('opening_premium', 0)}% | "
                    f"即时溢价:{premium_data.get('intraday_premium', 0)}% | "
                    f"高开>3%:{premium_data.get('high_open_ratio', 0)}% | "
                    f"红盘率:{premium_data.get('positive_ratio', 0)}% | 样本:{premium_data.get('total_count', 0)}只")
        logger.info(f"  [持仓]  活跃:{len(holdings)}只 (AI:{ai_count} 手动:{manual_count})")
        logger.info(f"  [风格]  {style['style']} → {style['priority_strategy']} | {style['reason']}")
        logger.info(f"  [配置]  LLM:{settings.LLM_MODEL} | 轮询间隔:{settings.MONITOR_INTERVAL_SECONDS}s | "
                    f"情绪到顶阈值:连板>={settings.EMOTION_TOP_MAX_LBC}板&炸板>{settings.EMOTION_TOP_ZHABAN_RATE}%")
        logger.info("=" * 60)

        # 落库
        from database.services import SystemLogManager
        detail = (
            f"昨日全天:{baseline:.0f}亿 | 此刻累计:{now_amount:.0f}亿 | K={k:.2f} | "
            f"涨停:{zt_count} 跌停:{dt_count} 炸板:{zhaban_count}({market_zhaban_rate}%) | "
            f"上涨:{up_count} 下跌:{down_count} | "
            f"溢价:{premium_data.get('intraday_premium', 0)}% 红盘率:{premium_data.get('positive_ratio', 0)}% | "
            f"活跃持仓:{len(holdings)}只(AI:{ai_count}手动:{manual_count}) | "
            f"风格:{style['style']}({style['priority_strategy']})"
        )
        SystemLogManager.add_log(
            log_date=datetime.datetime.now().strftime("%Y-%m-%d"),
            category="startup",
            title=f"交易日 {today} 盘中监控启动 | 风格:{style['style']} K={k:.2f}",
            detail=detail
        )


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


    def is_auction_time(self) -> bool:
        """判断是否在集合竞价观察窗口 (09:15-09:25)"""
        now = datetime.datetime.now()
        if now.weekday() >= 5:
            return False
        current_time = now.time()
        return datetime.time(9, 15) <= current_time <= datetime.time(9, 25)


    def _refresh_pool_cache(self):
        """刷新涨停池/炸板池缓存（每 60 秒一次），同时检测封单衰减"""
        now = time.time()
        if now - self._pool_cache_time < self._pool_cache_interval:
            return  # 缓存未过期
        try:
            today_str = datetime.datetime.now().strftime("%Y%m%d")
            self._zt_pool_cache = self._DF.get_zt_pool(date_str=today_str)
            self._zhaban_pool_cache = self._DF.get_zhaban_pool(date_str=today_str)
            self._pool_cache_time = now

            # 封单衰减检测
            self._check_seal_decay()
            # 板块联动检测
            self._check_sector_linkage()

            logger.debug("涨停/炸板池缓存已刷新")
        except Exception as e:
            logger.warning(f"刷新涨停/炸板池缓存失败: {e}")
            # 失败后退避重试（默认 5 分钟后再试），避免对异常接口每 60 秒频繁轮询造成反爬压力
            self._pool_cache_time = now + settings.POOL_CACHE_FAIL_BACKOFF_SECONDS


    def _get_ma_prices(self, code: str) -> Dict[str, Any]:
        """获取个股的关键均线价格，按交易日缓存（MA5/MA10/MA20 盘中不变）"""
        today = datetime.datetime.now().strftime("%Y%m%d")

        # 新的交易日清空缓存
        if self._ma_cache_date != today:
            self._ma_cache = {}
            self._ma_cache_date = today

        if code not in self._ma_cache:
            try:
                ma_data = self._DF.get_stock_ma_prices(code, lookback=30)
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
        global _monitor_running, _last_monitor_cycle
        self.is_running = True
        _monitor_running = True
        from scheduler.helpers import _record_job_run
        _record_job_run("job_monitor_loop", "盘中实时监控")
        logger.info(f"启动盘中实时轮询监控引擎 (轮询间隔: {settings.MONITOR_INTERVAL_SECONDS}秒)...")

        while self.is_running:
            try:
                if not is_trading_day():
                    time.sleep(300)  # 非交易日5分钟检查一次，不浪费API
                    continue

                if self.is_auction_time():
                    self._reset_daily_state()
                    self._check_auction_phase()
                    time.sleep(30)  # 竞价期间30秒采集一次
                    continue
                elif self.is_trading_time():
                    self._check_realtime_market()
                    _last_monitor_cycle = datetime.datetime.now().strftime("%H:%M:%S")
                    # 每 60 秒更新一次任务执行时间（避免每次 15 秒轮询都写库）
                    if getattr(self, '_job_record_counter', 0) % 4 == 0:
                        from scheduler.helpers import _record_job_run
                        _record_job_run("job_monitor_loop", "盘中实时监控")
                    self._job_record_counter = getattr(self, '_job_record_counter', 0) + 1
                else:
                    # 非交易时间清缓存
                    self._zt_pool_cache = None
                    self._zhaban_pool_cache = None
                    self._pool_cache_time = 0.0
                    time.sleep(60)  # 非交易时间每分钟检查一次
                    continue
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
        spot_df = self._DF.get_realtime_spot()
        if spot_df.empty:
            return

        # 1.5 每日首次输出完整启动报告
        self._log_startup_report(spot_df)

        # 2. 计算全市场风控参数（来自真实涨停池数据）
        market_max_lbc = self._get_market_max_lbc()
        market_zhaban_rate = self._get_market_zhaban_rate()

        # 2.5 盘中实时市场风格判定（影响选股优先级）
        market_style = self._classify_intraday_style(spot_df, market_max_lbc, market_zhaban_rate)
        self._current_market_style = market_style  # 缓存，供 API 查询
        global _current_market_style_global
        _current_market_style_global = market_style
        self._notify_style_change(market_style)

        index_breaker_triggered = self._check_index_breaker(spot_df)

        # 3. 从数据库读取当前持仓与待观察推荐标的
        active_holdings = HoldingManager.get_active_holdings()
        # 今日有效的推荐 = 上一交易日 18:01 复盘生成的（trade_date = 上一交易日）
        pending_recs = RecommendationManager.get_pending_recommendations(
            trade_date=get_previous_trading_day()
        )
        pending_codes = {r["code"] for r in pending_recs}

        # 4. 扫描全市场抢筹信号 + 自动买入 + 推送（已拆到 _scan_signals）
        self._scan_signals(spot_df, market_style, pending_recs, pending_codes, index_breaker_triggered)

        # 5. 全市场高位连板股"炸板"监控（基于真实涨停池对比）
        self._check_zhaban_alert(spot_df)

        self._check_emotion_top_alert(market_max_lbc, market_zhaban_rate)

        self._check_consistency_alert()

        # 7. 持仓卖出/止损信号监控 + 批量更新收益率（已拆到 _monitor_holdings）
        self._monitor_holdings(spot_df, active_holdings, market_max_lbc, market_zhaban_rate)


    def _notify_style_change(self, market_style):
        """市场风格切换通知（从 _check_realtime_market 拆出）"""
        if market_style["style"] != self._last_logged_style:
            logger.info(
                f"市场风格切换: [{market_style['style']}] {market_style['reason']} | "
                f"涨停:{market_style['zt_count']}({market_style['zt_source']}) "
                f"跌停:{market_style['dt_count']} "
                f"炸板:{market_style['zhaban_count']}({market_style['zhaban_rate']}%) "
                f"情绪分:{market_style['sentiment_index']}"
            )
            emoji_map = {"抱团": "🛡️", "共振": "🚀", "打板": "🎯", "低吸": "📉", "高潮": "⚠️", "观望": "💤"}
            emoji = emoji_map.get(market_style["style"], "📊")
            bark_notifier.send(
                title=f"{emoji} 风格切换 → {market_style['style']}",
                body=(f"{market_style['reason']}\n"
                      f"涨停:{market_style['zt_count']} 跌停:{market_style['dt_count']} "
                      f"情绪:{market_style['sentiment_index']}分 K={market_style.get('capacity_factor',0):.2f}"),
                group="市场风格",
                level="timeSensitive" if market_style["style"] in ("抱团", "高潮") else "active"
            )
            self._last_logged_style = market_style["style"]

    def _check_index_breaker(self, spot_df) -> bool:
        """大盘级熔断检查：全市场均涨幅跌破阈值时停止买入并预警，返回是否触发"""
        # 2.8 大盘级熔断检查：全市场均涨幅跌破阈值时停止所有买入
        market_avg_change = float(spot_df["change_pct"].mean()) if not spot_df.empty else 0.0
        index_breaker_triggered = market_avg_change <= settings.INDEX_DROP_CIRCUIT_BREAKER
        if index_breaker_triggered and not getattr(self, '_index_breaker_alerted', False):
            global _index_breaker_alerted
            self._index_breaker_alerted = True
            _index_breaker_alerted = True
            logger.warning(f"大盘熔断: 全市场均涨幅 {market_avg_change:.2f}% <= {settings.INDEX_DROP_CIRCUIT_BREAKER}%，停止自动买入")
            bark_notifier.send(
                title="🛑 [大盘熔断] 系统性风险",
                body=f"全市场均涨幅 {market_avg_change:.2f}%，触发大盘熔断阈值({settings.INDEX_DROP_CIRCUIT_BREAKER}%)。已停止所有自动买入，建议逢高减仓。",
                group="风控提醒",
                level="timeSensitive"
            )
        return index_breaker_triggered

    def _check_emotion_top_alert(self, market_max_lbc, market_zhaban_rate):
        """真正的情绪到顶预警：连板极高 + 情绪仍热 + 炸板率异常（顶部裂纹出现时提醒，非崩溃后）"""
        # 6. 全市场情绪到顶预警（全局层面，每日仅推送一次）
        # 必须同时满足：连板极高 + 情绪仍热 + 炸板率异常（顶部出现裂纹时预警，而非情绪崩塌后）
        sentiment = self._current_market_style.get("sentiment_index", 50)
        if (not self._emotion_top_alerted_today and
                market_max_lbc >= settings.EMOTION_TOP_MAX_LBC and
                sentiment >= settings.EMOTION_TOP_SENTIMENT_MIN and
                market_zhaban_rate > settings.EMOTION_TOP_ZHABAN_RATE):
            self._emotion_top_alerted_today = True
            bark_notifier.send(
                title="🚨 [情绪到顶预警] 高潮末端风险",
                body=(f"最高连板{market_max_lbc}板+情绪{sentiment}分+炸板率{market_zhaban_rate}%，"
                      f"市场处于高潮末端/顶部区间，警惕次日分歧，高位标的逢高减仓、不打加速板。"),
                group="卖出提醒",
                level="timeSensitive"
            )

    def _check_consistency_alert(self):
        """一致性预警：情绪过热+涨停过多→次日分歧概率大（每日一次）（从 _check_realtime_market 拆出）"""
        # 6.5 一致性预警：情绪过热+涨停过多→次日分歧概率大（每日一次）
        si = self._current_market_style.get("sentiment_index", 0)
        zt = self._current_market_style.get("zt_count", 0)
        if (not self._consistency_alerted_today and si >= 70 and zt >= 80):
            self._consistency_alerted_today = True
            bark_notifier.send(
                title="⚠️ [一致性预警] 明日分歧风险",
                body=(f"情绪{si}分+涨停{zt}家，市场过于一致。"
                      f"建议：高位标的逢高减仓，不打缩量加速板。"),
                group="卖出提醒",
                level="timeSensitive"
            )

    def _scan_signals(self, spot_df, market_style, pending_recs, pending_codes, index_breaker_triggered):
        """第4步：扫描全市场抢筹信号 + 自动买入 + 推送（从 _check_realtime_market 拆出）"""
        # 4. 扫描全市场抢筹信号（四种类型）
        #    a) 点火异动: 放量 + 涨幅 3%~9%
        #    b) 逼近封板: 涨幅 8%~9.5% + 量比 > 5
        #    c) 低开猛拉: 低开 + 直线拉回 + 量比 > 3
        #    d) 振幅放量: 振幅 > 7% + 量比 > 3 + 涨幅 > 3%
        spot_df = spot_df.copy()
        spot_df["amt_billion"] = spot_df["amount"].astype(float) / 1e8

        # 辅助列：低开猛拉的拉升强度 = (现价-开盘) / (最高-最低)
        price_range = spot_df["high"].astype(float) - spot_df["low"].astype(float)
        rally_strength = (spot_df["price"].astype(float) - spot_df["open"].astype(float)) / price_range.replace(0, 1)

        # 按板块区分涨停线：主板 10cm vs 双创 20cm（科创板已在源头过滤，此处主要区分创业板 300）
        is_main_board = spot_df["code"].astype(str).str.match(r"^(60|00)")
        spot_df["_limit_max"] = settings.PRICE_BURST_MAX  # 主板 10cm
        spot_df.loc[~is_main_board, "_limit_max"] = settings.PRICE_BURST_MAX_20CM  # 双创 20cm
        # 逼近封板区间 = 涨停线的 80%~100%
        spot_df["_near_limit_min"] = spot_df["_limit_max"] * settings.MONITOR_NEAR_LIMIT_RATIO  # 涨停线×比值
        spot_df["_near_limit_max"] = spot_df["_limit_max"]

        spot_df["_signal_burst"] = (
            (spot_df["volume_ratio"] >= settings.VOL_BURST_THRESHOLD) &
            (spot_df["change_pct"] >= settings.PRICE_BURST_THRESHOLD) &
            (spot_df["change_pct"] < spot_df["_limit_max"])
        )
        spot_df["_signal_near_limit"] = (
            (spot_df["change_pct"] >= spot_df["_near_limit_min"]) &
            (spot_df["change_pct"] <= spot_df["_near_limit_max"]) &
            (spot_df["volume_ratio"] > settings.NEAR_LIMIT_VOL_RATIO)
        )
        spot_df["_signal_low_open_rally"] = (
            (spot_df["open"].astype(float) > 0) &  # OHLC 数据缺失时（0值）不触发，避免垃圾判定
            (spot_df["open"].astype(float) < spot_df["pre_close"].astype(float) * settings.LOW_OPEN_DEV) &
            (spot_df["volume_ratio"] > settings.RALLY_VOL_RATIO) &
            (rally_strength > settings.RALLY_STRENGTH_MIN) &
            (spot_df["change_pct"] > 0)
        )
        spot_df["_signal_amplitude"] = (
            (spot_df["amplitude"] > settings.AMPLITUDE_SIGNAL_MIN) &
            (spot_df["volume_ratio"] > settings.RALLY_VOL_RATIO) &
            (spot_df["change_pct"] > settings.AMPLITUDE_CHANGE_MIN)
        )

        # 任一信号命中
        signal_hit = spot_df["_signal_burst"] | spot_df["_signal_near_limit"] | \
                     spot_df["_signal_low_open_rally"] | spot_df["_signal_amplitude"]
        hit_df = spot_df[signal_hit]

        # 按市场风格调整排序优先级
        priority_strategy = market_style.get("priority_strategy", "")
        if priority_strategy == "打板接力":
            hit_df = hit_df.sort_values(by=["_signal_near_limit", "change_pct", "volume_ratio"],
                                        ascending=[False, False, False])
        elif priority_strategy == "中军回踩":
            hit_df = hit_df.sort_values(by=["_signal_low_open_rally", "volume_ratio", "change_pct"],
                                        ascending=[False, False, True])
        elif priority_strategy == "避险抱团":
            hit_df = hit_df.sort_values(by=["_signal_amplitude", "volume_ratio"],
                                        ascending=[False, False])
        else:
            hit_df = hit_df.sort_values(by=["volume_ratio", "change_pct"], ascending=[False, False])

        if not hit_df.empty:
            burst_codes_for_fund = []
            # 全市场总成交额（未过滤，对齐券商软件口径）+ 均涨幅，循环外计算一次
            _total_amt = self._DF.get_market_total_amount()
            if not _total_amt or _total_amt <= 0:
                _total_amt = float(spot_df["amount"].sum()) if "amount" in spot_df.columns else 1e12
            _index_pct = float(spot_df["change_pct"].mean()) if "change_pct" in spot_df.columns else 0.0
            for _, row in hit_df.head(5).iterrows():
                code = str(row["code"])
                name = str(row["name"])
                price = float(row["price"])
                change_pct = float(row["change_pct"])
                vol_ratio = float(row["volume_ratio"])
                amt_billion = float(row["amt_billion"])

                # 当日已推送过该股，跳过
                if code in self._alerted_burst_codes:
                    continue

                # 归类信号
                signals = []
                if row["_signal_burst"]:
                    signals.append("点火异动")
                if row["_signal_near_limit"]:
                    signals.append("逼近封板")
                if row["_signal_low_open_rally"]:
                    signals.append("低开猛拉")
                if row["_signal_amplitude"]:
                    signals.append("振幅放量")
                signal_label = "+".join(signals)

                is_recommended = code in pending_codes

                pre_close = float(row.get("pre_close", price))

                # 推荐标的竞价条件验证：开盘涨幅满足 open_requirement + 当下未明显走弱
                rec_condition_met = True
                if is_recommended:
                    rec_info = next((r for r in pending_recs if r["code"] == code), None)
                    if rec_info and rec_info.get("open_requirement"):
                        open_req = rec_info["open_requirement"]
                        open_change = round((float(row.get("open", price)) - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                        rec_condition_met = self._check_open_requirement(open_change, open_req)
                        # 当下走势校验：高开标的相对开盘回落超过 REC_FADE_MAX（走弱）不自动买入
                        if rec_condition_met and open_change > 0 and (open_change - change_pct) > settings.REC_FADE_MAX:
                            rec_condition_met = False

                # 高质量信号 = 逼近封板 / 低开猛拉 / 点火异动(高质量)
                # 高质量信号判定跟随市场风格
                if priority_strategy == "打板接力":
                    is_high_signal = row["_signal_near_limit"]
                elif priority_strategy == "中军回踩":
                    is_high_signal = row["_signal_low_open_rally"]
                elif priority_strategy == "避险抱团":
                    is_high_signal = row["_signal_amplitude"]
                else:
                    # 共振 / 观望：所有信号均接受
                    is_high_signal = (
                        row["_signal_near_limit"] or
                        row["_signal_low_open_rally"] or
                        (row["_signal_burst"] and change_pct >= 5.0 and vol_ratio >= 5.0 and amt_billion >= 2.0)
                    )

                # 自动买入: 推荐标的 或 符合当前风格的高质量信号，且当前未封板（封板买不进）
                # 情绪周期约束：冰点/退潮期不自动买入，启动期只买推荐标的
                cycle_allow = self._cycle_stance.get("allow_auto_buy", True)
                cycle_only_rec = self._cycle_stance.get("only_recommended", False)
                if cycle_only_rec and not is_recommended:
                    cycle_allow = False
                # 竞价 LLM 结论门控：09:26 判定"放弃"的推荐标的不自动买入（仍推送异动提醒）
                auction_verdict = ""
                if is_recommended and rec_info:
                    auction_verdict = rec_info.get("auction_verdict", "") or ""
                verdict_blocked = is_recommended and auction_verdict == "放弃"
                # 当前已封板则买不进（封板判定覆盖一字板，含盘中打开后重新封死的）
                is_sealed = type(self)._is_limit_up(code, change_pct)
                # 市场风格否决权（④）：观望不自动买入（抱团是防御买入型，不在此列）
                style_blocks_buy = market_style.get("style") == "观望"
                # 板块因子（切片2）：该股所在板块阶段——退潮/冰点板块否决；高潮非主线否决（只做主线高潮龙头）
                sector_industry = self._get_stock_industry(code)
                sector_phase = self._get_sector_phase(sector_industry)
                sector_mainline = self._get_sector_is_mainline(sector_industry)
                sector_blocks_buy = sector_phase in ("退潮", "冰点") or (
                    sector_phase == "高潮" and not sector_mainline
                )
                should_buy = (not sector_blocks_buy) and (not style_blocks_buy) and (not verdict_blocked) and cycle_allow and not index_breaker_triggered and (
                    (is_recommended and rec_condition_met) or is_high_signal
                ) and not is_sealed
                buy_reason = ""
                if should_buy and code not in self._auto_bought_codes:
                    # 仓位管理检查
                    ai_holdings = HoldingManager.get_active_holdings(holding_type="AI_AUTO")
                    if len(ai_holdings) >= settings.MAX_AI_POSITIONS:
                        pass  # 超出最大持仓数，跳过
                    elif len(self._auto_bought_codes) >= settings.MAX_DAILY_BUYS:
                        pass  # 超出当日最大买入次数，跳过
                    elif self._is_daily_loss_breaker_triggered(ai_holdings):
                        pass  # 当日总亏损熔断，跳过
                    elif amt_billion >= settings.PATTERN_CHECK_MIN_AMOUNT and self._is_bad_intraday_pattern(code):
                        pass  # 分时形态不佳（冲高回落/放量滞涨），跳过
                    elif self._is_sector_concentrated(code, ai_holdings):
                        pass  # 板块集中度限制（同板块持仓已达上限），跳过
                    elif not any(h["code"] == code for h in ai_holdings):
                        self._auto_bought_codes.add(code)
                        buy_reason = "复盘推荐" if is_recommended else signal_label
                        # 成交滑点模型：模拟真实买入成本高于快照价（高位放量信号额外加滑点）
                        slippage = settings.AI_BUY_SLIPPAGE_PCT
                        if row["_signal_near_limit"] or vol_ratio >= settings.NEAR_LIMIT_VOL_RATIO:
                            slippage += settings.AI_BUY_SLIPPAGE_HOT_PCT
                        cost_price = round(price * (1 + slippage / 100), 2)
                        HoldingManager.add_holding(
                            code=code,
                            name=name,
                            cost_price=cost_price,
                            holding_type="AI_AUTO",
                            strategy=f"AI自动跟进({buy_reason})"
                        )
                        # 复盘推荐被买入 → 推荐状态流转 TRIGGERED，形成胜率闭环
                        if is_recommended and rec_info:
                            RecommendationManager.mark_triggered(rec_info["id"])
                        sector_tag = f" 板块[{sector_industry} {sector_phase}{'·主线' if sector_mainline else ''}]" if sector_phase else ""
                        bark_notifier.send(
                            title=f"🤖 [AI 自动买入] {name}({code})",
                            body=f"{buy_reason}标的 {name}({code}) 触发买入信号 (现价:{price}元, 成交成本:{cost_price}元含滑点{slippage:.2f}%, +{change_pct}%, 量比{vol_ratio}倍, 成交{amt_billion:.1f}亿{sector_tag})，已自动纳入 AI 持仓追踪！",
                            group="AI自动持仓",
                            level="timeSensitive"
                        )

                # 判断是否属于动态中军池（成交额 >= 20亿 即为大容量标的）
                is_core = amt_billion >= settings.CORE_POOL_MIN_AMOUNT

                # 从涨停池获取该股所在板块的实时涨停家数
                stock_industry = ""
                zt_df = self._zt_pool_cache
                if zt_df is not None and not zt_df.empty and "industry" in zt_df.columns:
                    match = zt_df[zt_df["code"].astype(str) == code]
                    if not match.empty:
                        stock_industry = str(match.iloc[0].get("industry", ""))
                sector_count = self._current_sector_counts.get(stock_industry, 1) if stock_industry else 1

                tags = StrategyAnalyzer.identify_tags(
                    stock_code=code,
                    stock_name=name,
                    change_pct=change_pct,
                    turnover_rate=float(row.get("turnover_rate", 0.0)),
                    is_in_core_pool=is_core,
                    market_total_amount=_total_amt,
                    index_change_pct=_index_pct,
                    sector_active_count=sector_count
                )

                # 质量过滤：推荐标的或高质量信号才推送
                is_quality = is_recommended or is_high_signal
                if not is_quality:
                    self._alerted_burst_codes.add(code)
                    continue

                # 推送标题按信号类型区分
                if row["_signal_near_limit"]:
                    emoji = "🎯"
                    trigger_title = f"[逼近封板-{signal_label}]"
                elif row["_signal_low_open_rally"]:
                    emoji = "🚀"
                    trigger_title = f"[低开猛拉-{signal_label}]"
                elif row["_signal_amplitude"] and not row["_signal_burst"]:
                    emoji = "📊"
                    trigger_title = f"[振幅放量-{signal_label}]"
                elif is_recommended:
                    emoji = "🔥"
                    trigger_title = "[复盘推荐股异动触发]"
                else:
                    emoji = "⚡"
                    trigger_title = f"[{signal_label}]"

                detail = f"量比:{vol_ratio}倍, 涨幅:{change_pct}%, 成交:{amt_billion:.1f}亿"
                if row["_signal_low_open_rally"]:
                    detail += f", 拉升强度:{float(rally_strength.loc[row.name]):.2f}"
                if row["_signal_amplitude"]:
                    detail += f", 振幅:{float(row['amplitude']):.1f}%"

                # LLM润色：非观望风格且当日调用未超限时才走 LLM。
                # LLM 调用(拉分时+生成)可能耗时数十秒，放到后台线程执行，避免阻塞 15 秒轮询。
                llm_call_limit = settings.MONITOR_LLM_ALERT_LIMIT
                llm_calls_today = getattr(self, '_llm_alert_calls_today', 0)
                alert_level = "timeSensitive" if is_recommended or row["_signal_near_limit"] else "active"
                if market_style["style"] != "观望" and llm_calls_today < llm_call_limit:
                    self._llm_alert_calls_today = llm_calls_today + 1
                    self._submit_llm_alert(
                        trigger_title=trigger_title,
                        code=code,
                        name=name,
                        price=price,
                        change_pct=change_pct,
                        vol_ratio=vol_ratio,
                        tags=tags,
                        detail=detail,
                        emoji=emoji,
                        level=alert_level,
                    )
                else:
                    alert_msg = (
                        f"【{trigger_title}】{name}({code})\n"
                        f"现价:{price}元 (+{change_pct}%) {detail}\n"
                        f"标签:[{','.join(tags)}]"
                    )
                    bark_notifier.send(
                        title=f"{emoji} {trigger_title} {name}({code}) +{change_pct}%",
                        body=alert_msg,
                        group="盘中异动",
                        level=alert_level
                    )
                self._alerted_burst_codes.add(code)
                burst_codes_for_fund.append(code)

            # 4.5 大单抱团监控：对命中标的验证主力资金（含流通市值分级阈值）
            cap_map = {}
            if not spot_df.empty and "circ_market_cap" in spot_df.columns:
                for _, srow in spot_df.iterrows():
                    c = str(srow.get("code", ""))
                    cap = float(srow.get("circ_market_cap", 0))
                    if c and cap > 0:
                        cap_map[c] = cap
            self._check_fund_inflow_alert(burst_codes_for_fund[:3], cap_map)


    def _load_today_ai_bought_codes(self) -> set:
        """从数据库恢复今日已 AI 买入的股票代码（buy_date=今天 且 AI_AUTO），保证盘中重启后当日计数不丢"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        codes = set()
        try:
            from database.services import db_manager
            from database.models import Holding
            session = db_manager.get_session()
            try:
                rows = session.query(Holding).filter(
                    Holding.buy_date == today,
                    Holding.holding_type == "AI_AUTO",
                ).all()
                codes = {h.code for h in rows}
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"恢复今日AI已买集合失败: {e}")
        if codes:
            logger.info(f"从数据库恢复今日 AI 已买 {len(codes)} 只")
        return codes

    def _get_stock_industry(self, code: str) -> str:
        """查询个股所属行业（当日缓存）：优先涨停池缓存，其次近期 daily_zt_pool"""
        cached = getattr(self, '_industry_cache', {})
        if code in cached:
            return cached[code]
        industry = ""
        zt = self._zt_pool_cache
        if zt is not None and not zt.empty and "industry" in zt.columns:
            m = zt[zt["code"].astype(str) == code]
            if not m.empty:
                industry = str(m.iloc[0].get("industry", "") or "")
        if not industry:
            try:
                from database.services import db_manager
                from database.models import DailyZtPool
                session = db_manager.get_session()
                try:
                    row = session.query(DailyZtPool).filter(
                        DailyZtPool.code == code
                    ).order_by(DailyZtPool.trade_date.desc()).first()
                    if row and row.industry:
                        industry = row.industry
                finally:
                    session.close()
            except Exception:
                pass
        cached[code] = industry
        return industry

    def _is_sector_concentrated(self, code: str, ai_holdings: list) -> bool:
        """板块集中度控制：候选股所属板块在 AI 持仓中已达 MAX_AI_SECTOR_POSITIONS 只则跳过"""
        if not ai_holdings:
            return False
        industry = self._get_stock_industry(code)
        if not industry:
            return False  # 查不到行业则不限制
        same = sum(1 for h in ai_holdings if h["code"] != code and self._get_stock_industry(h["code"]) == industry)
        return same >= settings.MAX_AI_SECTOR_POSITIONS

    def _ensure_sector_cycle_cache(self):
        """当日加载一次最新板块周期（sector_cycle → 板块阶段/主线 缓存）"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if getattr(self, '_sector_cycle_date', '') == today and hasattr(self, '_sector_cycle_info'):
            return
        self._sector_cycle_info = {}
        self._sector_cycle_date = today
        try:
            from database import SectorCycleManager
            for r in SectorCycleManager.get_sector_cycle(top=500):
                self._sector_cycle_info[r["sector"]] = {
                    "phase": r["phase"], "is_mainline": r["is_mainline"],
                }
        except Exception as e:
            logger.warning(f"加载板块阶段缓存失败: {e}")

    def _get_sector_phase(self, industry: str) -> str:
        """查询板块阶段（当日缓存），返回 冰点/启动/发酵/高潮/退潮 或 ""（未知）"""
        self._ensure_sector_cycle_cache()
        if not industry:
            return ""
        return self._sector_cycle_info.get(industry, {}).get("phase", "")

    def _get_sector_is_mainline(self, industry: str) -> bool:
        """查询板块是否主线（当日缓存）"""
        self._ensure_sector_cycle_cache()
        return self._sector_cycle_info.get(industry, {}).get("is_mainline", False)

    def _monitor_holdings(self, spot_df, active_holdings, market_max_lbc, market_zhaban_rate):
        """第7步：持仓卖出/止损信号监控 + 批量更新收益率（从 _check_realtime_market 拆出）"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")  # A股 T+1：当日买入不可当日卖
        # 7. 从数据库持仓表监控卖出/止损条件
        # 7.0 先处理跌停锁定中已破板的股票
        for code in list(self._pending_sell_codes):
            stock_data = spot_df[spot_df["code"] == code]
            if not stock_data.empty:
                row = stock_data.iloc[0]
                change_pct = float(row["change_pct"])
                if not type(self)._is_limit_down(code, change_pct):
                    # 破跌停板了，执行卖出
                    hold_match = [h for h in active_holdings if h["code"] == code]
                    if hold_match:
                        h = hold_match[0]
                        if h.get("buy_date") == today:
                            # T+1：当日买入即使破板也不能当日卖，等次日再处理
                            self._pending_sell_codes.discard(code)
                            continue
                        sell_px = float(row["price"])
                        if h.get("holding_type") == "AI_AUTO":
                            sell_px = round(sell_px * (1 - settings.AI_SELL_SLIPPAGE_PCT / 100), 2)  # AI卖出模拟滑点
                        HoldingManager.close_holding(code=code, holding_type=h.get("holding_type"), sell_price=sell_px)
                        self._pending_sell_codes.discard(code)
                        bark_notifier.send(
                            title=f"🔓 [破板卖出] {h.get('name')}({code})",
                            body=f"已破跌停板(现涨{change_pct}%)，自动执行卖出。",
                            group="卖出提醒",
                            level="timeSensitive"
                        )

        price_updates = []  # (code, price, holding_type)，循环内收集、循环后一次性批量写库
        for holding in active_holdings:
            code = holding["code"]
            stock_data = spot_df[spot_df["code"] == code]
            if not stock_data.empty:
                row = stock_data.iloc[0]
                curr_price = float(row["price"])
                curr_change_pct = float(row["change_pct"])
                is_zt = type(self)._is_limit_up(code, curr_change_pct)

                # 实时更新持仓最新价格与收益率 —— 先收集到列表，循环后一次性批量写库
                price_updates.append((code, curr_price, holding.get("holding_type")))

                # 如果盘中封过涨停，更新数据库状态（按 holding_type 定位，避免同 code 多持仓误更新）
                if is_zt and not holding.get("was_limit_up_today", False):
                    HoldingManager.update_was_limit_up(code, True, holding_type=holding.get("holding_type"))
                    holding["was_limit_up_today"] = True  # 本地同步，避免重复调用

                # A股 T+1：当日买入的持仓当日不可卖出，跳过卖出信号检查（价格更新照常进行）
                if holding.get("buy_date") == today:
                    continue

                # 获取真实的 MA5 均线价格
                ma_data = self._get_ma_prices(code)
                ma5_price = ma_data.get("ma5") or (curr_price * settings.MA5_FALLBACK_RATIO)

                # VWAP = 成交额 / 成交量（volume已统一为"股"）
                raw_vol = float(row.get("volume", 0))
                raw_amt = float(row.get("amount", 0))
                if raw_vol > 0 and raw_amt > 0:
                    vwap = raw_amt / raw_vol
                else:
                    vwap = (float(row.get("open", curr_price)) + curr_price +
                            float(row.get("high", curr_price)) +
                            float(row.get("low", curr_price))) / 4.0

                # 计算持仓天数
                buy_date_str = holding.get("buy_date", "")
                holding_days = 0
                if buy_date_str:
                    try:
                        buy_dt = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d")
                        holding_days = (datetime.datetime.now() - buy_dt).days
                    except ValueError:
                        pass

                signals = HoldingMonitor.check_sell_signals(
                    stock_code=code,
                    stock_name=holding.get("name", code),
                    current_price=curr_price,
                    cost_price=holding.get("cost_price", curr_price),
                    avg_vwap_price=vwap,
                    ma5_price=ma5_price,
                    is_limit_up=is_zt,
                    was_limit_up_today=holding.get("was_limit_up_today", False),
                    market_max_lbc=market_max_lbc if market_max_lbc > 0 else 5,
                    market_zhaban_rate=market_zhaban_rate if market_zhaban_rate > 0 else 20.0,
                    holding_days=holding_days,
                    buy_strategy=holding.get("buy_strategy", "")
                )

                for sig in signals:
                    sig_type = sig["type"]
                    # 去重：同股同类型卖出信号当日仅推送一次
                    if code not in self._alerted_sell_signals:
                        self._alerted_sell_signals[code] = set()
                    if sig_type in self._alerted_sell_signals[code]:
                        continue
                    self._alerted_sell_signals[code].add(sig_type)

                    # 触发卖出信号 → 自动平仓（跌停时不卖，等破板）
                    hold_type = holding.get("holding_type", "MANUAL")
                    if sig["level"] in ("CRITICAL", "HIGH"):
                        # 判断是否跌停

                        if type(self)._is_limit_down(code, curr_change_pct):
                            # 跌停板封死，暂不平仓，等破板
                            self._pending_sell_codes.add(code)
                            if code not in self._alerted_sell_signals.get("__dt_pending__", set()):
                                self._alerted_sell_signals.setdefault("__dt_pending__", set()).add(code)
                                bark_notifier.send(
                                    title=f"🔒 [跌停锁定] {holding.get('name')}({code})",
                                    body=f"触发 {sig['type']} 但当前跌停({curr_change_pct}%)，暂不平仓，等破跌停板后自动卖出。",
                                    group="卖出提醒",
                                    level="timeSensitive"
                                )
                            continue

                        sell_px = curr_price
                        if hold_type == "AI_AUTO":
                            sell_px = round(curr_price * (1 - settings.AI_SELL_SLIPPAGE_PCT / 100), 2)  # AI卖出模拟滑点
                        HoldingManager.close_holding(code=code, holding_type=hold_type, sell_price=sell_px)
                        self._pending_sell_codes.discard(code)
                        bark_notifier.send(
                            title=f"🚨 [{sig['type']}] {holding.get('name')}({code})",
                            body=sig["reason"] + "\n\n已自动标记为卖出，不再持续监控。",
                            group="卖出提醒",
                            level="timeSensitive"
                        )
                    else:
                        bark_notifier.send(
                            title=f"🚨 [{sig['type']}] {holding.get('name')}",
                            body=sig["reason"],
                            group="卖出提醒",
                            level="timeSensitive"
                        )

        # 批量写库：一次 session 更新所有持仓价格与收益率（避免每 15 秒逐只开 session）
        if price_updates:
            HoldingManager.batch_update_profit_rates(price_updates)

    def _submit_llm_alert(self, *, trigger_title: str, code: str, name: str,
                          price: float, change_pct: float, vol_ratio: float,
                          tags: list, detail: str, emoji: str, level: str):
        """后台线程执行 LLM 润色 + Bark 推送，避免同步 LLM 阻塞盘中轮询主循环"""
        def _task():
            try:
                alert_msg = DynamicSellAdvisor.format_alert_message(
                    trigger_type=trigger_title,
                    stock_code=code,
                    stock_name=name,
                    current_price=price,
                    change_pct=change_pct,
                    volume_ratio=vol_ratio,
                    strategy_tag=",".join(tags),
                    detail_info=detail
                )
                bark_notifier.send(
                    title=f"{emoji} {trigger_title} {name}({code}) +{change_pct}%",
                    body=alert_msg,
                    group="盘中异动",
                    level=level
                )
            except Exception as e:
                logger.warning(f"LLM 异动推送后台任务异常 ({name}({code})): {e}")
        _llm_alert_executor.submit(_task)
