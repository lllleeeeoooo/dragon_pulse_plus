"""
数据库访问层
按领域拆分为独立模块，通过 __init__.py 统一导出以保持向后兼容。
外部代码可以从 database 直接导入，无需关心内部模块结构。

使用方式:
    from database import HoldingManager, db_manager
    from database import MarketIndexManager, ZtPoolManager
"""

from database.connection import db_manager, switch_to_test_db, switch_to_prod_db
from database.holdings import HoldingManager
from database.recommendations import RecommendationManager
from database.sentiment import SentimentManager
from database.dragons import DragonManager
from database.logs import PushLogManager, LLMLogManager, ErrorLogManager, LogRetentionCleaner
from database.market_data import MarketIndexManager, DailySnapshotManager, ZtPoolManager, SectorStrengthManager
from database.calendar import TradeCalendarManager
from database.system_log import SystemLogManager
from database.investigation import InvestigationManager
from database.seats import SeatProfileManager
from database.sector_cycle import SectorCycleManager
from database.concept_cycle import ConceptCycleManager
from database.pre_market_report import PreMarketReportManager

__all__ = [
    "db_manager",
    "switch_to_test_db",
    "switch_to_prod_db",
    "HoldingManager",
    "RecommendationManager",
    "SentimentManager",
    "DragonManager",
    "PushLogManager",
    "LLMLogManager",
    "ErrorLogManager",
    "LogRetentionCleaner",
    "MarketIndexManager",
    "DailySnapshotManager",
    "ZtPoolManager",
    "SectorStrengthManager",
    "TradeCalendarManager",
    "SystemLogManager",
    "InvestigationManager",
    "SeatProfileManager",
    "SectorCycleManager",
    "ConceptCycleManager",
    "PreMarketReportManager",
]
