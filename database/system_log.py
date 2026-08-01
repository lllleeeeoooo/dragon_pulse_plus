import logging
from typing import List, Dict, Optional, Any
from database.models import SystemLog
from database.connection import db_manager
logger = logging.getLogger(__name__)

class SystemLogManager:
    """系统运行日志管理服务"""

    @staticmethod
    def add_log(log_date: str, category: str, title: str, detail: str = ""):
        """新增一条系统运行日志"""
        session = db_manager.get_session()
        try:
            log = SystemLog(log_date=log_date, category=category, title=title, detail=detail)
            session.add(log)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"保存系统日志失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_logs(log_date: Optional[str] = None, category: Optional[str] = None,
                 limit: int = 50) -> List[Dict[str, Any]]:
        """查询系统运行日志"""
        session = db_manager.get_session()
        try:
            query = session.query(SystemLog).order_by(SystemLog.created_at.desc())
            if log_date:
                query = query.filter(SystemLog.log_date == log_date)
            if category:
                query = query.filter(SystemLog.category == category)
            logs = query.limit(limit).all()
            return [
                {"id": l.id, "log_date": l.log_date, "category": l.category,
                 "title": l.title, "detail": l.detail[:500] if l.detail else "",
                 "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""}
                for l in logs
            ]
        finally:
            session.close()