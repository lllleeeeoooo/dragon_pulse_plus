"""
A股交易日历模块
交易日数据存储在 trade_calendar 表中，保留 ±30 天范围，每日自动维护。
"""
import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _ensure_synced():
    """确保交易日历已同步到数据库（每天首次调用时触发）"""
    try:
        from database.services import TradeCalendarManager
        TradeCalendarManager.sync_calendar()
    except Exception as e:
        logger.warning(f"交易日历同步检查失败: {e}")


def is_trading_day(date: Optional[datetime.date] = None) -> bool:
    """
    判断指定日期是否为 A 股交易日。
    查询 trade_calendar 表，覆盖范围外降级为周一至周五判断。
    """
    d = date or datetime.date.today()
    _ensure_synced()

    try:
        from database.services import TradeCalendarManager
        return TradeCalendarManager.is_trading_day(d.isoformat())
    except Exception:
        pass

    # 数据库不可用时降级
    return d.weekday() < 5


def is_last_non_trading_day(date: Optional[datetime.date] = None) -> bool:
    """今天不是交易日，但明天是 → 假期/周末最后一天"""
    d = date or datetime.date.today()
    tomorrow = d + datetime.timedelta(days=1)
    return (not is_trading_day(d)) and is_trading_day(tomorrow)
