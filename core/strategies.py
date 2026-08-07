import logging
import math
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)


class MarketStyle:
    """
    市场风格识别与战法推荐（龙魂自适应动态阈值版）

    以 8000 亿成交额为基准线，通过「容量因子 K」对所有阈值做动态修正：
    - 枯水期（6000亿）：收紧阈值，敏感捕捉风险
    - 丰水期（1.2万亿+）：放宽阈值，容纳更高的波动

    判定优先级（按风险从高到低）：
    1. 冰点/抱团 — 溢价崩塌或跌停爆发
    2. 高潮/共振 — 涨停多+炸板少+情绪高
    3. 分歧/打板 — 高度在线+涨停适中
    4. 轮动/低吸 — 涨停多但高度低
    5. 垃圾/观望 — 无量震荡
    """

    @staticmethod
    def _capacity_factor(estimated_today: float, baseline: float) -> float:
        """
        容量因子 K = 今日预估成交额 / 20日均成交额

        含义：
        - K > 1.2：增量溢出行情，可激进
        - 0.8 < K < 1.2：平稳惯性，常规阈值
        - K < 0.8：流动性枯竭，只能抱团

        约束范围 [0.7, 1.5]
        """
        if baseline <= 0:
            return 1.0
        k = estimated_today / baseline
        return max(min(k, settings.CAPACITY_K_MAX), settings.CAPACITY_K_MIN)

    @staticmethod
    def _ramp(x: float, a: float, b: float) -> float:
        """线性爬升：x<=a→0，x>=b→1，[a,b] 之间线性。用于边界平滑，消除硬切悬崖。"""
        if b <= a:
            return 1.0 if x >= b else 0.0
        return max(0.0, min(1.0, (x - a) / (b - a)))

    @staticmethod
    def _trap(x: float, a: float, b: float, c: float, d: float) -> float:
        """平台函数：x∈[b,c]→1，两侧 [a,b] 与 [c,d] 线性衰减到 0。"""
        if x <= a or x >= d:
            return 0.0
        if b <= x <= c:
            return 1.0
        if a < x < b:
            return (x - a) / (b - a)
        return (d - x) / (d - c)

    @staticmethod
    def classify(emotion: dict, market_amount: float = 8000, baseline: float = 8000,
                 prev_style: str = None) -> dict:
        """
        市场风格分类 —— 平滑评分模型 + 滞后缓冲（防评分在阈值附近每 15s 频闪横跳）。
        :param emotion: {height, zt_count, dt_count, zhaban_rate, sentiment_index, yield_rate}
        :param market_amount: 今日预估成交额（亿元）
        :param baseline: 20日均成交额基准（亿元）
        :param prev_style: 上一轮风格（盘中连续调用传入；首次/盘后传 None 用基础阈值 0.5/0.55）
        :return: {"style","reason","priority_strategy","capacity_factor","confidence","scores"}
        """
        height = emotion.get("height", 0)
        zt_count = emotion.get("zt_count", 0)
        dt_count = emotion.get("dt_count", 0)
        zhaban_rate = emotion.get("zhaban_rate", 0)
        sentiment_index = emotion.get("sentiment_index", 50)
        premium = emotion.get("yield_rate", 0)

        # ── 容量因子 K 与动态阈值 ──
        k = MarketStyle._capacity_factor(market_amount, baseline)
        sqrt_k = math.sqrt(k)
        dt_panic = int(10 * sqrt_k)          # 跌停恐慌线
        zt_daban_min = int(30 * k)           # 打板涨停区间下限
        zt_daban_max = int(50 * k)           # 打板涨停区间上限
        zt_dip_min = int(40 * k)             # 低吸涨停下限
        zt_mid_min = int(20 * k)             # 中段活跃下限
        base_zb = 25 / sqrt_k                # 基础炸板容忍度（随 K 缩放）
        # 炸板容忍度随涨停家数平滑放宽（涨停越多容忍度越高），不再硬切档位
        zb_limit = base_zb * (1 + 0.5 * MarketStyle._ramp(zt_count, 40, 100))

        # ---- 各风格适宜度（0~1，加权平滑）----
        dt_part = MarketStyle._ramp(dt_count, dt_panic * 0.6, dt_panic * 1.2)
        prem_part = MarketStyle._ramp(-premium, 1.0, 3.0)
        s_baotuan = max(dt_part, 0.8 * prem_part)          # 生存：跌停恐慌 或 溢价崩塌

        s_gaochao = (0.5 * MarketStyle._ramp(zt_count, zt_daban_max, zt_daban_max * 1.6)
                     + 0.3 * MarketStyle._ramp(height, 5, 7)
                     + 0.2 * MarketStyle._ramp(sentiment_index, 60, 80))   # 顶部：超上限+高标+热

        s_gongzhen = (0.5 * MarketStyle._ramp(zt_count, zt_dip_min * 0.8, zt_dip_min * 1.2)
                      + 0.25 * (1 - MarketStyle._ramp(zhaban_rate, zb_limit * 0.5, zb_limit))
                      + 0.25 * MarketStyle._ramp(sentiment_index, 50, 70))  # 共振：涨停多+炸板低+情绪高

        s_daban = (0.4 * MarketStyle._trap(zt_count, zt_daban_min * 0.8, zt_daban_min,
                                           zt_daban_max, zt_daban_max * 1.2)
                   + 0.4 * MarketStyle._ramp(height, 3, 5)
                   + 0.2 * MarketStyle._ramp(sentiment_index, 40, 60))       # 打板：区间+高度+情绪

        s_dixi = (0.4 * MarketStyle._ramp(zt_count, zt_mid_min * 0.8, zt_mid_min * 1.2)
                  + 0.4 * (1 - MarketStyle._ramp(height, 2, 4))
                  + 0.2 * MarketStyle._ramp(sentiment_index, 40, 60))        # 低吸：涨停够+高度低

        scores = {
            "抱团": round(s_baotuan, 2), "高潮": round(s_gaochao, 2),
            "共振": round(s_gongzhen, 2), "打板": round(s_daban, 2), "低吸": round(s_dixi, 2),
        }

        def _ret(style, priority, reason, confidence):
            return {"style": style, "reason": reason, "priority_strategy": priority,
                    "capacity_factor": round(k, 2),
                    "confidence": round(confidence, 2), "scores": scores}

        _STYLE_PRIORITY = {"抱团": "避险抱团", "高潮": "观望/跟随", "共振": "板块共振",
                           "打板": "打板接力", "低吸": "中军回踩"}

        # ── 滞后缓冲：已处非观望风格且分数未跌破退出阈值 → 保持原风格（防每15s频闪横跳）──
        if settings.STYLE_HYSTERESIS_ENABLED and prev_style in _STYLE_PRIORITY:
            prev_score = scores.get(prev_style, 0.0)
            if prev_score >= settings.STYLE_EXIT_SCORE:
                return _ret(prev_style, _STYLE_PRIORITY[prev_style],
                            f"滞后保持：{prev_style} 分{prev_score:.2f}≥退出阈值{settings.STYLE_EXIT_SCORE}", prev_score)

        # 进入阈值：首次/盘后无 prev_style 用基础(风险0.5/攻击0.55)；盘中跌破退出后重新选择用更严阈值(0.55/0.60)
        if prev_style is None:
            thr_risk, thr_attack = 0.5, 0.55
        else:
            thr_risk, thr_attack = settings.STYLE_ENTER_RISK_SCORE, settings.STYLE_ENTER_ATTACK_SCORE

        # ── 抱团最高优先级仅当流动性枯竭(K<STYLE_BAOTUAN_K_MAX)且涨停稀少(<STYLE_BAOTUAN_ZT_MAX)；
        #    否则主线进攻/高潮优先，抱团降为后备（评审B6：主线爆发时杂毛跌停不再强制防守错失打板接力）──
        _baotuan_priority = (k < settings.STYLE_BAOTUAN_K_MAX and zt_count < settings.STYLE_BAOTUAN_ZT_MAX)
        if _baotuan_priority and s_baotuan >= thr_risk:
            return _ret("抱团", "避险抱团",
                        f"流动性枯竭(K={k:.2f}<{settings.STYLE_BAOTUAN_K_MAX})且涨停仅{zt_count}家，恐慌蔓延，绝对防守", s_baotuan)
        if s_gaochao >= thr_risk:
            return _ret("高潮", "观望/跟随",
                        f"最高{height}板+涨停{zt_count}家（超上限{zt_daban_max}）且情绪{sentiment_index}分，"
                        f"市场过于一致，明日必分歧。高位减仓，等分歧后做弱转强。", s_gaochao)

        # ── 攻击风格取最高分，需达进入阈值（主线进攻优先）──
        attack = {"共振": s_gongzhen, "打板": s_daban, "低吸": s_dixi}
        best_style = max(attack, key=attack.get)
        best_score = attack[best_style]
        if best_score >= thr_attack:
            if best_style == "共振":
                return _ret("共振", "板块共振",
                            f"情绪{sentiment_index}分+涨停{zt_count}家+炸板率{zhaban_rate}%（上限{zb_limit:.0f}%），全力进攻首板", best_score)
            if best_style == "打板":
                return _ret("打板", "打板接力",
                            f"最高{height}板+涨停{zt_count}家（区间{zt_daban_min}~{zt_daban_max}），精选弱转强3进4/4进5", best_score)
            return _ret("低吸", "中军回踩",
                        f"涨停{zt_count}家（下限{zt_mid_min}）+高度{height}板+情绪{sentiment_index}分，轮动修复期，低吸中军", best_score)

        # ── 抱团后备：活跃市场(K≥0.8或涨停≥15)里杂毛跌停不再主导风格，但两极端并存(涨停多+跌停多)仍防守 ──
        if s_baotuan >= thr_risk:
            return _ret("抱团", "避险抱团",
                        f"跌停{dt_count}家（动态线{dt_panic}）或溢价{premium}%，恐慌蔓延，绝对防守", s_baotuan)

        # ── 都不够 → 观望（带原因：哪个风格差多少达标）──
        gap = thr_attack - best_score
        return _ret("观望", "观望/跟随",
                    f"涨停{zt_count}/跌停{dt_count}/情绪{sentiment_index}/K={k:.2f}，各风格分均低"
                    f"（最高{best_style} {best_score:.2f}，差{gap:.2f}达标）——观望等方向", 0.0)

class StrategyAnalyzer:
    """
    战法标签化归因引擎
    ==================

    功能：对盘中/盘后异动标的，根据量价关系和市场背景自动打上战法标签。
    每个标的可能同时命中多个标签（如既满足打板接力又满足板块共振）。

    五大标签及触发条件：
    ┌──────────┬──────────┬──────────────────────────────────────────┐
    │ 标签      │ 战法      │ 量化条件                                  │
    ├──────────┼──────────┼──────────────────────────────────────────┤
    │ 打板接力  │ 打板战法  │ 涨幅>=涨停线(主板9.5%/双创19.5%)          │
    │ 二波预警  │ 二波战法  │ 前龙头 + 回撤30-50% + 涨幅>3%            │
    │ 中军回踩  │ 低吸战法  │ 核心池成员 + 涨幅在-2%~3%区间             │
    │ 板块共振  │ 共振战法  │ 大盘>=1% + 板块涨停>=3 + 个股>=5%        │
    │ 避险抱团  │ 抱团战法  │ 大盘<-0.5% + 缩量<7000亿 + 个股微涨>0    │
    │ 观望/跟随 │ 无        │ 以上条件均不满足                         │
    └──────────┴──────────┴──────────────────────────────────────────┘

    板块区分：打板接力标签按涨停线自动区分主板(9.5%)和双创(19.5%)。
    """

    @staticmethod
    def identify_tags(
        stock_code: str,
        stock_name: str,
        change_pct: float,
        turnover_rate: float,
        is_in_core_pool: bool = False,
        retreat_ratio_from_high: float = 0.0,
        is_past_dragon: bool = False,
        index_change_pct: float = 0.0,
        market_total_amount: float = 1e12,
        sector_active_count: int = 1
    ) -> List[str]:
        """
        根据量价和市场背景进行战法标签溯源。

        :param stock_code:              股票代码（用于区分主板/双创涨停线）
        :param change_pct:              当日涨幅%
        :param turnover_rate:           换手率%
        :param is_in_core_pool:         是否属于动态中军池成员
        :param retreat_ratio_from_high: 距最高点回撤比例（如0.40=回撤40%）
        :param is_past_dragon:          是否是30天内的历史人气龙头
        :param index_change_pct:        大盘指数涨跌幅%
        :param market_total_amount:     全市场总成交额（元）
        :param sector_active_count:     所属板块涨停家数

        :return: 标签列表，如 ["打板接力", "板块共振"]，至少返回 ["观望/跟随"]
        """
        tags = []

        # ====== 1. 二波战法：前期龙头回撤后止跌反包 ======
        # 条件：过去30天人气龙头 + 回撤在30%-50%区间 + 当日涨幅>3%
        if is_past_dragon and (settings.SECOND_WAVE_RETREAT_MIN <= retreat_ratio_from_high <= settings.SECOND_WAVE_RETREAT_MAX):
            if change_pct > 3.0:
                tags.append("二波预警")

        # ====== 2. 低吸战法：核心中军池成员回踩均线 ======
        # 条件：属于中军池成员 + 涨幅在-2%~3%缩量区间
        if is_in_core_pool:
            if -2.0 <= change_pct <= 3.0:
                tags.append("中军回踩")

        # ====== 3. 共振战法：大盘放量起跳+板块联动 ======
        # 条件：大盘>=1% + 板块>=3只涨停 + 个股>=5%
        if index_change_pct >= 1.0 and sector_active_count >= 3 and change_pct >= 5.0:
            tags.append("板块共振")

        # ====== 4. 抱团战法：大盘缩量阴跌时资金避险 ======
        # 条件：大盘<-0.5% + 全市场缩量<7000亿 + 个股逆势微涨
        if index_change_pct < -0.5 and market_total_amount < 7e11 and change_pct > 0.0:
            tags.append("避险抱团")

        # ====== 5. 打板战法：涨停板接力 ======
        # 条件：涨幅达到涨停线（主板10%/双创20%/北交所30%，按代码前缀自动区分，审计🟡②）
        if str(stock_code).startswith(("43", "83", "87", "92")):
            limit_line = 30.0    # 北交所 30cm
        elif str(stock_code).startswith(("30", "688")):
            limit_line = 19.5    # 双创 20cm
        else:
            limit_line = 9.5     # 主板 10cm
        if change_pct >= limit_line:
            tags.append("打板接力")

        return tags if tags else ["观望/跟随"]
