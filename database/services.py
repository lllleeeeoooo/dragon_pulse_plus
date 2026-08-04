"""向后兼容层：所有 Manager 已迁移到 database/ 子模块，此处 re-export。"""
from database.connection import db_manager, switch_to_test_db, switch_to_prod_db
from database.holdings import HoldingManager
from database.recommendations import RecommendationManager
from database.sentiment import SentimentManager
from database.dragons import DragonManager
from database.logs import PushLogManager, LLMLogManager, ErrorLogManager, LogRetentionCleaner
from database.market_data import MarketIndexManager, DailySnapshotManager, ZtPoolManager, SectorStrengthManager
from database.calendar import TradeCalendarManager
from database.system_log import SystemLogManager
from database.seats import SeatProfileManager
from database.sector_cycle import SectorCycleManager
from database.concept_cycle import ConceptCycleManager
from database.pre_market_report import PreMarketReportManager
