"""
全市场数据抓取类。
方法分散在 3 个 mixin 模块中，工具函数在 data/core.py。
"""

# 工具函数从 core 导入并 re-export（向后兼容）
from data.core import retry_on_exception, multi_source_fetch, SOURCE_PRIORITY

# Mixin 组合
from data.fetcher_spot import _SpotMixin
from data.fetcher_pool import _PoolMixin
from data.fetcher_history import _HistoryMixin


class DataFetcher(_SpotMixin, _PoolMixin, _HistoryMixin):
    """
    全市场数据抓取类。方法分散在 3 个 mixin 模块中：
    - _SpotMixin:     实时行情、溢价、流动性基线
    - _PoolMixin:      涨停池、跌停池、炸板池、龙虎榜、板块成分、资金流向
    - _HistoryMixin:   历史K线、分时数据、VWAP、分时形态检测
    """
    pass
