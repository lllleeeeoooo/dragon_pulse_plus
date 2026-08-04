"""
板块情绪周期状态机（东财行业维度）
====================================
服务于"主线板块 → 板块阶段 → 个股机会"三层设计中的板块层。
从涨停池按行业聚合，判定每个活跃板块处于 冰点/启动/发酵/高潮/退潮 哪个阶段，
并计算主线分（涨停×持续×加速×高度），供盘中个股机会打分与盘后复盘使用。
"""
import statistics
from typing import List


class SectorCycleMachine:
    """板块阶段状态机（类似市场情绪周期，但作用于单个板块）"""

    @staticmethod
    def derive_phase(cur_zt: int, max_lbc: int, history_zt: List[int], prev_phase: str = "") -> str:
        """
        判定板块阶段。
        :param cur_zt:     当日板块涨停家数
        :param max_lbc:    当日板块内最高连板
        :param history_zt: 近N日(不含当日)该板块涨停家数序列
        :param prev_phase: 上一交易日阶段
        """
        if cur_zt <= 0:
            return "冰点"
        peak = max(history_zt) if history_zt else 0

        # 高潮：涨停达高位(≥5) 且 板块内高连板(≥4) 或 创近期峰值
        if cur_zt >= 5 and (max_lbc >= 4 or (peak and cur_zt >= peak)):
            return "高潮"
        # 退潮：曾高位(峰值≥4)，现在明显回落(≤峰值一半且<4)
        if peak >= 4 and cur_zt <= max(1, int(peak * 0.5)):
            return "退潮"
        # 发酵：涨停加速(≥3) 或 (涨停≥2 且有 3板龙头)——单只妖股(1家涨停)不算板块发酵
        if cur_zt >= 3 or (cur_zt >= 2 and max_lbc >= 3):
            return "发酵"
        # 高位滑落：从发酵/高潮掉到 1-2 家 → 退潮
        if prev_phase in ("发酵", "高潮") and cur_zt <= 2:
            return "退潮"
        # 其余：1-2 家涨停 → 启动（冰点后回暖）
        return "启动"

    @staticmethod
    def mainline_score(cur_zt: int, appear_days: int, accel: int, max_lbc: int,
                       lookback_days: int = 5) -> float:
        """
        主线分（0~1）：涨停家数 × 持续性 × 加速 × 高度，各维度归一化后加权。
        用于"哪些板块是本轮主线"的排序与阈值判定。
        """
        zt_norm = min(cur_zt / 8.0, 1.0)                      # 8 家涨停算满
        persist_norm = min(appear_days / lookback_days, 1.0)  # 近5日出现涨停的比例
        accel_norm = min(max(accel, 0) / 3.0, 1.0)            # 较昨日加速
        height_norm = min(max_lbc / 5.0, 1.0)                 # 板块内最高连板
        return round(0.3 * zt_norm + 0.3 * persist_norm + 0.2 * accel_norm + 0.2 * height_norm, 2)
