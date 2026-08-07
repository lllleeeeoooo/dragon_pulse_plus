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


def get_n_trading_days_ago(n: int, date: Optional[datetime.date] = None) -> str:
    """返回 date 往前第 n 个交易日对应的日期 (YYYYMMDD)。
    供按交易日衰减的逻辑（如『龙头过期 30 个交易日』）——自然日含周末/节假日，
    活跃期按交易日算更贴合实际（≈42 自然日）。
    交易日取 trade_calendar 表（akshare 同步，历史覆盖 120 天）；表内查不到（表空/同步失败）
    时降级为工作日(周一~周五)估算，避免因表覆盖不足而死循环。"""
    _ensure_synced()
    earliest = _get_calendar_earliest()
    d = date or datetime.date.today()
    cnt = 0
    while cnt < n:
        d -= datetime.timedelta(days=1)
        if _is_td_best_effort(d, earliest):
            cnt += 1
    return d.strftime("%Y%m%d")


def _get_calendar_earliest() -> Optional[str]:
    """trade_calendar 表内最早日期 (YYYY-MM-DD)，用于判断日期是否在表覆盖范围内。"""
    try:
        from database.connection import db_manager
        from database.models import TradeCalendar
        session = db_manager.get_session()
        try:
            e = session.query(TradeCalendar).order_by(TradeCalendar.trade_date.asc()).first()
            return e.trade_date if e else None
        finally:
            session.close()
    except Exception:
        return None


def _is_td_best_effort(d: datetime.date, earliest: Optional[str]) -> bool:
    """覆盖范围内用 trade_calendar 真实交易日（含节假日/调休）；范围外/表空按工作日估算兜底。"""
    if earliest and d.isoformat() >= earliest:
        try:
            from database.services import TradeCalendarManager
            return TradeCalendarManager.is_trading_day(d.isoformat())
        except Exception:
            return d.weekday() < 5
    return d.weekday() < 5


def count_trading_days(start: datetime.date, end: datetime.date) -> int:
    """start(含)~end(含)之间的交易日数。
    日历覆盖范围内精确计数；超出覆盖范围或日历不可用时按工作日(周一~周五)估算。
    （供时间止损改交易日计算：避免自然日跨周末误触发。）"""
    _ensure_synced()
    try:
        from database.services import TradeCalendarManager
        n = TradeCalendarManager.count_trading_days(start.isoformat(), end.isoformat())
        if n is not None:
            return n
    except Exception:
        pass
    return sum(1 for i in range((end - start).days + 1)
               if (start + datetime.timedelta(days=i)).weekday() < 5)
