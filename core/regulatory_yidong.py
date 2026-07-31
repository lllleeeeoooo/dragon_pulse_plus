import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)


class RegulatoryYidongCalculator:
    """
    交易所规则“异常波动”计算与风险预警评估器
    计算等级：
    - [正常]: 未触及异动红线，残余涨幅空间充足
    - [一级异动警示]: 3日偏离度已达 15%~20% (主板) 或 25%~30% (创业板)，今日若再涨停将触发官方《交易异常波动公告》
    - [二级严重异动警告]: 10日累计偏离度达到 80%~100% 或 10日内已触发 3次异动，随时触发《严重异常波动》红线/停牌核查风险！
    """

    @classmethod
    def evaluate_stock_yidong(
        cls,
        code: str,
        name: str,
        recent_3d_pct: float,          # 近 3 日累计涨跌幅 (%)
        index_3d_pct: float = 0.0,      # 近 3 日大盘/行业指数涨跌幅 (%)
        recent_10d_pct: float = 0.0,     # 近 10 日累计涨跌幅 (%)
        index_10d_pct: float = 0.0,     # 近 10 日大盘/行业指数涨跌幅 (%)
        yidong_count_10d: int = 0       # 近 10 日内已触发异动的次数
    ) -> Dict[str, Any]:
        """
        评估单只个股的交易所监管异动风险等级与“残余涨幅空间”
        """
        if not settings.REGULATORY_MONITOR_ENABLED:
            return {"level": "INFO", "warning_msg": "监管异动监控未开启", "remaining_space": 100.0}

        # 1. 确定板类型 (主板 10% / 创业板科创板 20% / 北交所 30%)
        code_str = str(code)
        is_gem = code_str.startswith(("300", "301"))
        is_star = code_str.startswith("688")
        is_bse = code_str.startswith(("82", "83", "87", "88", "92"))
        if is_bse:
            dev_3d_limit = 45.0  # 北交所 30% 涨跌幅，偏离度红线更高
        elif is_star:
            dev_3d_limit = settings.STAR_3D_DEV_LIMIT
        elif is_gem:
            dev_3d_limit = settings.GEM_3D_DEV_LIMIT
        else:
            dev_3d_limit = settings.MAIN_BOARD_3D_DEV_LIMIT

        # 2. 计算 3 日涨跌幅偏离度
        dev_3d = round(recent_3d_pct - index_3d_pct, 2)

        # 3. 计算 10 日严重异动偏离度
        dev_10d = round(recent_10d_pct - index_10d_pct, 2)

        # 计算残余 3 日异动空间 (%)
        remaining_3d_space = round(dev_3d_limit - dev_3d, 2)
        # 计算残余 10 日严重异动空间 (%)
        remaining_10d_space = round(settings.REGULATORY_10D_LIMIT - dev_10d, 2)

        level = "NORMAL"
        warning_tags = []
        warning_msg = ""

        # ---------- 等级 2：二级严重异常波动预警 (红线级) ----------
        if dev_10d >= (settings.REGULATORY_10D_LIMIT - 15.0) or yidong_count_10d >= (settings.MAX_YIDONG_COUNT_10D - 1):
            level = "CRITICAL_SERIOUS"
            warning_tags.append("🚨触及严重异动红线")
            warning_msg = (
                f"【严重异动风险极高】该股10日偏离度已达 {dev_10d}% (红线:{settings.REGULATORY_10D_LIMIT}%)，"
                f"且10日内已异动 {yidong_count_10d} 次！再涨停极易触发停牌核查/重点监控，游资随时砸盘规避，建议【严格避开/逢高落袋】！"
            )

        # ---------- 等级 1：一级交易异常波动预警 (触线级) ----------
        elif dev_3d >= (dev_3d_limit - 6.0) or remaining_3d_space <= 6.0:
            level = "WARNING_YIDONG"
            warning_tags.append("⚠️即将触发异动公告")
            warning_msg = (
                f"【异动警示】该股 3 日偏离度已达 {dev_3d}% (红线:{dev_3d_limit}%)，残余空间仅剩 {remaining_3d_space}%。"
                f"今日若封涨停必将触发官方《交易异常波动公告》，谨防主力‘控异动’炸板砸盘。"
            )

        else:
            warning_msg = f"异动风险可控，3日偏离度 {dev_3d}% (残余空间 {remaining_3d_space}%)。"

        return {
            "code": code,
            "name": name,
            "level": level,
            "dev_3d": dev_3d,
            "dev_10d": dev_10d,
            "remaining_3d_space": remaining_3d_space,
            "remaining_10d_space": remaining_10d_space,
            "warning_tags": warning_tags,
            "warning_msg": warning_msg
        }
