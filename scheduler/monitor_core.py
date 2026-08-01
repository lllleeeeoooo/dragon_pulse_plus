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


    def _reset_daily_state(self):
        """新交易日重置所有去重集合，输出盘前启动日志"""
        today = datetime.datetime.now().strftime("%Y%m%d")
        if self._alert_date != today:
            self._alerted_burst_codes.clear()
            self._alerted_zhaban_codes.clear()
            self._auto_bought_codes.clear()
            self._emotion_top_alerted_today = False
            self._consistency_alerted_today = False
            self._pending_sell_codes.clear()
            self._alerted_sell_signals.clear()
            self._alert_date = today
            self._ma_cache.clear()
            self._startup_logged = False  # 允许下一天输出启动日志
            self._circuit_breaker_alerted = False
            self._prev_seal_amounts.clear()
            self._alerted_seal_decay_codes.clear()
            self._prev_sector_counts.clear()
            self._alerted_sector_names.clear()
            self._current_sector_counts.clear()
            self._llm_alert_calls_today = 0
            self._auction_snapshots.clear()
            self._auction_summary_sent = False
            self._index_breaker_alerted = False
            self._promotion_rate_cache = None
            HoldingManager.reset_all_limit_up_flags()
            RecommendationManager.expire_old_recommendations(before_date=today)
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
        bl = DataFetcher.get_adaptive_baseline()
        baseline = bl["ma_amount"]
        spot_total = DataFetcher.get_market_total_amount() if not spot_df.empty else 0
        now_amount = spot_total / 1e8  # 此刻累计(亿)
        est = DataFetcher.estimate_today_amount(spot_total)
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
        zhaban_count_raw = len(zhaban_df) if zhaban_df is not None and not zhaban_df.empty else 0
        # 真炸板 = 在炸板池但不在涨停池（扣掉回封的）
        if zt_df is not None and not zt_df.empty and "code" in zt_df.columns and \
           zhaban_df is not None and not zhaban_df.empty and "code" in zhaban_df.columns:
            zt_codes = set(zt_df["code"].astype(str))
            zhaban_codes = set(zhaban_df["code"].astype(str))
            zhaban_count = len(zhaban_codes - zt_codes)
        else:
            zhaban_count = zhaban_count_raw

        # --- 涨跌分布 ---
        up_count = 0
        down_count = 0
        if not spot_df.empty:
            up_count = int((spot_df["change_pct"] > 0).sum())
            down_count = int((spot_df["change_pct"] < 0).sum())

        # --- 昨日涨停溢价 ---
        premium_data = DataFetcher.get_yesterday_zt_premium()
        self._premium_cache = premium_data
        self._premium_cache_time = time.time()

        # --- 市场风格预判 ---
        market_max_lbc = self._get_market_max_lbc()
        market_zhaban_rate = self._get_market_zhaban_rate()
        dt_count = int(spot_df.apply(
            lambda r: MarketMonitor._is_limit_down(str(r["code"]), float(r["change_pct"])), axis=1).sum())
        emotion = {
            "height": market_max_lbc, "zt_count": zt_count, "dt_count": dt_count,
            "zhaban_rate": market_zhaban_rate, "zhaban_count": zhaban_count,
            "breadth": zt_count - dt_count, "yield_rate": premium_data.get("intraday_premium", 0),
            "seal_force_ratio": 0, "yidong_bravery": 50,
        }
        _p = emotion["yield_rate"]
        score_premium = max(min((_p + 3) * (100 / 7), 100), 0) if _p < 0 else max(min(50 + _p * (50 / 4), 100), 50)
        _hm = {0: 0, 1: 15, 2: 30, 3: 50, 4: 65, 5: 78, 6: 88, 7: 95}
        score_height = _hm.get(market_max_lbc, 100) if market_max_lbc <= 7 else 100
        score_breadth = max(min(((zt_count - dt_count) + 40) * 1.0, 100), 0)  # 涨停多→高分
        score_support = max(100 - market_zhaban_rate * 2.5, 0)
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
            self._zt_pool_cache = DataFetcher.get_zt_pool(date_str=today_str)
            self._zhaban_pool_cache = DataFetcher.get_zhaban_pool(date_str=today_str)
            self._pool_cache_time = now

            # 封单衰减检测
            self._check_seal_decay()
            # 板块联动检测
            self._check_sector_linkage()

            logger.debug("涨停/炸板池缓存已刷新")
        except Exception as e:
            logger.warning(f"刷新涨停/炸板池缓存失败: {e}")


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
        global _monitor_running, _last_monitor_cycle
        self.is_running = True
        _monitor_running = True
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
        spot_df = DataFetcher.get_realtime_spot()
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

        # 3. 从数据库读取当前持仓与待观察推荐标的
        active_holdings = HoldingManager.get_active_holdings()
        today_str = datetime.datetime.now().strftime("%Y%m%d")
        pending_recs = RecommendationManager.get_pending_recommendations(trade_date=today_str)
        pending_codes = {r["code"] for r in pending_recs}

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
        spot_df["_near_limit_min"] = spot_df["_limit_max"] * 0.84   # e.g. 9.5*0.84≈8.0 / 19.5*0.84≈16.4
        spot_df["_near_limit_max"] = spot_df["_limit_max"]

        spot_df["_signal_burst"] = (
            (spot_df["volume_ratio"] >= settings.VOL_BURST_THRESHOLD) &
            (spot_df["change_pct"] >= settings.PRICE_BURST_THRESHOLD) &
            (spot_df["change_pct"] < spot_df["_limit_max"])
        )
        spot_df["_signal_near_limit"] = (
            (spot_df["change_pct"] >= spot_df["_near_limit_min"]) &
            (spot_df["change_pct"] <= spot_df["_near_limit_max"]) &
            (spot_df["volume_ratio"] > 5)
        )
        spot_df["_signal_low_open_rally"] = (
            (spot_df["open"].astype(float) < spot_df["pre_close"].astype(float) * 0.98) &
            (spot_df["volume_ratio"] > 3) &
            (rally_strength > 0.8) &
            (spot_df["change_pct"] > 0)
        )
        spot_df["_signal_amplitude"] = (
            (spot_df["amplitude"] > 7) &
            (spot_df["volume_ratio"] > 3) &
            (spot_df["change_pct"] > 3)
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

                # 判断是否为一字板（无法买入）
                pre_close = float(row.get("pre_close", price))
                is_one_word_board = (price >= pre_close * 1.095) and (float(row.get("low", price)) == price)

                # 推荐标的竞价条件验证：检查开盘涨幅是否满足 open_requirement
                rec_condition_met = True
                if is_recommended:
                    rec_info = next((r for r in pending_recs if r["code"] == code), None)
                    if rec_info and rec_info.get("open_requirement"):
                        open_req = rec_info["open_requirement"]
                        open_change = round((float(row.get("open", price)) - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                        rec_condition_met = self._check_open_requirement(open_change, open_req)

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

                # 自动买入: 推荐标的 或 符合当前风格的高质量信号，且非一字板
                # 情绪周期约束：冰点/退潮期不自动买入，启动期只买推荐标的
                cycle_allow = self._cycle_stance.get("allow_auto_buy", True)
                cycle_only_rec = self._cycle_stance.get("only_recommended", False)
                if cycle_only_rec and not is_recommended:
                    cycle_allow = False
                should_buy = cycle_allow and not index_breaker_triggered and (
                    (is_recommended and rec_condition_met) or is_high_signal
                ) and not is_one_word_board
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
                    elif self._is_bad_intraday_pattern(code):
                        pass  # 分时形态不佳（冲高回落/放量滞涨），跳过
                    elif not any(h["code"] == code for h in ai_holdings):
                        self._auto_bought_codes.add(code)
                        buy_reason = "复盘推荐" if is_recommended else signal_label
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
                    market_total_amount=total_amt,
                    index_change_pct=index_pct,
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

                # LLM润色：非观望风格且当日调用未超限时才调用，否则用规则化文案
                llm_call_limit = 10
                llm_calls_today = getattr(self, '_llm_alert_calls_today', 0)
                if market_style["style"] != "观望" and llm_calls_today < llm_call_limit:
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
                    self._llm_alert_calls_today = llm_calls_today + 1
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
                    level="timeSensitive" if is_recommended or row["_signal_near_limit"] else "active"
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

        # 5. 全市场高位连板股"炸板"监控（基于真实涨停池对比）
        self._check_zhaban_alert(spot_df)

        # 6. 全市场情绪到顶预警（全局层面，每日仅推送一次）
        # 必须同时满足：连板极高 + 炸板率高 + 情绪崩塌（涨停多时炸板率高属于正常分歧）
        if (not self._emotion_top_alerted_today and
                market_max_lbc >= settings.EMOTION_TOP_MAX_LBC and
                market_zhaban_rate > settings.EMOTION_TOP_ZHABAN_RATE and
                self._current_market_style.get("sentiment_index", 50) < 40):
            self._emotion_top_alerted_today = True
            style_info = self._current_market_style
            bark_notifier.send(
                title="🚨 [情绪到顶预警] 全市场退潮风险",
                body=f"最高连板{market_max_lbc}板+炸板率{market_zhaban_rate}%+情绪仅{style_info.get('sentiment_index',0)}分，退潮前兆，建议落袋为安。",
                group="卖出提醒",
                level="timeSensitive"
            )

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

        # 7. 从数据库持仓表监控卖出/止损条件
        # 7.0 先处理跌停锁定中已破板的股票
        for code in list(self._pending_sell_codes):
            stock_data = spot_df[spot_df["code"] == code]
            if not stock_data.empty:
                row = stock_data.iloc[0]
                change_pct = float(row["change_pct"])
                is_main = str(code).startswith(("60", "00"))
                is_gem_star = str(code).startswith(("30", "688"))
                dt_limit = -19.8 if is_gem_star else -9.8
                if not MarketMonitor._is_limit_down(code, change_pct):
                    # 破跌停板了，执行卖出
                    hold_match = [h for h in active_holdings if h["code"] == code]
                    if hold_match:
                        h = hold_match[0]
                        HoldingManager.close_holding(code=code, holding_type=h.get("holding_type"), sell_price=float(row["price"]))
                        self._pending_sell_codes.discard(code)
                        bark_notifier.send(
                            title=f"🔓 [破板卖出] {h.get('name')}({code})",
                            body=f"已破跌停板(现涨{change_pct}%)，自动执行卖出。",
                            group="卖出提醒",
                            level="timeSensitive"
                        )

        for holding in active_holdings:
            code = holding["code"]
            stock_data = spot_df[spot_df["code"] == code]
            if not stock_data.empty:
                row = stock_data.iloc[0]
                curr_price = float(row["price"])
                curr_change_pct = float(row["change_pct"])
                is_zt = MarketMonitor._is_limit_up(code, curr_change_pct)

                # 实时计算并更新持仓最新价格与收益率 (%)
                HoldingManager.update_holding_profit_rate(code, curr_price)

                # 如果盘中封过涨停，更新数据库状态
                if is_zt and not holding.get("was_limit_up_today", False):
                    HoldingManager.update_was_limit_up(code, True)
                    holding["was_limit_up_today"] = True  # 本地同步，避免重复调用

                # 获取真实的 MA5 均线价格
                ma_data = self._get_ma_prices(code)
                ma5_price = ma_data.get("ma5") or (curr_price * 0.97)

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

                        if MarketMonitor._is_limit_down(code, curr_change_pct):
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

                        HoldingManager.close_holding(code=code, holding_type=hold_type, sell_price=curr_price)
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


