import time
import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from core.strategies import StrategyAnalyzer, MarketStyle
from core.holding_monitor import HoldingMonitor
from core.trade_calendar import is_trading_day
from llm.sell_advisor import DynamicSellAdvisor
from notifier.bark import bark_notifier
from database.services import HoldingManager, RecommendationManager

logger = logging.getLogger(__name__)

# 模块级全局变量，供 API 端点实时查询
_current_market_style_global: Dict[str, str] = {}
_monitor_running: bool = False
_last_monitor_cycle: str = ""
_circuit_breaker_alerted: bool = False
_index_breaker_alerted: bool = False

from scheduler.monitor_core import _MonitorCoreMixin
from scheduler.monitor_signals import _MonitorSignalsMixin
from scheduler.monitor_style import _MonitorStyleMixin
from scheduler.monitor_auction import _MonitorAuctionMixin


class MarketMonitor(
    _MonitorCoreMixin,
    _MonitorSignalsMixin,
    _MonitorStyleMixin,
    _MonitorAuctionMixin,
):
    """盘中 15 秒实时轮询监控引擎。"""
    from data.fetcher import DataFetcher as _DF
    pass
