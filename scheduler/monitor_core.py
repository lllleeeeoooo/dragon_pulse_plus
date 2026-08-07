import time
import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from core.emotion_index import EmotionVector
from core.strategies import MarketStyle
from core.signal_flags import compute_signal_flags
from core.holding_monitor import HoldingMonitor
from core.trade_calendar import is_trading_day, get_previous_trading_day, count_trading_days
from core.regulatory_yidong import RegulatoryYidongCalculator
from llm.sell_advisor import DynamicSellAdvisor
from notifier.bark import bark_notifier
from database.services import HoldingManager, RecommendationManager
from database.market_data import MarketIndexManager

logger = logging.getLogger(__name__)

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
        # 『买入决策被推迟』的候选独立去重：封板/复核失败/LLM预算用尽时推送一次"待重评"提醒，
        # 但不锁 _alerted_burst_codes——破板/快照恢复/预算空出后下轮仍可重新评估买入（审查#3/#4）
        self._deferred_alerted_codes: set = set()
        # 盘前简报盘中自愈每日标记：交易时段首轮检查当日简报是否缺失（08:30 定时任务未跑时补发一次）
        self._pre_market_self_heal_done: bool = False
        self._alerted_zhaban_codes: set = set()
        self._auto_bought_codes: set = set()
        # 尾盘博弈独立预算：当日已买入的 AI_TAIL 持仓 code 集合（独立于 _auto_bought_codes）
        self._tail_auto_bought_codes: set = set()
        # 龙头二波独立预算：当日已买入的 AI_SW 持仓 code 集合
        self._sw_auto_bought_codes: set = set()
        self._emotion_top_alerted_today: bool = False
        self._consistency_alerted_today: bool = False
        self._pending_sell_codes: set = set()  # 跌停中待卖出的股票
        self._alerted_sell_signals: Dict[str, set] = {}  # code -> {signal_type, ...}
        # "code:sig_type" -> 卖出 LLM 判"持有"冷却到期时间戳。
        # 按 (code, sig_type) 键控：一条信号判持有只冷却该信号，不蒙住同股更严重的新信号（审查#2）
        self._llm_sell_hold_until: Dict[str, float] = {}
        # "code:sig_type" -> 卖出 LLM 判"持有"时的决策价（冷却破除判断：急跌≥阈值打破冷却）
        self._llm_sell_hold_price: Dict[str, float] = {}
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
        # 看门狗：主循环心跳 + 疑似卡死告警（数据源挂起/网络阻塞时主线程阻塞，看门狗线程仍可跑）
        self._heartbeat: float = time.time()
        self._watchdog_alerted: bool = False


    def _reset_daily_state(self):
        """新交易日重置所有去重集合，输出盘前启动日志"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if self._alert_date != today:
            self._alerted_burst_codes.clear()
            self._deferred_alerted_codes.clear()
            self._alerted_zhaban_codes.clear()
            self._pre_market_self_heal_done = False  # 盘前简报自愈每日一次（新交易日重置）
            # 重启/换日后从数据库重建当日已买入集合（避免盘中重启导致 MAX_DAILY_BUYS 计数清零）
            self._auto_bought_codes = self._load_today_ai_bought_codes()
            self._tail_auto_bought_codes = self._load_today_tail_bought_codes()
            self._sw_auto_bought_codes = self._load_today_sw_bought_codes()
            self._emotion_top_alerted_today = False
            self._consistency_alerted_today = False
            self._pending_sell_codes.clear()
            self._alerted_sell_signals.clear()
            self._llm_sell_hold_until.clear()
            self._llm_sell_hold_price.clear()
            self._rec_auction_buy_evaluated = set()  # 推荐标的 09:26 一次性评估标记（新交易日重置）
            self._alert_date = today
            self._ma_cache.clear()
            self._pattern_cache.clear()
            self._industry_cache.clear()
            self._startup_logged = False  # 允许下一天输出启动日志
            global _circuit_breaker_alerted
            self._circuit_breaker_alerted = False
            _circuit_breaker_alerted = False
            # 审计①：清空竞价预判缓存，避免跨日读到前一天预判喂给 09:26 竞价 LLM
            try:
                import scheduler.monitor_auction as _ma
                _ma._auction_prediction_cache = ""
            except Exception:
                pass
            self._prev_seal_amounts.clear()
            self._alerted_seal_decay_codes.clear()
            self._prev_sector_counts.clear()
            self._alerted_sector_names.clear()
            self._current_sector_counts.clear()
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
        ai_count = sum(1 for h in holdings if h.get("holding_type") in ("AI_AUTO", "AI_TAIL", "AI_SW"))
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

    def is_tail_end_time(self) -> bool:
        """判断是否在尾盘博弈窗口 (14:30-15:00)——指南：避开 14:00-14:30 跳水，买在走势定型后"""
        now = datetime.datetime.now()
        if now.weekday() >= 5:
            return False
        current_time = now.time()
        return datetime.time(14, 30) <= current_time <= datetime.time(15, 0)


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
        # 配置校验：周期预算必须小于看门狗阈值，否则预算检查在看门狗之后触发、失去保护
        if settings.MONITOR_CYCLE_BUDGET_SECONDS >= settings.WATCHDOG_STALL_SECONDS:
            logger.warning(
                f"MONITOR_CYCLE_BUDGET_SECONDS({settings.MONITOR_CYCLE_BUDGET_SECONDS}) "
                f">= WATCHDOG_STALL_SECONDS({settings.WATCHDOG_STALL_SECONDS})：周期预算在看门狗之后才触发，"
                f"等于关闭了『慢源提前收尾』保护，慢周期仍会触发看门狗重启。建议设 < 看门狗阈值(默认90)。")
        self._start_watchdog()  # 看门狗：主循环心跳停更时推送卡死告警
        self._notify_watchdog_recovered()  # 若为看门狗自动重启，推送『已恢复』并清理标记

        while self.is_running:
            self._heartbeat = time.time()  # 心跳：本轮开始前更新，卡死(网络阻塞)时不再更新
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

    def _start_watchdog(self):
        """启动看门狗线程：主循环心跳停更(数据源挂起/网络阻塞卡死)时推送告警。
        独立 daemon 线程——socket 阻塞会释放 GIL，故主线程卡死时看门狗仍能运行。"""
        try:
            import threading
            threading.Thread(target=self._watchdog_loop, daemon=True,
                             name="watchdog").start()
        except Exception as e:
            logger.warning(f"看门狗启动失败: {e}")

    def _watchdog_check(self) -> bool:
        """单次看门狗检查：心跳停更超过阈值 → 推送一次卡死告警并置位；心跳恢复 → 复位。
        返回 True 表示本次推送了告警。供 _watchdog_loop 与测试调用。"""
        try:
            stall = time.time() - self._heartbeat
            if stall > settings.WATCHDOG_STALL_SECONDS:
                if not self._watchdog_alerted:
                    self._watchdog_alerted = True
                    logger.error(
                        f"监控主循环疑似卡死 {stall:.0f} 秒（心跳停更，数据源挂起/网络阻塞），请检查/重启")
                    bark_notifier.send(
                        title="⚠️ [监控疑似卡死] 盘中监控停摆",
                        body=(f"主循环心跳已 {stall:.0f} 秒未更新（数据源挂起/网络阻塞），"
                              f"盘中监控已停摆。"),
                        group="系统告警",
                        level="timeSensitive",
                    )
                    if settings.WATCHDOG_AUTO_RESTART:
                        self._auto_restart()  # 推送后自动拉起新进程（自愈，冷却期内防循环）
                    return True
            else:
                self._watchdog_alerted = False  # 心跳恢复 → 复位，下次卡死可再告警
        except Exception as e:
            logger.warning(f"看门狗检查异常: {e}")
        return False

    def _watchdog_loop(self):
        """看门狗主循环：定期检查心跳是否停更，卡死推送一次，心跳恢复后复位可再次告警。"""
        while True:
            time.sleep(settings.WATCHDOG_CHECK_SECONDS)
            self._watchdog_check()

    def _auto_restart(self):
        """看门狗自动重启：检测到卡死时拉起新 main.py 进程并退出当前进程。
        冷却期内(距上次自动重启不足 N 分钟)再次卡死 → 停止自动重启防循环，交人工。
        通过 logs/.watchdog_restart 标记文件让新进程感知'是自动重启'并推送恢复通知。"""
        import os
        import subprocess
        import sys
        marker = os.path.abspath("logs/.watchdog_restart")
        try:
            if os.path.exists(marker) and \
                    (time.time() - os.path.getmtime(marker)) < \
                    settings.WATCHDOG_RESTART_COOLDOWN_MINUTES * 60:
                logger.error("看门狗冷却期内再次卡死，停止自动重启（疑似循环），需人工介入")
                bark_notifier.send(
                    title="🚨 [监控反复卡死] 自动重启已暂停",
                    body=f"冷却期({settings.WATCHDOG_RESTART_COOLDOWN_MINUTES}分钟)内再次卡死，"
                         f"已停止自动重启避免循环，请人工检查数据源/网络。",
                    group="系统告警", level="timeSensitive")
                return
            open(marker, "w").write(str(time.time()))
            bark_notifier.send(
                title="🔄 [监控自动重启] 检测到卡死，正在重启",
                body=f"主循环心跳停更超 {settings.WATCHDOG_STALL_SECONDS}s，"
                     f"已自动拉起新进程，恢复后会推送确认。",
                group="系统告警", level="timeSensitive")
            subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=os.getcwd(),
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                               getattr(subprocess, "DETACHED_PROCESS", 0)),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
        except Exception as e:
            logger.error(f"自动重启失败: {e}")
            return
        os._exit(0)  # 新进程已拉起，强制退出当前卡死进程

    def _notify_watchdog_recovered(self):
        """新进程启动：若存在自动重启标记，推送『已恢复』并清理标记（每次自动重启仅通知一次）。"""
        try:
            import os
            marker = os.path.abspath("logs/.watchdog_restart")
            if os.path.exists(marker):
                os.remove(marker)
                bark_notifier.send(
                    title="✅ [监控已自动恢复] 盘中监控重新上线",
                    body="看门狗自动重启完成，监控已恢复运行。",
                    group="系统告警", level="timeSensitive")
        except Exception as e:
            logger.warning(f"恢复通知失败: {e}")

    def _self_heal_pre_market_report(self):
        """
        盘中自愈兜底：当日 08:30 盘前简报若因进程未运行/系统睡眠等错过定时任务而未生成
        （pre_market_report 无当日记录），交易时段首次轮询时补生成+落库+推送，并告警说明。
        每日仅尝试一次（_pre_market_self_heal_done 标记，新交易日重置）。
        补发复用 job_pre_market（内部幂等落库 + 更新内存缓存，09:26 竞价/复盘仍能读到当日盘前上下文）。
        """
        if getattr(self, '_pre_market_self_heal_done', False):
            return
        self._pre_market_self_heal_done = True  # 先置位：当日只尝试一次，避免每轮轮询重复触发
        try:
            from database import PreMarketReportManager
            today = datetime.datetime.now().strftime("%Y%m%d")
            if PreMarketReportManager.get(today):
                return  # 当日已生成，无需自愈
            logger.warning("当日 08:30 盘前简报缺失，交易时段盘中补发...")
            from scheduler.pre_market import job_pre_market
            job_pre_market()
            bark_notifier.send(
                title="⚠️ [盘前简报补发] 08:30 未生成，已盘中补发",
                body="检测到今日盘前简报 08:30 未生成（可能因进程未运行或系统睡眠错过定时任务），"
                     "已自动补发并落库，竞价/复盘可正常读取。",
                group="盘前简报",
                level="timeSensitive",
            )
        except Exception as e:
            logger.error(f"盘中补发盘前简报失败: {e}")

    def _check_realtime_market(self):
        """
        单次轮询检测逻辑
        """
        # 周期时间预算起点：慢源(如东财限流后腾讯降级 ~80s)把单轮拖长时，超预算跳过非关键步骤，
        # 保证单轮不触发看门狗 120s 自动重启（重启环根因是单轮周期过长，治本于周期预算）
        self._cycle_started_at = time.time()
        # 交易日切换时重置去重状态
        self._reset_daily_state()

        # 0.5 盘中自愈：08:30 盘前简报缺失时补发一次（每日仅一次，不依赖 APScheduler 定时器）
        self._self_heal_pre_market_report()

        # 0. 刷新涨停/炸板池缓存（内部自带 60 秒间隔控制）
        self._refresh_pool_cache()
        # 周期内推进心跳：慢源(东财限流后腾讯/新浪降级)把单轮拖长时，让看门狗知道"仍在推进只是慢"，
        # 只对真正的单次卡死(>120s)报警，不再把"慢但活着的长周期"误判为停摆
        self._heartbeat = time.time()

        # 1. 获取全市场快照（可能最慢：多源降级 spot 可达数十秒）
        spot_df = self._DF.get_realtime_spot()
        self._heartbeat = time.time()
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

        # 7. 持仓卖出/止损信号监控 + 批量更新收益率（已拆到 _monitor_holdings）。
        # 前置到买入扫描之前：绝对止损等硬护栏不因买入 LLM 同步调用(20-60s)被拖后（审查#1）
        self._monitor_holdings(spot_df, active_holdings, market_max_lbc, market_zhaban_rate)
        self._heartbeat = time.time()  # 卖出/止损监控完成，推进心跳

        # 4. 扫描全市场抢筹信号 + 自动买入 + 推送（已拆到 _scan_signals）
        self._scan_signals(spot_df, market_style, pending_recs, pending_codes, index_breaker_triggered)
        self._heartbeat = time.time()  # 抢筹扫描完成，推进心跳

        # 周期时间预算：慢源拖累单轮超预算时跳过非关键策略/预警（尾盘/二波/炸板/情绪到顶/一致性），
        # 提前收尾避免触发看门狗重启；卖出监控与抢筹买入扫描已在前置完成（安全优先）
        if self._cycle_over_budget():
            logger.info(f"单轮监控周期超预算({settings.MONITOR_CYCLE_BUDGET_SECONDS}s)，跳过非关键步骤提前收尾")
            return

        # 尾盘博弈（14:30-15:00）：独立策略，闸门+LLM+AI_TAIL 持仓，次日早盘兑现（内部 is_tail_end_time 门控）
        self._scan_tail_game(spot_df, index_breaker_triggered)

        # 龙头二波（全天盘中）：历史龙头回撤30-50%止跌反包 → AI_SW 持仓，N天不创新高离场（内部 is_trading_time 门控）
        self._scan_second_wave(spot_df, index_breaker_triggered)

        # 5. 全市场高位连板股"炸板"监控（基于真实涨停池对比）
        self._check_zhaban_alert(spot_df)

        self._check_emotion_top_alert(market_max_lbc, market_zhaban_rate)

        self._check_consistency_alert()
        self._heartbeat = time.time()  # 本周期全部步骤完成，推进心跳

    def _cycle_over_budget(self) -> bool:
        """单轮监控周期是否已超出时间预算（慢源把单轮拖长时提前收尾，防触发看门狗 120s 重启）。
        未记录起点（异常路径）时保守返回 False，不误跳过关键步骤。"""
        start = getattr(self, "_cycle_started_at", None)
        if start is None:
            return False
        return time.time() - start > settings.MONITOR_CYCLE_BUDGET_SECONDS

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

    def _get_stock_recent_pct_from_cache(self, code: str, limit_pct: float) -> tuple:
        """从 daily_kline 缓存算【含今日涨停】的 3/10 日累计涨幅 (%, 现价按今日涨停价计)。
        缓存覆盖不足时返回 (0,0)，由调用方放行（未知即放行）。"""
        try:
            from database.connection import db_manager
            from database.models import DailyKline
            session = db_manager.get_session()
            try:
                rows = session.query(DailyKline).filter(
                    DailyKline.code == str(code)
                ).order_by(DailyKline.trade_date.desc()).limit(11).all()
            finally:
                session.close()
        except Exception:
            return 0.0, 0.0
        closes = [float(r.close) for r in rows if getattr(r, "close", 0)]
        if len(closes) < 3:
            return 0.0, 0.0
        closes = closes[::-1]  # 升序
        limit_price = closes[-1] * (1 + limit_pct / 100)  # 今日涨停价（假设封板）
        recent_3d = round((limit_price / closes[-3] - 1) * 100, 2)
        recent_10d = round((limit_price / closes[-10] - 1) * 100, 2) if len(closes) >= 10 else 0.0
        return recent_3d, recent_10d

    def _regulatory_blocks_buy(self, code: str) -> bool:
        """盘中监管异动闸门：今日涨停是否触发交易所异动公告/停牌核查（一级/二级），是则拦截买入。
        指数偏离度取 market_index 表；个股 3/10 日累计取 daily_kline 缓存（本地零网络）。
        按 code 每日缓存一次；数据不足不拦截（未知即放行）。"""
        if not settings.REGULATORY_GATE_ENABLED:
            return False
        today = datetime.datetime.now().strftime("%Y%m%d")
        cache = getattr(self, "_regulatory_block_cache", {})
        if getattr(self, "_regulatory_cache_date", "") != today:
            cache = {}
            self._regulatory_block_cache = cache
            self._regulatory_cache_date = today
        if code in cache:
            return cache[code]
        limit_pct = 20.0 if str(code).startswith(("30", "688")) else 10.0
        recent_3d, recent_10d = self._get_stock_recent_pct_from_cache(code, limit_pct)
        block = False
        if recent_3d or recent_10d:  # 数据足够才评估（0=缓存覆盖不足）
            index_3d, index_10d = MarketIndexManager.get_index_3d_10d(code)
            info = RegulatoryYidongCalculator.evaluate_stock_yidong(
                code=code, name="",
                recent_3d_pct=recent_3d, index_3d_pct=index_3d,
                recent_10d_pct=recent_10d, index_10d_pct=index_10d)
            block = info["level"] in ("WARNING_YIDONG", "CRITICAL_SERIOUS")
            if block:
                logger.info(f"[监管异动] {code} 买入拦截: {info['warning_msg'][:90]}")
        cache[code] = block
        return block

    @staticmethod
    def _compute_buy_cost(row, price: float, pre_close: float, vol_ratio: float) -> tuple:
        """成交成本模型 → (cost_price, slippage_pct)。
        普通信号按 AI_BUY_SLIPPAGE_PCT 滑点加价；逼近封板/高位放量信号默认按涨停价撮合
        （AI_BUY_NEAR_LIMIT_FILL_LIMIT=True，打板资金实际多撮合在涨停附近，原 0.5% 滑点对回测过于乐观；
        False 时退回 AI_BUY_SLIPPAGE_PCT + AI_BUY_SLIPPAGE_HOT_PCT 滑点模型）。"""
        slippage = settings.AI_BUY_SLIPPAGE_PCT
        if row.get("_signal_near_limit") or vol_ratio >= settings.NEAR_LIMIT_VOL_RATIO:
            if settings.AI_BUY_NEAR_LIMIT_FILL_LIMIT:
                # 逼近封板按涨停价成交（涨停线 = 昨收×(1+_limit_max%)）
                limit_price = pre_close * (1 + float(row["_limit_max"]) / 100)
                cost_price = round(limit_price, 2)
                if price > 0:
                    slippage = round((limit_price - price) / price * 100, 2)
                return cost_price, slippage
            slippage += settings.AI_BUY_SLIPPAGE_HOT_PCT
        cost_price = round(price * (1 + slippage / 100), 2)
        return cost_price, slippage

    def _scan_signals(self, spot_df, market_style, pending_recs, pending_codes, index_breaker_triggered):
        """第4步：扫描全市场抢筹信号 + 自动买入 + 推送（从 _check_realtime_market 拆出）"""
        # 4. 扫描全市场抢筹信号（四种类型）
        #    a) 点火异动: 放量 + 涨幅 3%~9%
        #    b) 逼近封板: 涨幅 8%~9.5% + 量比 > 5
        #    c) 低开猛拉: 低开 + 直线拉回 + 量比 > 3
        #    d) 振幅放量: 振幅 > 7% + 量比 > 3 + 涨幅 > 3%
        # 四类抢筹信号 + 尾盘博弈候选：与回测共用 compute_signal_flags（阈值口径一致）
        spot_df = compute_signal_flags(spot_df)
        hit_df = spot_df[spot_df["_signal_hit"]]

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

        # ---- 竞价"买入"指令执行权（切片：LLM 逐票判定=买入的推荐标的，无信号也进候选） ----
        # 09:26 竞价 LLM 明确判定"买入"的推荐标的 = 最高优先级执行指令，不再依赖命中技术信号；
        # 仍需过 open_requirement / 各闸门 / 封板 / 持仓限制。其余判定(观察/放弃/无verdict)维持原状。
        hit_df = self._merge_auction_buy_candidates(spot_df, hit_df, pending_recs)

        if not hit_df.empty:
            burst_codes_for_fund = []
            # 每轮买入 LLM 确认预算：控制同步 LLM(20-60s)对 15s 主循环的阻塞（审查#1）
            self._llm_buy_confirms_this_cycle = 0
            for _, row in hit_df.head(5).iterrows():
                code = str(row["code"])
                name = str(row["name"])
                price = float(row["price"])
                change_pct = float(row["change_pct"])
                vol_ratio = float(row["volume_ratio"])
                amt_billion = float(row["amt_billion"])

                # 当日已推送过该股，跳过（推荐标的同样锁：09:26 竞价一次性评估
                # 由 _merge_auction_buy_candidates 负责，推过一次后盘中不再重复评估/推送）
                if self._skip_alerted_burst(code, pending_codes):
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
                    if rec_info:
                        rec_condition_met = self._check_rec_buy_condition(
                            rec_info,
                            open_price=float(row.get("open", price)),
                            pre_close=pre_close,
                            change_pct=float(change_pct),
                        )

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
                # 封板买不进：推送一次『封板待重评』提醒（独立去重），但**不锁** _alerted_burst_codes。
                # 否则竞价买入候选第一轮就被推送锁入 _alerted_burst_codes，破板后
                # _skip_alerted_burst(行555) 恒 True → continue，买入评估永不重跑（审查#3）。
                # 竞价推荐候选的 _rec_auction_buy_evaluated 也不标记已评估，破板后自然重新进池。
                if is_sealed:
                    self._push_deferred_burst_alert(
                        code=code, name=name, price=price, change_pct=change_pct,
                        vol_ratio=vol_ratio, amt_billion=amt_billion,
                        title="封板待重评",
                        reason=f"已封板({change_pct:+.1f}%)当前买不进，破板后将重新评估买入")
                    continue
                # 市场风格否决权（④）：观望不自动买入（抱团是防御买入型，不在此列）
                # 推荐标的（09:26 竞价评估）无视大盘风格（观望不拦推荐买入）
                style_blocks_buy = market_style.get("style") == "观望" and not is_recommended
                # 板块因子（切片2）：该股所在板块阶段——退潮/冰点板块否决；高潮非主线否决（只做主线高潮龙头）
                sector_industry = self._get_stock_industry(code)
                sector_phase = self._get_sector_phase(sector_industry)
                sector_mainline = self._get_sector_is_mainline(sector_industry)
                sector_blocks_buy = sector_phase in ("退潮", "冰点") or (
                    sector_phase == "高潮" and not sector_mainline
                )
                # 概念因子（切片3）：该股全部概念均为负向（退潮/冰点/高潮非主线）才否决；
                # 存在任一可买概念（发酵/启动/主线高潮）即放行；无概念数据不否决（覆盖率有限）。
                concept_blocks_buy = settings.CONCEPT_GATE_ENABLED and self._get_concept_blocks_buy(code)
                concept_brief = self._get_stock_concept_tag(code)
                # 监管异动闸门（评审补充）：今日涨停将触发交易所异动/停牌核查的禁止买入
                regulatory_blocks_buy = self._regulatory_blocks_buy(code)
                should_buy = (not sector_blocks_buy) and (not concept_blocks_buy) and (not regulatory_blocks_buy) and (not style_blocks_buy) and (not verdict_blocked) and cycle_allow and not index_breaker_triggered and (
                    (is_recommended and rec_condition_met) or is_high_signal
                ) and not is_sealed
                # 可观测性：候选未过买入闸门时，落 INFO 日志标注具体拦截原因，便于排查"为什么没买"
                if not should_buy:
                    if regulatory_blocks_buy:
                        _block = "监管异动(今日涨停将触发异动/停牌核查)"
                    elif sector_blocks_buy:
                        _block = f"板块否决[{sector_industry or '-'} {sector_phase or '-'}{'·主线' if sector_mainline else '非主线'}]"
                    elif concept_blocks_buy:
                        _block = f"概念否决[{concept_brief or '全部概念明确负向'}]"
                    elif style_blocks_buy:
                        _block = "市场风格观望"
                    elif verdict_blocked:
                        _block = f"竞价判定放弃[{auction_verdict}]"
                    elif not cycle_allow:
                        _block = f"情绪周期[{self._cycle_stance.get('stance', '')}]禁止自动买入"
                    elif index_breaker_triggered:
                        _block = "大盘熔断"
                    elif is_recommended and not rec_condition_met:
                        _block = "推荐买点不满足(开盘/回落)"
                    elif not is_high_signal:
                        _block = "非高质量信号"
                    else:
                        _block = "其他(含封板)"
                    logger.info(f"[买入评估] {name}({code}) 跳过买入: {_block}")
                buy_reason = ""
                if should_buy and code not in self._auto_bought_codes:
                    # 廉价仓位闸门前置：先过闸门再调 LLM/复核，避免满仓时白烧
                    # LLM 决策 + 全市场快照复核阻塞 15s 监控主循环（审计#2）
                    ai_holdings = HoldingManager.get_active_holdings(holding_type="AI_AUTO")
                    if self._buy_gates_open(code, ai_holdings, amt_billion):
                        # 预算用尽：本轮不再同步调 LLM，候选留待下轮评估（不降级规则裸买），
                        # 把同步阻塞控制在每轮 N 次 LLM 以内（审查#1）。
                        # 仍推送一次『预算待重评』提醒（独立去重）但**不锁** _alerted_burst_codes：
                        # 避免低排名候选的异动告警被无限推迟、信号短暂而漏报（审查#3），
                        # 同时下轮预算空出后仍可重新评估买入。
                        if (self._llm_buy_confirms_this_cycle >= settings.LLM_BUY_CONFIRM_PER_CYCLE
                                or self._cycle_over_budget()):
                            logger.debug(f"本轮买入 LLM 确认预算已用尽或单轮超预算，{name}({code}) 推送待重评提醒")
                            self._push_deferred_burst_alert(
                                code=code, name=name, price=price, change_pct=change_pct,
                                vol_ratio=vol_ratio, amt_billion=amt_billion,
                                title="预算待重评",
                                reason="本轮 LLM 确认预算已用尽，暂不买入，下轮重新评估")
                            continue  # 未锁 → 下轮 15s 后重新评估
                        self._llm_buy_confirms_this_cycle += 1
                        # B方案：规则候选已通过，LLM 最终买入决定（失败降级回 rule）
                        decision_source, llm_allow = self._llm_confirm_buy(
                            code, name, price, change_pct, vol_ratio, signal_label, [signal_label])
                        if not llm_allow:
                            logger.info(f"[买入评估] {name}({code}) LLM 判观望/放弃，不买入")
                        if llm_allow:
                            # LLM 等待期间价格可能变化：买前用最新快照复核（封板/跌停/回落）
                            price, recheck_ok, recheck_retry = self._recheck_buy_after_llm(code, price)
                            if not recheck_ok:
                                logger.info(f"买前复核不通过(已封板/跌停/回落): {name}({code})，放弃买入")
                                if recheck_retry:
                                    # 审查#4：复核失败若因快照不可用/封板(可能恢复) → 不下最终锁，
                                    # 推送『复核待重评』提醒并留待下轮重新评估；快照恢复后不得被
                                    # _alerted_burst_codes 永久跳过。推荐标的同时撤回竞价一次性
                                    # 评估标记，避免 _rec_auction_buy_evaluated 把它锁死。
                                    if is_recommended:
                                        self._rec_auction_buy_evaluated.discard(code)
                                    self._push_deferred_burst_alert(
                                        code=code, name=name, price=price, change_pct=change_pct,
                                        vol_ratio=vol_ratio, amt_billion=amt_billion,
                                        title="复核待重评",
                                        reason="买前复核未通过(快照不可用/已封板)，暂不买入，下轮重新评估")
                                    continue
                                # retry=False（跌停/回落，快照数据可靠）→ 视为本轮已评估，
                                # 落到推送分支正常推送并锁（当日不再重复评估）
                            elif self._buy_gates_open(code, HoldingManager.get_active_holdings(holding_type="AI_AUTO"), amt_billion):
                                # LLM/复核耗时数秒，期间其他候选可能已买入 → 二次过闸门后再执行
                                self._auto_bought_codes.add(code)
                                buy_reason = "复盘推荐" if is_recommended else signal_label
                                # 成交滑点模型：普通信号按滑点加价；逼近封板/高位放量信号默认按涨停价撮合
                                cost_price, slippage = self._compute_buy_cost(row, price, pre_close, vol_ratio)
                                HoldingManager.add_holding(
                                    code=code,
                                    name=name,
                                    cost_price=cost_price,
                                    holding_type="AI_AUTO",
                                    strategy=f"AI自动跟进({'LLM' if decision_source == 'llm' else '规则'}-{buy_reason})",
                                    decision_source=decision_source
                                )
                                # 复盘推荐被买入 → 推荐状态流转 TRIGGERED，形成胜率闭环
                                if is_recommended and rec_info:
                                    RecommendationManager.mark_triggered(rec_info["id"])
                                sector_tag = f" 板块[{sector_industry} {sector_phase}{'·主线' if sector_mainline else ''}]" if sector_phase else ""
                                concept_tag = f" 概念[{concept_brief}]" if concept_brief else ""
                                bark_notifier.send(
                                    title=f"🤖 [AI 自动买入] {name}({code})",
                                    body=f"{buy_reason}标的 {name}({code}) 触发买入信号 (现价:{price}元, 成交成本:{cost_price}元含滑点{slippage:.2f}%, +{change_pct}%, 量比{vol_ratio}倍, 成交{amt_billion:.1f}亿{sector_tag}{concept_tag})，已自动纳入 AI 持仓追踪！",
                                    group="AI自动持仓",
                                    level="timeSensitive"
                                )

                # 质量过滤：推荐标的或高质量信号才进入主力资金检测
                is_quality = is_recommended or is_high_signal
                if not is_quality:
                    self._alerted_burst_codes.add(code)
                    continue

                # 已推送过的候选不再重复推送（买入评估已在更早位置完成）。
                # 含『待重评』deferred 候选：本轮买入评估已走完(未再被推迟) → 落最终锁，
                # 避免该候选下轮每 15s 重复评估/推送（审查#3/#4）
                if code in self._alerted_burst_codes or code in self._deferred_alerted_codes:
                    self._alerted_burst_codes.add(code)
                    continue

                # 【盘中异动推送已移除】：不再发送【触发信息】信号通知与 LLM 润色链。
                # 仅保留当日去重锁 + 主力资金检测输入。
                self._alerted_burst_codes.add(code)
                burst_codes_for_fund.append(code)

            # 4.5 大单抱团监控：对命中标的验证主力资金（含流通市值分级阈值）
            # 周期预算：慢源拖累单轮超预算时跳过（仅日志/提示类，非买入闸门，可牺牲）
            if not self._cycle_over_budget():
                cap_map = {}
                if not spot_df.empty and "circ_market_cap" in spot_df.columns:
                    for _, srow in spot_df.iterrows():
                        c = str(srow.get("code", ""))
                        cap = float(srow.get("circ_market_cap", 0))
                        if c and cap > 0:
                            cap_map[c] = cap
                self._check_fund_inflow_alert(burst_codes_for_fund[:3], cap_map)

    def _scan_tail_game(self, spot_df, index_breaker_triggered):
        """
        尾盘博弈（14:30-15:00）：按指南选低吸强势股（涨幅2-5%/放量/收阳/短上影/收盘≥均价）博次日高开。
        独立预算(TAIL_MAX_*)、闸门+LLM 风控、AI_TAIL 持仓、次日 _monitor_holdings 早盘兑现。
        独立策略，不污染 _alerted_burst_codes/风格质量过滤。
        """
        if not self.is_tail_end_time():
            return
        spot_df = compute_signal_flags(spot_df)
        candidates = spot_df[spot_df["_signal_tail_game"]]
        if candidates is None or candidates.empty:
            return
        candidates = candidates.sort_values(
            by=["volume_ratio", "change_pct"], ascending=[False, False])
        tail_codes = {h["code"] for h in HoldingManager.get_active_holdings(holding_type="AI_TAIL")}
        self._tail_llm_confirm_this_cycle = 0
        for _, row in candidates.head(max(settings.TAIL_MAX_DAILY_BUYS, 1) * 2).iterrows():
            code = str(row["code"])
            if code in self._tail_auto_bought_codes or code in tail_codes:
                continue
            name = str(row["name"])
            price = float(row["price"])
            change_pct = float(row["change_pct"])
            vol_ratio = float(row["volume_ratio"])
            amt_billion = float(row["amt_billion"])
            if not self._tail_gates_open(code, amt_billion, index_breaker_triggered):
                continue
            # 指南：上升趋势站上 5/10 日均线（MA 数据缺失放行）
            if not self._tail_above_ma(code, price):
                continue
            # 独立 LLM 预算：尾盘时段每轮最多确认 TAIL_LLM_PER_CYCLE 次
            if self._tail_llm_confirm_this_cycle >= settings.TAIL_LLM_PER_CYCLE:
                continue
            self._tail_llm_confirm_this_cycle += 1
            decision_source, llm_allow = self._llm_confirm_buy(
                code, name, price, change_pct, vol_ratio, "尾盘博弈", ["尾盘博弈"])
            if not llm_allow:
                logger.info(f"[尾盘博弈] {name}({code}) LLM 判观望，不买入")
                continue
            price, recheck_ok, _retry = self._recheck_buy_after_llm(code, price)
            if not recheck_ok:
                logger.info(f"[尾盘博弈] {name}({code}) 买前复核未通过，放弃")
                continue
            if not self._tail_gates_open(code, amt_billion, index_breaker_triggered):
                continue
            slippage = settings.AI_BUY_SLIPPAGE_PCT
            cost_price = round(price * (1 + slippage / 100), 2)
            HoldingManager.add_holding(
                code=code, name=name, cost_price=cost_price, holding_type="AI_TAIL",
                strategy="尾盘博弈-次日高开", decision_source=decision_source)
            self._tail_auto_bought_codes.add(code)
            # 通知里带上对应个股 MA5/MA10（_get_ma_prices 当日缓存，命中不联网）
            ma_info = ""
            try:
                _ma = self._get_ma_prices(code)
                _ma5 = _ma.get("ma5")
                _ma10 = _ma.get("ma10")
                if _ma5 and _ma10:
                    ma_info = f" 站上MA5:{_ma5:.2f}/MA10:{_ma10:.2f}"
            except Exception:
                pass
            bark_notifier.send(
                title=f"🤖 [AI 尾盘博弈买入] {name}({code})",
                body=(f"尾盘博弈标的 {name}({code}) 现价:{price}元(+{change_pct}%), 量比{vol_ratio}倍, "
                      f"成本:{cost_price}元(含滑点{slippage:.2f}%),{ma_info}，明日早盘兑现卖出。"),
                group="AI自动持仓", level="timeSensitive")

    def _tail_gates_open(self, code: str, amt_billion: float, index_breaker_triggered: bool) -> bool:
        """尾盘博弈独立闸门：独立持仓上限/当日次数 + 共享亏损熔断 + 已持仓 + 大盘熔断。"""
        tail_holdings = HoldingManager.get_active_holdings(holding_type="AI_TAIL")
        if len(tail_holdings) >= settings.TAIL_MAX_POSITIONS:
            logger.info(f"[尾盘博弈] {code} 闸门拦截: 尾盘持仓已达上限({len(tail_holdings)}/{settings.TAIL_MAX_POSITIONS})")
            return False
        if len(self._tail_auto_bought_codes) >= settings.TAIL_MAX_DAILY_BUYS:
            logger.info(f"[尾盘博弈] {code} 闸门拦截: 当日尾盘买入已达上限({len(self._tail_auto_bought_codes)}/{settings.TAIL_MAX_DAILY_BUYS})")
            return False
        if index_breaker_triggered:
            logger.info(f"[尾盘博弈] {code} 闸门拦截: 大盘熔断")
            return False
        if any(h["code"] == code for h in tail_holdings):
            logger.info(f"[尾盘博弈] {code} 闸门拦截: 已持仓")
            return False
        ai_all = (HoldingManager.get_active_holdings(holding_type="AI_AUTO") +
                  HoldingManager.get_active_holdings(holding_type="AI_TAIL"))
        if self._is_daily_loss_breaker_triggered(ai_all):
            logger.info(f"[尾盘博弈] {code} 闸门拦截: 当日亏损熔断")
            return False
        return True

    def _tail_above_ma(self, code: str, price: float) -> bool:
        """指南：个股处于上升趋势，站上 5/10 日均线。
        MA 数据缺失/获取失败时**明确告警并放行**（未知即放行，但告知该候选未验证站均线规则）。"""
        if not settings.TAIL_GAME_REQUIRE_MA:
            return True
        try:
            ma = self._get_ma_prices(code)
            ma5 = ma.get("ma5")
            ma10 = ma.get("ma10")
            if ma5 is None or ma10 is None:
                logger.warning(f"[尾盘博弈] {code} MA5/MA10 数据缺失(新股/数据源失败)，"
                               f"站上均线规则未验证，按放行处理")
                return True
            return price > ma5 and price > ma10
        except Exception as e:
            logger.warning(f"[尾盘博弈] {code} MA 检查异常({e})，站上均线规则未验证，按放行处理")
            return True

    def _sw_dragons(self) -> dict:
        """近30天历史龙头 {code: peak_price}，每日缓存（避免每轮查库）"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if getattr(self, '_sw_dragons_date', '') == today:
            return getattr(self, '_sw_dragons_cache', {})
        dragons = {}
        try:
            from database import DragonManager
            for d in DragonManager.get_recent_dragons(
                    days_lookback=settings.SECOND_WAVE_LOOKBACK_DAYS):
                dragons[str(d["code"])] = float(d["peak_price"])
        except Exception as e:
            logger.warning(f"[二波] 加载历史龙头失败: {e}")
        self._sw_dragons_date = today
        self._sw_dragons_cache = dragons
        return dragons

    def _scan_second_wave(self, spot_df, index_breaker_triggered):
        """
        龙头二波（全天盘中 9:30-15:00）：近30天历史龙头回撤 30%-50% 且当日涨幅>3%（止跌反包）。
        独立闸门+LLM → AI_SW 持仓；卖出：突破前高兑现 / N 天未创新高离场（_monitor_holdings AI_SW 分支）。
        独立策略，不污染白天信号去重/风格锁。
        """
        if not self.is_trading_time():
            return
        dragons = self._sw_dragons()
        if not dragons:
            return
        spot_df = compute_signal_flags(spot_df, dragons=dragons)
        candidates = spot_df[spot_df["_signal_second_wave"]]
        if candidates is None or candidates.empty:
            return
        candidates = candidates.copy()
        candidates["_retreat"] = (
            (candidates["_peak_price"].astype(float) - candidates["price"].astype(float)) /
            candidates["_peak_price"].astype(float))
        candidates = candidates.sort_values(by=["_retreat", "change_pct"],
                                            ascending=[True, False])  # 回撤越浅+涨幅高优先
        sw_codes = {h["code"] for h in HoldingManager.get_active_holdings(holding_type="AI_SW")}
        self._sw_llm_confirm_this_cycle = 0
        for _, row in candidates.head(max(settings.SW_MAX_DAILY_BUYS, 1) * 2).iterrows():
            code = str(row["code"])
            if code in self._sw_auto_bought_codes or code in sw_codes:
                continue
            name = str(row["name"])
            price = float(row["price"])
            change_pct = float(row["change_pct"])
            vol_ratio = float(row["volume_ratio"])
            amt_billion = float(row["amt_billion"])
            peak_price = float(row["_peak_price"])
            retreat = float(row["_retreat"])
            if not self._second_wave_gates_open(code, amt_billion, index_breaker_triggered):
                continue
            if not self._tail_above_ma(code, price):  # 复用：站上 MA5/MA10
                continue
            if self._sw_llm_confirm_this_cycle >= settings.SW_LLM_PER_CYCLE:
                continue
            self._sw_llm_confirm_this_cycle += 1
            decision_source, llm_allow = self._llm_confirm_buy(
                code, name, price, change_pct, vol_ratio, "二波预警", ["二波预警"])
            if not llm_allow:
                logger.info(f"[二波] {name}({code}) LLM 判观望，不买入")
                continue
            price, recheck_ok, _retry = self._recheck_buy_after_llm(code, price)
            if not recheck_ok:
                continue
            if not self._second_wave_gates_open(code, amt_billion, index_breaker_triggered):
                continue
            slippage = settings.AI_BUY_SLIPPAGE_PCT
            cost_price = round(price * (1 + slippage / 100), 2)
            HoldingManager.add_holding(
                code=code, name=name, cost_price=cost_price, holding_type="AI_SW",
                strategy=f"二波战法-PEAK{peak_price}", decision_source=decision_source)
            self._sw_auto_bought_codes.add(code)
            ma_info = ""
            try:
                _ma = self._get_ma_prices(code)
                if _ma.get("ma5") and _ma.get("ma10"):
                    ma_info = f" 站上MA5:{_ma['ma5']:.2f}/MA10:{_ma['ma10']:.2f}"
            except Exception:
                pass
            bark_notifier.send(
                title=f"🤖 [AI 二波买入] {name}({code})",
                body=(f"二波标的 {name}({code}) 现价:{price}元(+{change_pct}%), 前高{peak_price}元"
                      f"(回撤{retreat * 100:.1f}%), 成本:{cost_price}元(含滑点),{ma_info}。"
                      f"突破前高兑现 / {settings.SW_HOLD_DAYS}天未创新高离场。"),
                group="AI自动持仓", level="timeSensitive")

    def _second_wave_gates_open(self, code: str, amt_billion: float,
                                index_breaker_triggered: bool) -> bool:
        """二波独立闸门：独立持仓上限/当日次数 + 共享亏损熔断 + 已持仓 + 大盘熔断。"""
        sw_holdings = HoldingManager.get_active_holdings(holding_type="AI_SW")
        if len(sw_holdings) >= settings.SW_MAX_POSITIONS:
            logger.info(f"[二波] {code} 闸门拦截: 二波持仓已达上限({len(sw_holdings)}/{settings.SW_MAX_POSITIONS})")
            return False
        if len(self._sw_auto_bought_codes) >= settings.SW_MAX_DAILY_BUYS:
            logger.info(f"[二波] {code} 闸门拦截: 当日二波买入已达上限({len(self._sw_auto_bought_codes)}/{settings.SW_MAX_DAILY_BUYS})")
            return False
        if index_breaker_triggered:
            logger.info(f"[二波] {code} 闸门拦截: 大盘熔断")
            return False
        if any(h["code"] == code for h in sw_holdings):
            logger.info(f"[二波] {code} 闸门拦截: 已持仓")
            return False
        ai_all = (HoldingManager.get_active_holdings(holding_type="AI_AUTO") +
                  HoldingManager.get_active_holdings(holding_type="AI_TAIL") +
                  HoldingManager.get_active_holdings(holding_type="AI_SW"))
        if self._is_daily_loss_breaker_triggered(ai_all):
            logger.info(f"[二波] {code} 闸门拦截: 当日亏损熔断")
            return False
        return True

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

    def _load_today_tail_bought_codes(self) -> set:
        """从数据库恢复今日尾盘博弈已买入的代码（buy_date=今天 且 AI_TAIL），保证盘中重启后独立预算计数不丢"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        codes = set()
        try:
            from database.services import db_manager
            from database.models import Holding
            session = db_manager.get_session()
            try:
                rows = session.query(Holding).filter(
                    Holding.buy_date == today,
                    Holding.holding_type == "AI_TAIL",
                ).all()
                codes = {h.code for h in rows}
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"恢复今日尾盘博弈已买集合失败: {e}")
        if codes:
            logger.info(f"从数据库恢复今日尾盘博弈已买 {len(codes)} 只")
        return codes

    def _load_today_sw_bought_codes(self) -> set:
        """从数据库恢复今日二波已买入代码（buy_date=今天 且 AI_SW），保证盘中重启后独立预算计数不丢"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        codes = set()
        try:
            from database.services import db_manager
            from database.models import Holding
            session = db_manager.get_session()
            try:
                rows = session.query(Holding).filter(
                    Holding.buy_date == today,
                    Holding.holding_type == "AI_SW",
                ).all()
                codes = {h.code for h in rows}
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"恢复今日二波已买集合失败: {e}")
        if codes:
            logger.info(f"从数据库恢复今日二波已买 {len(codes)} 只")
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
            records = SectorCycleManager.get_sector_cycle(top=500)
            for r in records:
                self._sector_cycle_info[r["sector"]] = {
                    "phase": r["phase"], "is_mainline": r["is_mainline"],
                }
            # 断链6：freshness 校验——最新周期未达上一交易日则告警并标记
            self._sector_cycle_stale = not self._check_cycle_fresh(
                records[0]["trade_date"] if records else "", "板块")
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

    def _check_rec_buy_condition(self, rec_info, open_price: float,
                                 pre_close: float, change_pct: float) -> bool:
        """
        推荐标的是否满足买入条件：
        - 竞价判定"买入"(前提=满足已自证) → 跳过 open_requirement/竞价量能正则，信任竞价 LLM 实时判断
        - 观察/无verdict（未获竞价确认）→ 用 open_requirement + 竞价量能 正则防御
        - 无论哪种：相对开盘回落超过 REC_FADE_MAX（走弱）均不买（活的安全网）
        """
        open_change = round((float(open_price) - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
        auction_verdict = (rec_info or {}).get("auction_verdict", "")
        if auction_verdict != "买入":
            if (rec_info or {}).get("open_requirement"):
                if not self._check_open_requirement(open_change, rec_info["open_requirement"]):
                    return False
            if not self._check_auction_volume(rec_info):
                return False
        if open_change > 0 and (open_change - change_pct) > settings.REC_FADE_MAX:
            return False
        return True

    def _check_auction_volume(self, rec_info) -> bool:
        """
        竞价量能校验（断链3）：auction_vol_ratio 要求 vs 09:26 存储的 auction_amount。
        无要求/无金额/解析失败 → 放行（不因未知卡死）。
        """
        req = (rec_info or {}).get("auction_vol_ratio") or ""
        amount = (rec_info or {}).get("auction_amount")
        if not req or not amount:
            return True
        import re
        m = re.search(r"≥\s*([\d.]+)\s*(万|亿)", req)  # "竞价成交额≥1900万" / "≥0.19亿"
        if not m:
            return True  # 无法解析 → 放行
        threshold = float(m.group(1)) * (1e4 if m.group(2) == "万" else 1e8)
        if float(amount) >= threshold:
            return True
        logger.warning(f"竞价量能不足: {rec_info.get('name')}({rec_info.get('code')}) "
                       f"竞价{float(amount) / 1e4:.0f}万 < 要求[{req}]")
        return False

    def _check_cycle_fresh(self, latest_date: str, name: str) -> bool:
        """周期数据 freshness 校验（断链6）：最新周期日期是否达到上一交易日。
        落后 → 告警（闸门在用旧数据）并返回 False；异常时保守返回 True。"""
        try:
            expected = get_previous_trading_day(datetime.date.today())
            if latest_date and latest_date >= expected:
                return True
            logger.warning(f"{name}周期数据陈旧: 最新 {latest_date or '无'}, "
                           f"应为 ≥{expected}（盘后任务可能未执行，闸门在用旧数据）")
            return False
        except Exception:
            return True

    def _buy_gates_open(self, code: str, ai_holdings: list, amt_billion: float) -> bool:
        """买入闸门是否全部通过（True=可买入）。

        廉价检查集中在 LLM 调用之前：满仓/熔断/形态不佳时直接跳过，
        不白烧 LLM 决策 + 全市场快照复核（避免阻塞 15s 监控主循环，审计#2）。
        """
        if len(ai_holdings) >= settings.MAX_AI_POSITIONS:
            logger.info(f"[买入评估] {code} 闸门拦截: AI持仓已达上限({len(ai_holdings)}/{settings.MAX_AI_POSITIONS})")
            return False  # 超出最大持仓数
        if len(self._auto_bought_codes) >= settings.MAX_DAILY_BUYS:
            logger.info(f"[买入评估] {code} 闸门拦截: 当日买入次数已达上限({len(self._auto_bought_codes)}/{settings.MAX_DAILY_BUYS})")
            return False  # 超出当日最大买入次数
        if self._is_daily_loss_breaker_triggered(ai_holdings):
            logger.info(f"[买入评估] {code} 闸门拦截: 当日亏损熔断")
            return False  # 当日总亏损熔断
        if amt_billion >= settings.PATTERN_CHECK_MIN_AMOUNT and self._is_bad_intraday_pattern(code):
            logger.info(f"[买入评估] {code} 闸门拦截: 分时形态不佳(冲高回落/放量滞涨)")
            return False  # 分时形态不佳（冲高回落/放量滞涨）
        if self._is_sector_concentrated(code, ai_holdings):
            logger.info(f"[买入评估] {code} 闸门拦截: 板块集中度达上限")
            return False  # 板块集中度限制
        if any(h["code"] == code for h in ai_holdings):
            logger.info(f"[买入评估] {code} 闸门拦截: 已持仓")
            return False  # 已持仓
        return True

    def _llm_confirm_buy(self, code, name, price, change_pct, vol_ratio,
                         signal_label, tags) -> tuple:
        """
        B方案买入复核：规则候选已通过 → LLM 最终决定（失败降级回规则）。
        返回 (source, allow_buy)：source='llm'/'rule'；allow_buy=是否买入。
        """
        text = DynamicSellAdvisor.format_buy_decision(
            code=code, name=name, current_price=price, change_pct=change_pct,
            volume_ratio=vol_ratio, signal_label=signal_label, tags=tags or [signal_label])
        verdict = DynamicSellAdvisor._parse_verdict(text)
        if not verdict:
            return "rule", True   # LLM 失败 → 降级：按规则买
        if verdict == "买入":
            return "llm", True
        return "llm", False       # 观望/放弃 → 不买

    def _recheck_buy_after_llm(self, code: str, prev_price: float) -> tuple:
        """
        LLM 决策等待(~20s)期间价格可能变化：买前用最新快照复核。
        返回 (fresh_price, ok, retry)：
        - ok=True：可用 fresh_price 买入。
        - ok=False, retry=True：快照不可用/封板(可能恢复) → 未落买入标记，下轮监控
          重新评估，避免用 LLM 前旧价记录不可达成交，也不因瞬时故障把当日机会永久丢弃（审查#4）。
        - ok=False, retry=False：跌停/回落(快照数据可靠判不买) → 本候选当日评估结束。
        """
        try:
            fresh = DataFetcher.get_realtime_spot()
            if fresh is None or fresh.empty:
                logger.warning(f"买前复核快照不可用({code})，放弃买入（下轮重新评估）")
                return prev_price, False, True
            m = fresh[fresh["code"].astype(str) == str(code).zfill(6)]
            if m.empty:
                return prev_price, False, True  # 最新快照查不到该股 → 下轮重试
            row = m.iloc[0]
            fresh_price = float(row.get("price", prev_price) or prev_price)
            fresh_chg = float(row.get("change_pct", 0) or 0)
            # 已封板 → 买不进，但封板可能打开 → retry（破板后重新评估）
            if type(self)._is_limit_up(code, fresh_chg):
                return fresh_price, False, True
            # 已跌停 → 不该买，快照数据可靠 → final
            if type(self)._is_limit_down(code, fresh_chg):
                return fresh_price, False, False
            # 回落校验：开盘涨幅 - 最新涨幅 > REC_FADE_MAX → 冲高回落不追，数据可靠 → final
            open_p = float(row.get("open", 0) or 0)
            pre_close = float(row.get("pre_close", 0) or 0)
            open_chg = (open_p - pre_close) / pre_close * 100 if pre_close > 0 else 0
            if open_chg > 0 and (open_chg - fresh_chg) > settings.REC_FADE_MAX:
                return fresh_price, False, False
            return fresh_price, True, False
        except Exception as e:
            logger.warning(f"买前复核失败({code}): {e}，放弃买入（下轮重新评估）")
            return prev_price, False, True  # 复核异常 → 瞬时 → retry（fail-closed）

    def _llm_confirm_sell(self, holding: dict, sig: dict, curr_price: float,
                          curr_change_pct: float, holding_days: int) -> tuple:
        """
        B方案卖出复核：规则信号 → LLM 最终决定（失败降级回规则）。
        返回 (source, do_sell)：source='llm'/'rule'；do_sell=是否执行卖出。
        """
        h = dict(holding)
        h["hold_days"] = holding_days
        text = DynamicSellAdvisor.format_sell_decision(h, sig, curr_price, curr_change_pct)
        verdict = DynamicSellAdvisor._parse_verdict(text)
        if not verdict:
            return "rule", True   # LLM 失败 → 降级：按规则卖
        if verdict == "出货":
            return "llm", True
        return "llm", False       # 持有 → 不卖

    def _skip_alerted_burst(self, code: str, pending_codes) -> bool:
        """
        当日已推送过的股票是否跳过本轮候选。
        推荐标的只做"09:26 竞价后一次性评估"，推过一次后同样锁（盘中不再重复评估/推送）。
        """
        return code in self._alerted_burst_codes

    def _push_deferred_burst_alert(self, *, code: str, name: str, price: float,
                                   change_pct: float, vol_ratio: float,
                                   amt_billion: float, title: str, reason: str) -> bool:
        """
        给『买入决策被推迟』的候选推送一次"待重评"提醒（独立去重 _deferred_alerted_codes），
        但**不下** _alerted_burst_codes 最终锁：封板破板 / 快照恢复 / LLM 预算空出后，
        下轮 _skip_alerted_burst 仍放行，可重新评估买入（审查#3/#4）。
        返回 True=本次实际推送；False=已推送过(去重)。
        """
        if code in self._deferred_alerted_codes:
            return False
        self._deferred_alerted_codes.add(code)
        bark_notifier.send(
            title=f"⏳ [{title}] {name}({code}) +{change_pct}%",
            body=(f"{name}({code}) 现价:{price}元(+{change_pct}%, 量比:{vol_ratio}倍, "
                  f"成交:{amt_billion:.1f}亿)\n{reason}"),
            group="盘中异动",
            level="active",
        )
        return True

    def _sell_sig_set(self, code: str, holding_type: str) -> set:
        """
        卖出信号当日去重集合：按 (code, holding_type) 键控。
        同 code 多持仓(AI_AUTO+MANUAL，update_was_limit_up 注释明确支持)时，
        一仓卖出/标记去重不得抑制另一仓同 code 同类型的卖出信号与推送（审查#8）。
        """
        key = f"{code}:{holding_type}"
        return self._alerted_sell_signals.setdefault(key, set())

    def _merge_auction_buy_candidates(self, spot_df, hit_df, pending_recs) -> pd.DataFrame:
        """
        竞价"买入"指令执行权：09:26 竞价后**仅评估一次**（盘中不再重复评估）。
        判断=买入 且 前提=满足 的推荐标的 → 首次进候选池（无信号也进）评估买入；
        评估过的标记到 _rec_auction_buy_evaluated，之后盘中不再进池。
        - 判断=买入 但 前提=不满足/未声明 → 不执行，记录矛盾（LLM 自相矛盾，供复盘）
        - 补全信号列默认值，避免下游 row["_signal_*"] KeyError
        """
        try:
            _auction_buy_codes = set()
            contradicted = set()
            for r in (pending_recs or []):
                if r.get("auction_verdict") == "买入":
                    premise = r.get("auction_premise")
                    if premise == "满足":
                        _auction_buy_codes.add(str(r.get("code", "")))
                    else:
                        contradicted.add(str(r.get("code", "")))
            evaluated = getattr(self, "_rec_auction_buy_evaluated", set())
            # 矛盾记录只报一次（避免每轮刷屏）
            for c in contradicted - evaluated:
                logger.warning(f"竞价矛盾: {c} 判断=买入但前提不满足/未声明，不执行")
            # 一次性：已评估过的推荐标的盘中不再重复进池。
            # 注意：买入代码需确认存在于当前快照后才标记"已评估"——避免代码暂缺快照
            # (停牌/快照不全)时把 09:26 买入指令提前锁死，当天再出现也无法进池（审计#5）
            _new = _auction_buy_codes - evaluated
            if not _new:
                # 无新候选时也标记矛盾代码，避免每轮重复告警
                if contradicted - evaluated:
                    self._rec_auction_buy_evaluated = evaluated | contradicted
                return hit_df
        except Exception as _e:
            logger.warning(f"解析竞价买入判定失败: {_e}")
            return hit_df
        if not _auction_buy_codes:
            return hit_df
        try:
            _rec_buy_df = spot_df[spot_df["code"].astype(str).isin(_new)].copy()
            if _rec_buy_df.empty:
                # 新候选不在当前快照：不标记已评估，下轮快照齐全时再进池
                return hit_df
            # 封板标的暂不标记"已评估"：开盘一字封死买不进，若当天就锁死会把 09:26
            # 买入指令永久丢弃；破板后须能重新进池（审查#3）。
            _sealed_codes = {
                str(c) for c, chg in zip(_rec_buy_df["code"].astype(str),
                                         _rec_buy_df["change_pct"].astype(float))
                if type(self)._is_limit_up(c, chg)
            }
            _markable = set(_rec_buy_df["code"].astype(str)) - _sealed_codes
            # 仅对确实存在于快照且未封板的代码标记"已评估"（含矛盾代码，避免重复告警）
            self._rec_auction_buy_evaluated = evaluated | _markable | contradicted
            for _col in ("_signal_burst", "_signal_near_limit",
                         "_signal_low_open_rally", "_signal_amplitude"):
                _rec_buy_df[_col] = False
            return pd.concat([_rec_buy_df, hit_df], ignore_index=True).drop_duplicates(subset=["code"])
        except Exception as _e:
            logger.warning(f"竞价买入指令候选合并失败: {_e}")
            return hit_df

    def _ensure_concept_cycle_cache(self):
        """当日加载一次 概念周期(concept_cycle → 阶段/主线) + 概念成分股映射(concept_member → 个股概念)"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if getattr(self, '_concept_cache_date', '') == today and hasattr(self, '_concept_cycle_info'):
            return
        self._concept_cycle_info = {}
        self._concept_member_map = {}
        self._concept_cache_date = today
        try:
            from database import ConceptCycleManager
            records = ConceptCycleManager.get_concept_cycle(top=500)
            for r in records:
                self._concept_cycle_info[r["concept"]] = {
                    "phase": r["phase"], "is_mainline": r["is_mainline"],
                }
            # 断链6：freshness 校验——最新概念周期未达上一交易日则告警并标记
            self._concept_cycle_stale = not self._check_cycle_fresh(
                records[0]["trade_date"] if records else "", "概念")
            from database.connection import db_manager
            from database.models import ConceptMember
            session = db_manager.get_session()
            try:
                for code, name in session.query(
                        ConceptMember.stock_code, ConceptMember.concept_name).all():
                    # 按 (code, 概念名) 去重：concept_member 可能含跨日快照/同名重复行
                    # （如历史新浪源异常产生的 gn_x 占位行），加载层兜底避免概念名重复
                    lst = self._concept_member_map.setdefault(str(code).zfill(6), [])
                    if name not in lst:
                        lst.append(name)
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"加载概念周期缓存失败: {e}")

    def _get_stock_concepts(self, code: str) -> list:
        """查询个股所属概念（当日缓存），返回概念名列表或 []"""
        self._ensure_concept_cycle_cache()
        return self._concept_member_map.get(str(code).zfill(6), [])

    def _get_concept_blocks_buy(self, code: str) -> bool:
        """
        概念因子否决（切片3）：**只有全部概念均明确负向才否决**——
        退潮/冰点/高潮非主线；无概念数据 或 概念无周期记录(未知) 均放行
        （覆盖率有限，未知即放行，避免误杀数据源未覆盖题材的股票，如仅"参股金融"无记录）。
        存在任一可买概念（发酵/启动/主线高潮）同样放行。
        """
        concepts = self._get_stock_concepts(code)
        if not concepts:
            return False
        for c in concepts:
            info = self._concept_cycle_info.get(c, {})
            phase = info.get("phase", "")
            if phase in ("发酵", "启动"):
                return False
            if phase == "高潮" and info.get("is_mainline", False):
                return False
            if not phase:
                return False  # 无周期记录 = 未知 → 放行（与无概念数据一致）
            # phase ∈ {退潮, 冰点, 高潮非主线} → 该概念明确负向，继续看下一个
        return True

    def _get_stock_concept_tag(self, code: str) -> str:
        """生成个股概念标签（推送用）：取最优概念 概念名·阶段[·主线]"""
        concepts = self._get_stock_concepts(code)
        if not concepts:
            return ""
        scored = []
        for c in concepts:
            info = self._concept_cycle_info.get(c, {})
            phase = info.get("phase", "")
            if phase:
                scored.append((c, phase, info.get("is_mainline", False)))
        if not scored:
            return ""
        _RANK = {"发酵": 3, "启动": 2, "高潮": 2, "退潮": 0, "冰点": 0}

        def _key(item):
            c, phase, mainline = item
            return (1 if mainline else 0, _RANK.get(phase, 1))
        c, phase, mainline = max(scored, key=_key)
        return f"{c}·{phase}{'·主线' if mainline else ''}"

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
        _tdays_cache: Dict[str, int] = {}  # buy_date -> 交易日持仓天数（每轮每只持仓算一次）
        for holding in active_holdings:
            code = holding["code"]
            stock_data = spot_df[spot_df["code"] == code]
            if not stock_data.empty:
                row = stock_data.iloc[0]
                curr_price = float(row["price"])
                curr_change_pct = float(row["change_pct"])
                is_zt = type(self)._is_limit_up(code, curr_change_pct)

                # 实时更新持仓最新价格与收益率 —— 先收集到列表，循环后一次性批量写库
                price_updates.append((code, curr_price, holding.get("holding_type"), curr_change_pct))

                # 如果盘中封过涨停，更新数据库状态（按 holding_type 定位，避免同 code 多持仓误更新）
                if is_zt and not holding.get("was_limit_up_today", False):
                    HoldingManager.update_was_limit_up(code, True, holding_type=holding.get("holding_type"))
                    holding["was_limit_up_today"] = True  # 本地同步，避免重复调用

                # A股 T+1：当日买入的持仓当日不可卖出，跳过卖出信号检查（价格更新照常进行）
                if holding.get("buy_date") == today:
                    continue

                # 尾盘博弈次日早盘兑现：AI_TAIL 持仓次日必清，绝不过夜第2天。
                # 未高开→按开盘价兑现/止损；高开→动态移动止盈（从当日最高回落≥TAIL_GAME_TRAIL_PULLBACK_PCT
                # 实时价卖出，不再假设在开盘与最高点中点成交——原模型偏乐观），10:30 强平兜底（慢周期也不漏）。
                if holding.get("holding_type") == "AI_TAIL" and \
                        datetime.datetime.now().time() >= datetime.time(9, 30):
                    open_px = float(row.get("open", 0) or 0)
                    high_px = float(row.get("high", 0) or 0)
                    curr_price = float(row.get("price", open_px) or open_px)
                    cost = float(holding.get("cost_price", 0) or 0)
                    if cost <= 0 or open_px <= 0:
                        continue
                    if open_px < cost * (1 + settings.TAIL_GAME_OPEN_GAP_PCT / 100):
                        sell_px = open_px
                        reason = "尾盘博弈-次日开盘兑现"
                    elif datetime.datetime.now().time() >= datetime.time(10, 30):
                        sell_px = curr_price
                        reason = "尾盘博弈-次日10:30强平"
                    elif high_px > open_px and (high_px - curr_price) / high_px * 100 >= \
                            settings.TAIL_GAME_TRAIL_PULLBACK_PCT:
                        sell_px = curr_price
                        reason = "尾盘博弈-次日冲高回落止盈"
                    else:
                        continue  # 高开且未回落：持有等下一轮（10:30 强平兜底）
                    sell_px = round(sell_px * (1 - settings.AI_SELL_SLIPPAGE_PCT / 100), 2)
                    HoldingManager.close_holding(code=code, holding_type="AI_TAIL", sell_price=sell_px)
                    self._sell_sig_set(code, "AI_TAIL").add("尾盘博弈兑现")
                    logger.info(f"[尾盘博弈] {holding.get('name')}({code}) 兑现卖出: {reason} 卖价{sell_px}")
                    bark_notifier.send(
                        title=f"🔔 [尾盘博弈卖出] {holding.get('name')}({code})",
                        body=f"{reason}\n开盘:{open_px}元 卖出:{sell_px}元 (成本{cost}元)",
                        group="卖出提醒", level="timeSensitive")
                    continue  # 已兑现，不走常规卖出信号

                # 龙头二波卖出：突破前高→止盈兑现；N 天未创新高→离场（不创新高坚决离场）。
                # 绝对止损/破MA5/强止盈 仍走下方 check_sell_signals。
                if holding.get("holding_type") == "AI_SW":
                    peak = 0.0
                    _s = str(holding.get("buy_strategy", ""))
                    if "PEAK" in _s:
                        try:
                            peak = float(_s.split("PEAK")[-1])
                        except ValueError:
                            pass
                    high_px = float(row.get("high", 0) or 0)
                    if peak > 0 and high_px >= peak:
                        # 突破第一波前高 → 二波兑现止盈
                        sell_px = round(high_px * (1 - settings.AI_SELL_SLIPPAGE_PCT / 100), 2)
                        reason = "二波-突破前高兑现"
                        HoldingManager.close_holding(code=code, holding_type="AI_SW", sell_price=sell_px)
                        self._sell_sig_set(code, "AI_SW").add("二波兑现")
                        logger.info(f"[二波] {holding.get('name')}({code}) {reason} 卖价{sell_px}")
                        bark_notifier.send(
                            title=f"🔔 [二波卖出] {holding.get('name')}({code})",
                            body=f"{reason}\n突破前高{peak}元 卖出:{sell_px}元",
                            group="卖出提醒", level="timeSensitive")
                        continue
                    _days = 0
                    try:
                        _b = datetime.datetime.strptime(str(holding.get("buy_date", "")), "%Y-%m-%d")
                        _days = (datetime.datetime.now() - _b).days
                    except Exception:
                        pass
                    if peak > 0 and _days >= settings.SW_HOLD_DAYS and high_px < peak:
                        # N 天未创新高 → 坚决离场
                        sell_px = round(curr_price * (1 - settings.AI_SELL_SLIPPAGE_PCT / 100), 2)
                        reason = f"二波-{settings.SW_HOLD_DAYS}天未创新高离场"
                        HoldingManager.close_holding(code=code, holding_type="AI_SW", sell_price=sell_px)
                        self._sell_sig_set(code, "AI_SW").add("二波离场")
                        logger.info(f"[二波] {holding.get('name')}({code}) {reason} 卖价{sell_px}")
                        bark_notifier.send(
                            title=f"🔔 [二波卖出] {holding.get('name')}({code})",
                            body=f"{reason}\n未突破前高{peak}元 卖出:{sell_px}元 (成本{holding.get('cost_price')}元)",
                            group="卖出提醒", level="timeSensitive")
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

                # 计算持仓天数（按交易日：时间止损改交易日口径，避免自然日跨周末误触发）
                buy_date_str = holding.get("buy_date", "")
                holding_days = 0
                if buy_date_str:
                    if buy_date_str not in _tdays_cache:
                        try:
                            buy_dt = datetime.datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                            # count(buy_dt+1, today]：与旧口径"今日-买入日"一致，但只数交易日
                            _tdays_cache[buy_date_str] = count_trading_days(
                                buy_dt + datetime.timedelta(days=1), datetime.date.today())
                        except ValueError:
                            _tdays_cache[buy_date_str] = 0
                    holding_days = _tdays_cache[buy_date_str]

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
                    buy_strategy=holding.get("buy_strategy", ""),
                    day_high_price=float(row.get("high", 0) or 0),
                )

                hold_type = holding.get("holding_type", "MANUAL")
                for sig in signals:
                    sig_type = sig["type"]
                    # 去重：同股同类型卖出信号当日仅推送一次，按 (code, holding_type) 键控——
                    # 同 code 多持仓(AI_AUTO+MANUAL)一仓平仓不得抑制另一仓同类型卖出信号（审查#8）。
                    # 注意：标记必须放在"实际采取动作(卖出/推送)"之后——LLM 判"持有"不落标记，
                    # 下轮携带最新行情重新复核，避免一条陈旧判断把该信号锁死一整天（风控缺口）
                    if sig_type in self._sell_sig_set(code, hold_type):
                        continue

                    # 触发卖出信号 → 自动平仓（跌停时不卖，等破板）
                    if sig["level"] in ("CRITICAL", "HIGH"):
                        # 判断是否跌停

                        if type(self)._is_limit_down(code, curr_change_pct):
                            # 跌停板封死，暂不平仓，等破板
                            self._pending_sell_codes.add(code)
                            if code not in self._alerted_sell_signals.get("__dt_pending__", set()):
                                self._alerted_sell_signals.setdefault("__dt_pending__", set()).add(code)
                                self._sell_sig_set(code, hold_type).add(sig_type)
                                bark_notifier.send(
                                    title=f"🔒 [跌停锁定] {holding.get('name')}({code})",
                                    body=f"触发 {sig['type']} 但当前跌停({curr_change_pct}%)，暂不平仓，等破跌停板后自动卖出。",
                                    group="卖出提醒",
                                    level="timeSensitive"
                                )
                            continue

                        # B方案：除绝对止损(硬护栏)外，断板/破位/止盈/时间止损 由 LLM 复核决定
                        # 周期预算：单轮超预算时跳过 LLM 复核，直接按规则卖（安全优先，避免 LLM 阻塞把
                        # 单轮拖过看门狗阈值触发重启）
                        decision_source = "rule"
                        if sig_type != "绝对止损" and not self._cycle_over_budget():
                            # LLM 复核冷却：判"持有"后 N 秒内不重复咨询，避免持续信号每 15s
                            # 轮询都阻塞调 LLM(20-60s)；冷却到期后携最新行情重新复核（审计#3风控缺口的另一面）
                            _hold_key = f"{code}:{sig_type}"  # 冷却按信号类型区分（审查#2）
                            if time.time() < self._llm_sell_hold_until.get(_hold_key, 0):
                                # 冷却中急跌破局：现价较 LLM 决策价急跌 ≥LLM_SELL_COOLDOWN_BREAK_PCT%
                                # → 打破冷却立即重新评估（防闪崩/退潮时冷却期硬扛砸穿止损；绝对止损仍兜底）
                                _hold_px = self._llm_sell_hold_price.get(_hold_key, 0)
                                if not (_hold_px > 0 and
                                        curr_price < _hold_px * (1 - settings.LLM_SELL_COOLDOWN_BREAK_PCT / 100)):
                                    continue
                            decision_source, do_sell = self._llm_confirm_sell(
                                holding, sig, curr_price, curr_change_pct, holding_days)
                            if not do_sell:
                                self._llm_sell_hold_until[_hold_key] = time.time() + settings.LLM_SELL_HOLD_COOLDOWN_SECONDS
                                self._llm_sell_hold_price[_hold_key] = curr_price
                                logger.info(f"LLM 判持有: {holding.get('name')}({code}) {sig_type} 不卖出，"
                                            f"{settings.LLM_SELL_HOLD_COOLDOWN_SECONDS}s 内不重复咨询")
                                continue  # LLM 判持有 → 不卖（不落去重标记，冷却到期后重新复核）
                            # LLM 判出货 → 卖：清理该信号冷却状态
                            self._llm_sell_hold_until.pop(_hold_key, None)
                            self._llm_sell_hold_price.pop(_hold_key, None)

                        self._sell_sig_set(code, hold_type).add(sig_type)
                        sell_px = curr_price
                        if hold_type == "AI_AUTO":
                            sell_px = round(curr_price * (1 - settings.AI_SELL_SLIPPAGE_PCT / 100), 2)  # AI卖出模拟滑点
                        HoldingManager.close_holding(code=code, holding_type=hold_type, sell_price=sell_px)
                        self._pending_sell_codes.discard(code)
                        # 该持仓已卖出：同轮剩余信号一并落去重标记（仅本持仓类型），避免同持仓多信号
                        # 重复咨询 LLM + 对已平仓持仓重复"已自动标记为卖出"推送（审查#6）
                        self._sell_sig_set(code, hold_type).update(s["type"] for s in signals)
                        src_tag = "LLM" if decision_source == "llm" else "规则"
                        bark_notifier.send(
                            title=f"🚨 [{sig['type']}] {holding.get('name')}({code})",
                            body=f"{sig['reason']}\n\n[决策来源:{src_tag}] 已自动标记为卖出，不再持续监控。",
                            group="卖出提醒",
                            level="timeSensitive"
                        )
                    else:
                        self._sell_sig_set(code, hold_type).add(sig_type)
                        bark_notifier.send(
                            title=f"🚨 [{sig['type']}] {holding.get('name')}",
                            body=sig["reason"],
                            group="卖出提醒",
                            level="timeSensitive"
                        )

        # 批量写库：一次 session 更新所有持仓价格与收益率（避免每 15 秒逐只开 session）
        if price_updates:
            HoldingManager.batch_update_profit_rates(price_updates)
