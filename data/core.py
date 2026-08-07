"""
数据抓取工具函数。
从 fetcher.py 提取，避免 fetcher.py ↔ mixin 之间的循环导入。
"""

import json
import os
import time
import socket
import functools
import datetime
import logging
from typing import Callable, List, Tuple, Dict
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)

# 源熔断状态落盘文件（logs/.source_circuit.json）：看门狗自动重启会拉起新进程，
# 内存熔断态清零 → 新进程重打被限流源 → 再熔断 → 再重启 的恶性循环。
# 落盘后，当天内重启的新进程恢复"已熔断源"，直接走备用源，不再重打。
_CIRCUIT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", ".source_circuit.json")

# 数据源抓取 socket 超时（秒）：akshare 内部 requests 未传 timeout，
# 数据源挂起(服务器不响应也不断连)时会永久阻塞调用线程——盘中 15s 轮询的主循环
# 曾因此停摆 48 分钟。抓取期间设置 socket 默认超时，超时按该源失败降级/重试。
_FETCH_SOCKET_TIMEOUT = 12


def socket_timeout(timeout: float = _FETCH_SOCKET_TIMEOUT):
    """给数据源抓取函数加 socket 超时：akshare 内部 requests 未传 timeout，
    源挂起时防止永久阻塞主循环——卡死变为 socket.timeout 异常，主循环 try/except
    可 continue 自行恢复（不再停摆）。抓取期间设置 socket 默认超时，结束后恢复。"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _old = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            try:
                return func(*args, **kwargs)
            finally:
                socket.setdefaulttimeout(_old)
        return wrapper
    return decorator

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


def _persist_circuit_state():
    """把当日已熔断源落盘，供看门狗重启后的新进程恢复（避免重启后重打被限流源）"""
    try:
        with open(_CIRCUIT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": _source_fail_date, "blocked": sorted(_source_circuit_open)},
                      f, ensure_ascii=False)
    except Exception:
        pass


def _restore_circuit_state():
    """新进程启动时恢复当日熔断状态：落盘记录属于今天则不再重打该源（跨重启防循环）"""
    global _source_fail_date, _source_circuit_open
    try:
        if os.path.exists(_CIRCUIT_STATE_FILE):
            with open(_CIRCUIT_STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == _today_str():
                _source_circuit_open.update({s: True for s in data.get("blocked", [])})
                _source_fail_date = _today_str()
    except Exception:
        pass


def _reset_if_new_day():
    """跨天时清零熔断状态"""
    global _source_fail_date
    today = _today_str()
    if _source_fail_date != today:
        _source_fail_date = today
        _source_fail_counts.clear()
        _source_circuit_open.clear()
        _persist_circuit_state()  # 新一天清空落盘熔断（源可能已恢复）


def source_blocked(source_name: str) -> bool:
    """该源今日是否已被熔断（当日异常达阈值后当天不再调用）"""
    _reset_if_new_day()
    return _source_circuit_open.get(source_name, False)


def record_source_failure(source_name: str):
    """记录一次源调用异常；当日累计达阈值则熔断该源（次日重置），并落盘供重启恢复"""
    _reset_if_new_day()
    _source_fail_counts[source_name] = _source_fail_counts.get(source_name, 0) + 1
    if _source_fail_counts[source_name] >= settings.SOURCE_FAIL_CIRCUIT_LIMIT:
        if not _source_circuit_open.get(source_name, False):
            _source_circuit_open[source_name] = True
            _persist_circuit_state()  # 落盘：看门狗重启后新进程当天不再重打该源
            logger.warning(
                f"数据源 [{source_name}] 当日异常已达 {settings.SOURCE_FAIL_CIRCUIT_LIMIT} 次，"
                f"今日剩余时间熔断该源（次日自动重置，状态已落盘 {os.path.basename(_CIRCUIT_STATE_FILE)}）"
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


# 模块加载时恢复当日已熔断源（看门狗重启后的新进程不重打被限流源）
_restore_circuit_state()


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


def multi_source_fetch(source_chain: List[Tuple[str, Callable[[], pd.DataFrame]]],
                       timeout: float = _FETCH_SOCKET_TIMEOUT) -> pd.DataFrame:
    """多数据源降级：依次尝试，返回第一个非空 DataFrame。
    当日已熔断的源（异常达阈值）直接跳过，不浪费调用。
    每个源调用包裹 socket 超时：akshare 内部 requests 未传 timeout，数据源挂起时
    主循环会永久阻塞（曾导致盘中监控停摆 48 分钟）——超时按该源失败降级/记录。"""
    for source_name, fetch_func in source_chain:
        if source_blocked(source_name):
            logger.debug(f"数据源 [{source_name}] 今日已熔断，跳过")
            continue
        try:
            _old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            try:
                df = fetch_func()
            finally:
                socket.setdefaulttimeout(_old_timeout)
            if df is not None and not df.empty:
                logger.debug(f"数据源 [{source_name}] 成功，获取 {len(df)} 条记录")
                return df
            logger.warning(f"数据源 [{source_name}] 返回空数据，尝试下一个...")
        except Exception as e:
            logger.warning(f"数据源 [{source_name}] 异常: {e}，尝试下一个...")
            record_source_failure(source_name)
    logger.error("所有数据源均失败，返回空 DataFrame")
    return pd.DataFrame()
