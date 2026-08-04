"""
盘前简报持久化管理（断链4修复）
================================
08:30 生成的盘前简报落库 pre_market_report，09:26 竞价从库读取，
保证进程在 08:30~09:26 之间重启后盘前上下文不丢失。
"""
import datetime
import logging

from database.models import PreMarketReport
from database.connection import db_manager

logger = logging.getLogger(__name__)


class PreMarketReportManager:
    """盘前简报持久化管理"""

    @staticmethod
    def save(trade_date: str, report: str):
        """保存当日盘前简报（按日期幂等 upsert，同日覆盖）"""
        if not report:
            return
        session = db_manager.get_session()
        try:
            row = session.query(PreMarketReport).filter(
                PreMarketReport.trade_date == trade_date).first()
            if row:
                row.report = report
                row.created_at = datetime.datetime.now()
            else:
                session.add(PreMarketReport(trade_date=trade_date, report=report))
            session.commit()
            logger.info(f"盘前简报已落库: {trade_date} ({len(report)} 字)")
        except Exception as e:
            session.rollback()
            logger.warning(f"盘前简报落库失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get(trade_date: str) -> str:
        """读取某日盘前简报，无则返回空串"""
        session = db_manager.get_session()
        try:
            row = session.query(PreMarketReport).filter(
                PreMarketReport.trade_date == trade_date).first()
            return row.report if row and row.report else ""
        except Exception as e:
            logger.warning(f"读取盘前简报失败: {e}")
            return ""
        finally:
            session.close()
