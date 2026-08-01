"""
数据抓取工具函数。
从 fetcher.py 提取，避免 fetcher.py ↔ mixin 之间的循环导入。
"""

import time
import functools
import logging
from typing import Callable, List, Tuple
import pandas as pd

logger = logging.getLogger(__name__)


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


SOURCE_PRIORITY = ["新浪", "腾讯", "东财"]


def multi_source_fetch(source_chain: List[Tuple[str, Callable[[], pd.DataFrame]]]) -> pd.DataFrame:
    """多数据源降级：依次尝试，返回第一个非空 DataFrame"""
    for source_name, fetch_func in source_chain:
        try:
            df = fetch_func()
            if df is not None and not df.empty:
                logger.debug(f"数据源 [{source_name}] 成功，获取 {len(df)} 条记录")
                return df
            logger.warning(f"数据源 [{source_name}] 返回空数据，尝试下一个...")
        except Exception as e:
            logger.warning(f"数据源 [{source_name}] 异常: {e}，尝试下一个...")
    logger.error("所有数据源均失败，返回空 DataFrame")
    return pd.DataFrame()
