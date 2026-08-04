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


def get_previous_trading_day(date: Optional[datetime.date] = None) -> str:
    """
    获取指定日期的前一个交易日 (YYYYMMDD格式)。
    最多向前回溯15天（覆盖国庆等长假）。
    """
    d = date or datetime.date.today()
    for i in range(1, 16):
        prev = d - datetime.timedelta(days=i)
        if is_trading_day(prev):
            return prev.strftime("%Y%m%d")
    # 兜底（日历数据异常/极端长假）：返回最近一个工作日，避免降级到周末
    prev = d - datetime.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= datetime.timedelta(days=1)
    return prev.strftime("%Y%m%d")
