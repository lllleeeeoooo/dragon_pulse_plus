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

class _MonitorStyleMixin:
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
        """真炸板率：扣除炸板后回封的股票（涨停池 open_count>0 的视为回封成功）"""
        zt_df = self._zt_pool_cache
        zhaban_df = self._zhaban_pool_cache
        zt_count = len(zt_df) if zt_df is not None and not zt_df.empty else 0
        zhaban_count = len(zhaban_df) if zhaban_df is not None and not zhaban_df.empty else 0

        # 炸板后回封的股票：在涨停池里且 open_count > 0
        re_sealed_codes = set()
        if zt_df is not None and not zt_df.empty and "code" in zt_df.columns:
            if "open_count" in zt_df.columns:
                re_sealed = zt_df[zt_df["open_count"].astype(int) > 0]
                re_sealed_codes = set(re_sealed["code"].astype(str))

        # 真炸板 = 在炸板池但不在涨停池（炸了没回封）
        if zhaban_df is not None and not zhaban_df.empty and "code" in zhaban_df.columns:
            zhaban_codes = set(zhaban_df["code"].astype(str))
            true_zhaban = len(zhaban_codes - re_sealed_codes)
        else:
            true_zhaban = zhaban_count

        total = zt_count + true_zhaban
        if total > 0:
            return round((true_zhaban / total) * 100, 2)
        return 0.0


    @staticmethod
    def _is_limit_down(code: str, change_pct: float) -> bool:
        """判断是否跌停（按板块区分跌停线）"""
        code_str = str(code)
        if code_str.startswith(("30", "688")):
            return change_pct <= -19.8   # 双创 20cm 跌停
        return change_pct <= -9.8        # 主板 10cm 跌停


    @staticmethod
    def _is_limit_up(code: str, change_pct: float) -> bool:
        """判断是否涨停"""
        code_str = str(code)
        if code_str.startswith(("30", "688")):
            return change_pct >= 19.8
        return change_pct >= 9.8


    def _classify_intraday_style(self, spot_df: pd.DataFrame,
                                  market_max_lbc: int, market_zhaban_rate: float) -> dict:
        """用盘中已有数据拼出实时情绪向量，调用 MarketStyle 判定当前市场风格"""
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

        # 跌停/涨停估算（按板块区分涨跌停线）
        dt_count = 0
        estimate_zt = 0  # 涨停池兜底估算
        if not spot_df.empty:
            is_main = spot_df["code"].astype(str).str.match(r"^(60|00)")
            is_gem_star = spot_df["code"].astype(str).str.match(r"^(30|688)")
            # 跌停
            dt_count = int(
                ((is_main) & (spot_df["change_pct"] <= -9.8) |
                 (is_gem_star) & (spot_df["change_pct"] <= -19.8)).sum()
            )
            # 涨停兜底：涨停池缓存为空时用快照估算
            if zt_count == 0:
                estimate_zt = int(
                    ((is_main) & (spot_df["change_pct"] >= 9.8) |
                     (is_gem_star) & (spot_df["change_pct"] >= 19.8)).sum()
                )
                if estimate_zt > 0:
                    zt_count = estimate_zt

        # 炸板率兜底：炸板池为空但有涨停池
        if market_zhaban_rate == 0 and zhaban_count == 0 and zt_count > 0:
            # 用快照估算：涨幅曾接近涨停但回落
            pass  # 炸板率兜底较复杂，保持 0

        # 获取昨日涨停溢价（市场温度计），每 2 分钟刷新一次
        now = time.time()
        if now - getattr(self, '_premium_cache_time', 0) > 120:
            self._premium_cache = DataFetcher.get_yesterday_zt_premium()
            self._premium_cache_time = now
        premium = self._premium_cache.get("intraday_premium", 0)

        # 组装精简版情绪向量
        emotion = {
            "height": market_max_lbc,
            "zt_count": zt_count,
            "dt_count": dt_count,
            "zhaban_rate": market_zhaban_rate,
            "zhaban_count": zhaban_count,
            "breadth": zt_count - dt_count,
            "yield_rate": premium,      # ← 实际溢价，不再是 0
            "seal_force_ratio": 0,
            "yidong_bravery": 50,
            "_zt_source": "涨停池" if (zt_count > 0 and estimate_zt == 0) or zt_df is not None else
                          "快照估算(涨停池不可用)" if estimate_zt > 0 else "无数据",
            "_premium_source": self._premium_cache.get("source", ""),
        }

        # 情绪分公式 (动态权重, 0%溢价=50分中性锚点)
        _p2 = premium
        score_premium = max(min((_p2 + 3) * (100 / 7), 100), 0) if _p2 < 0 else max(min(50 + _p2 * (50 / 4), 100), 50)
        _hm2 = {0: 0, 1: 15, 2: 30, 3: 50, 4: 65, 5: 78, 6: 88, 7: 95}
        score_height = _hm2.get(market_max_lbc, 100) if market_max_lbc <= 7 else 100
        score_breadth = max(min(((zt_count - dt_count) + 40) * 1.0, 100), 0)  # 涨停多→高分
        score_support = max(100 - market_zhaban_rate * 2.5, 0)
        emotion["sentiment_index"] = round(
            score_premium * settings.PREMIUM_WEIGHT +
            score_breadth * settings.BREADTH_WEIGHT +
            score_height * settings.HEIGHT_WEIGHT +
            score_support * settings.SUPPORT_WEIGHT,
            1
        )

        # ── 自适应动态容量因子 K ──
        now_date = datetime.datetime.now().strftime("%Y%m%d")
        if getattr(self, '_baseline_date', '') != now_date:
            bl = DataFetcher.get_adaptive_baseline()
            self._baseline_ma = bl["ma_amount"]
            self._baseline_date = now_date
            self._baseline_source = bl["source"]
        baseline = getattr(self, '_baseline_ma', 8000)

        spot_total = DataFetcher.get_market_total_amount() if not spot_df.empty else 0
        now_amount = spot_total / 1e8  # 此刻累计(亿)

        # 预估全天：时间外推 + 昨日全天兜底
        est = DataFetcher.estimate_today_amount(spot_total)
        estimated_today = est["estimated"] if est.get("estimated", 0) > 0 else now_amount

        # K = 预估全天 / 昨日全天（盘后有真实数据时更准）
        style = MarketStyle.classify(emotion,
                                     market_amount=estimated_today,
                                     baseline=baseline)
        style["now_amount_billion"] = now_amount
        style["estimated_today_billion"] = estimated_today
        style["baseline_ma20_billion"] = baseline
        style["capacity_factor"] = MarketStyle._capacity_factor(estimated_today, baseline)
        # 附带上原始数据，供日志和 API 展示
        style["zt_count"] = emotion["zt_count"]
        style["dt_count"] = emotion["dt_count"]
        style["zhaban_count"] = emotion["zhaban_count"]
        style["zhaban_rate"] = emotion["zhaban_rate"]
        style["sentiment_index"] = emotion["sentiment_index"]
        style["height"] = emotion["height"]
        style["zt_source"] = emotion["_zt_source"]
        # 涨跌分布
        if not spot_df.empty:
            style["up_count"] = int((spot_df["change_pct"] > 0).sum())
            style["down_count"] = int((spot_df["change_pct"] < 0).sum())
            style["flat_count"] = int((spot_df["change_pct"] == 0).sum())
            style["limit_up_est"] = int((spot_df["change_pct"] >= 9.8).sum())
            style["limit_down_est"] = int(spot_df.apply(
            lambda r: MarketMonitor._is_limit_down(str(r["code"]), float(r["change_pct"])), axis=1).sum())
        style["score_premium"] = score_premium
        style["score_breadth"] = score_breadth
        style["score_height"] = score_height
        style["score_support"] = score_support
        style["premium_intraday"] = premium
        style["premium_opening"] = self._premium_cache.get("opening_premium", 0)
        style["total_count"] = self._premium_cache.get("total_count", 0)
        style["high_open_ratio"] = self._premium_cache.get("high_open_ratio", 0)
        style["positive_ratio"] = self._premium_cache.get("positive_ratio", 0)
        style["premium_source"] = emotion["_premium_source"]
        # 板块联动数据（Top5活跃板块）
        if self._current_sector_counts:
            top_sectors = sorted(self._current_sector_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            style["top_sectors"] = [{"name": s[0], "zt_count": s[1]} for s in top_sectors]
        # 情绪周期阶段
        style["cycle_phase"] = self._cycle_phase or "未知"
        style["cycle_stance"] = self._cycle_stance.get("stance", "未知")
        # 连板晋级率（每日首次计算后缓存）
        if getattr(self, '_promotion_rate_cache', None) is None:
            self._promotion_rate_cache = self._compute_promotion_rate()
        style["promotion_rate"] = self._promotion_rate_cache
        return style


    def _load_cycle_phase(self):
        """每日加载一次前一交易日的情绪周期阶段"""
        try:
            from database.services import SentimentManager
            from core.cycle_machine import EmotionCycleMachine
            recent = SentimentManager.get_recent_sentiments(days_lookback=1)
            if recent:
                self._cycle_phase = recent[0].get("cycle_stage", "")
            else:
                self._cycle_phase = ""
            self._cycle_stance = EmotionCycleMachine.get_trading_stance(self._cycle_phase or "冰点")
            logger.info(f"情绪周期: {self._cycle_phase or '无历史'} -> 操作: {self._cycle_stance.get('stance', '未知')}")
        except Exception as e:
            logger.warning(f"加载情绪周期失败: {e}")
            self._cycle_phase = ""
            self._cycle_stance = {"allow_auto_buy": True, "only_recommended": False, "stance": "未知"}


    def _compute_promotion_rate(self) -> float:
        """
        计算连板晋级率：昨日连板>=2的股票中，今日仍在涨停池的比例。
        反映市场"接力赚钱效应"强弱。每日首次刷新池时计算一次。
        """
        try:
            from core.trade_calendar import get_previous_trading_day
            yesterday = get_previous_trading_day()
            yesterday_zt = DataFetcher.get_zt_pool(date_str=yesterday)
            today_zt = self._zt_pool_cache

            if yesterday_zt is None or yesterday_zt.empty or "lbc" not in yesterday_zt.columns:
                return -1.0
            if today_zt is None or today_zt.empty:
                return 0.0

            # 昨日连板>=2的股票
            relay_stocks = yesterday_zt[yesterday_zt["lbc"].astype(int) >= 2]
            if relay_stocks.empty:
                return -1.0

            relay_codes = set(relay_stocks["code"].astype(str))
            today_codes = set(today_zt["code"].astype(str))
            promoted = relay_codes & today_codes

            rate = round(len(promoted) / len(relay_codes) * 100, 1)
            return rate
        except Exception as e:
            logger.debug(f"计算晋级率失败: {e}")
            return -1.0


