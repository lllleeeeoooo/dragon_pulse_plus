import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)


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
