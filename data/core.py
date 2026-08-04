"""
数据抓取工具函数。
从 fetcher.py 提取，避免 fetcher.py ↔ mixin 之间的循环导入。
"""

import time
import functools
import datetime
import logging
from typing import Callable, List, Tuple, Dict
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)

# ==============================================================================
# 数据源当日熔断（次日重置）
# 某数据源当日异常达 SOURCE_FAIL_CIRCUIT_LIMIT 次后，当天剩余时间不再调用该源，
# 避免"东财每15秒连接失败空等"这类持续异常拖慢轮询。跨天自动清零。
# ==============================================================================
_source_fail_counts: Dict[str, int] = {}
_source_circuit_open: Dict[str, bool] = {}
_source_fail_date: str = ""


def _today_str() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _reset_if_new_day():
    """跨天时清零熔断状态"""
    global _source_fail_date
    today = _today_str()
    if _source_fail_date != today:
        _source_fail_date = today
        _source_fail_counts.clear()
        _source_circuit_open.clear()


def source_blocked(source_name: str) -> bool:
    """该源今日是否已被熔断（当日异常达阈值后当天不再调用）"""
    _reset_if_new_day()
    return _source_circuit_open.get(source_name, False)


def record_source_failure(source_name: str):
    """记录一次源调用异常；当日累计达阈值则熔断该源（次日重置）"""
    _reset_if_new_day()
    _source_fail_counts[source_name] = _source_fail_counts.get(source_name, 0) + 1
    if _source_fail_counts[source_name] >= settings.SOURCE_FAIL_CIRCUIT_LIMIT:
        if not _source_circuit_open.get(source_name, False):
            _source_circuit_open[source_name] = True
            logger.warning(
                f"数据源 [{source_name}] 当日异常已达 {settings.SOURCE_FAIL_CIRCUIT_LIMIT} 次，"
                f"今日剩余时间熔断该源（次日自动重置）"
            )


def source_circuit_status() -> Dict[str, dict]:
    """查看当前各数据源熔断状态（供调试/看板）"""
    _reset_if_new_day()
    return {
        name: {
            "fail_count": _source_fail_counts.get(name, 0),
            "blocked": _source_circuit_open.get(name, False),
        }
        for name in SOURCE_PRIORITY
    }


def retry_on_exception(retries: int = 3, delay: float = 2.0, backoff: float = 1.5):
    """重试装饰器：网络波动时自动重试，指数退避"""
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _delay = delay
            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt < retries:
                        logger.warning(
                            f"{func.__name__} 第{attempt+1}次尝试失败: {e}，"
                            f"{_delay:.1f}秒后重试..."
                        )
                        time.sleep(_delay)
                        _delay *= backoff
                    else:
                        logger.error(f"{func.__name__} 重试{retries}次后仍失败: {e}")
                        raise
            return None
        return wrapper
    return decorator


SOURCE_PRIORITY = ["东财", "腾讯", "新浪"]


def multi_source_fetch(source_chain: List[Tuple[str, Callable[[], pd.DataFrame]]]) -> pd.DataFrame:
    """多数据源降级：依次尝试，返回第一个非空 DataFrame。
    当日已熔断的源（异常达阈值）直接跳过，不浪费调用。"""
    for source_name, fetch_func in source_chain:
        if source_blocked(source_name):
            logger.debug(f"数据源 [{source_name}] 今日已熔断，跳过")
            continue
        try:
            df = fetch_func()
            if df is not None and not df.empty:
                logger.debug(f"数据源 [{source_name}] 成功，获取 {len(df)} 条记录")
                return df
            logger.warning(f"数据源 [{source_name}] 返回空数据，尝试下一个...")
        except Exception as e:
            logger.warning(f"数据源 [{source_name}] 异常: {e}，尝试下一个...")
            record_source_failure(source_name)
    logger.error("所有数据源均失败，返回空 DataFrame")
    return pd.DataFrame()
