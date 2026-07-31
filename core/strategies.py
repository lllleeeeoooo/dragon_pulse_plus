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
    def classify(emotion: dict, market_amount: float = 8000, baseline: float = 8000) -> dict:
        """
        :param emotion: {
            "height": 最高连板数,
            "zt_count": 涨停家数,
            "dt_count": 跌停家数,
            "zhaban_rate": 炸板率(%),
            "sentiment_index": 情绪综合分(0-100),
            "yield_rate": 昨日涨停今日溢价率(%),
        }
        :param market_amount: 今日预估成交额（亿元），默认 8000
        :param baseline: 20日均成交额基准（亿元），默认 8000
        :return: {"style": "...", "reason": "...", "priority_strategy": "..."}
        """
        height = emotion.get("height", 0)
        zt_count = emotion.get("zt_count", 0)
        dt_count = emotion.get("dt_count", 0)
        zhaban_rate = emotion.get("zhaban_rate", 0)
        sentiment_index = emotion.get("sentiment_index", 50)
        premium = emotion.get("yield_rate", 0)

        # ── 自适应容量因子 K = 今日预估 / 20日均值 ──
        k = MarketStyle._capacity_factor(market_amount, baseline)
        sqrt_k = math.sqrt(k)

        # 动态阈值
        dt_panic = int(10 * sqrt_k)      # 跌停线：枯水期更敏感
        zt_daban_min = int(30 * k)       # 打板涨停下限
        zt_daban_max = int(50 * k)       # 打板涨停上限
        zt_dip_min = int(40 * k)         # 低吸涨停下限
        zb_resonance_max = 25 / sqrt_k   # 共振炸板率上限(%)

        # ═══════════════════════════════════════════
        # 优先级 1：生存（冰点/抱团）
        # ═══════════════════════════════════════════
        if dt_count >= dt_panic:
            return {
                "style": "抱团",
                "reason": f"跌停{dt_count}家（动态线{dt_panic}），恐慌蔓延，绝对防守",
                "priority_strategy": "避险抱团",
                "capacity_factor": round(k, 2),
                "dt_panic": dt_panic,
            }
        if premium <= settings.PREMIUM_PANIC_THRESHOLD and dt_count >= 5:
            return {
                "style": "抱团",
                "reason": f"昨日涨停溢价{premium}%，主力活埋追高资金+跌停{dt_count}家，立刻避险",
                "priority_strategy": "避险抱团",
                "capacity_factor": round(k, 2),
                "dt_panic": dt_panic,
            }

        # ═══════════════════════════════════════════
        # 优先级 2：高潮（板块共振）
        # 涨停多时放宽炸板率容忍度：涨停>60则上限=35%，涨停>80则上限=45%
        # ═══════════════════════════════════════════
        if zt_count >= 80:
            zb_limit = 45  # 百股涨停，炸板率上限放宽到45%
        elif zt_count >= 60:
            zb_limit = 35
        else:
            zb_limit = zb_resonance_max
        if sentiment_index >= 55 and zhaban_rate < zb_limit and zt_count >= zt_dip_min:
            return {
                "style": "共振",
                "reason": f"情绪{sentiment_index}分+涨停{zt_count}家+炸板率{zhaban_rate}%（上限{zb_limit:.0f}%），全力进攻首板",
                "priority_strategy": "板块共振",
                "capacity_factor": round(k, 2),
            }

        # ═══════════════════════════════════════════
        # 优先级 3：分歧（高度接力）
        # ═══════════════════════════════════════════
        if height >= 5 and zt_daban_min <= zt_count <= zt_daban_max:
            if sentiment_index >= 45:
                return {
                    "style": "打板",
                    "reason": f"最高{height}板+涨停{zt_count}家（区间{zt_daban_min}~{zt_daban_max}），精选弱转强3进4/4进5",
                    "priority_strategy": "打板接力",
                    "capacity_factor": round(k, 2),
                }
            else:
                return {
                    "style": "观望",
                    "reason": f"高标{height}板但情绪仅{sentiment_index}分，警惕高标补跌",
                    "priority_strategy": "观望/跟随",
                    "capacity_factor": round(k, 2),
                }

        # ═══════════════════════════════════════════
        # 优先级 4：轮动（低吸修复）
        # ═══════════════════════════════════════════
        if zt_count >= zt_dip_min and height <= 3:
            return {
                "style": "低吸",
                "reason": f"涨停{zt_count}家（下限{zt_dip_min}）但最高{height}板，试错轮动期，低吸中军",
                "priority_strategy": "中军回踩",
                "capacity_factor": round(k, 2),
            }

        # 高标+涨停超上限 → 一致性过强，明日分歧概率大
        if height >= 5 and zt_count > zt_daban_max:
            if sentiment_index >= 70:
                return {
                    "style": "高潮",
                    "reason": f"最高{height}板+涨停{zt_count}家+情绪{sentiment_index}分，市场过于一致，明日必分歧。高位减仓，等分歧后做弱转强。",
                    "priority_strategy": "观望/跟随",
                    "capacity_factor": round(k, 2),
                }
            elif sentiment_index >= 40:
                return {
                    "style": "打板",
                    "reason": f"最高{height}板+涨停{zt_count}家（超区间上限{zt_daban_max}），情绪{sentiment_index}分，谨慎接力",
                    "priority_strategy": "打板接力",
                    "capacity_factor": round(k, 2),
                }
            else:
                return {
                    "style": "观望",
                    "reason": f"高标{height}板但涨停{zt_count}家偏多+情绪{sentiment_index}分，警惕退潮前兆",
                    "priority_strategy": "观望/跟随",
                    "capacity_factor": round(k, 2),
                }

        # ═══════════════════════════════════════════
        # 优先级 5：垃圾时间（观望）
        # ═══════════════════════════════════════════
        return {
            "style": "观望",
            "reason": f"涨停{zt_count}/跌停{dt_count}/情绪{sentiment_index}/K={k:.2f}，无明确攻防信号",
            "priority_strategy": "观望/跟随",
            "capacity_factor": round(k, 2),
        }


class StrategyAnalyzer:
    """
    战法标签化归因引擎
    针对盘中/盘后异动标的，自动化溯源打上战法标签：
    1. [中军回踩] - 低吸战法：核心中军池成员 + 触及均线(10/20日) + 缩量分时止跌
    2. [打板接力] - 打板战法：启动期首板/1进2 或 发酵期板块龙一龙二
    3. [二波预警] - 二波战法：过去30天人气总龙头 + 回撤30%-50% + 止跌反包大阳
    4. [避险抱团] - 抱团战法：大盘缩量/阴跌 + 标的高位横盘/特立独行不跌
    5. [板块共振] - 共振战法：大盘放量起跳 + 新题材联动 + 最先封板龙一
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
        根据量价和市场背景进行战法标签溯源
        """
        tags = []

        # 1. 溯源 A：二波战法预警 [二波预警]
        if is_past_dragon and (settings.SECOND_WAVE_RETREAT_MIN <= retreat_ratio_from_high <= settings.SECOND_WAVE_RETREAT_MAX):
            if change_pct > 3.0:
                tags.append("二波预警")

        # 2. 溯源 B：低吸战法 [中军回踩]
        if is_in_core_pool:
            if -2.0 <= change_pct <= 3.0:
                tags.append("中军回踩")

        # 3. 溯源 C：共振战法 [板块共振]
        if index_change_pct >= 1.0 and sector_active_count >= 3 and change_pct >= 5.0:
            tags.append("板块共振")

        # 4. 溯源 D：抱团战法 [避险抱团]
        if index_change_pct < -0.5 and market_total_amount < 7e11 and change_pct > 0.0:
            tags.append("避险抱团")

        # 5. 打板战法标签 [打板接力]
        if change_pct >= 9.5:
            tags.append("打板接力")

        return tags if tags else ["观望/跟随"]
