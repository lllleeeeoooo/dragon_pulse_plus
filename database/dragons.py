import logging
from typing import List, Dict, Optional, Any
from database.models import HistoricDragon
from database.connection import db_manager
logger = logging.getLogger(__name__)

class DragonManager:
    """
    历史龙头数据服务 (用于二波战法溯源)
    """

    @staticmethod
    def get_recent_dragons(days_lookback: int = 30) -> List[Dict[str, Any]]:
        """获取近 N 天内的人气总龙头"""
        import datetime
        session = db_manager.get_session()
        try:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days_lookback)).strftime("%Y%m%d")
            dragons = session.query(HistoricDragon).filter(
                HistoricDragon.is_active == True,
                HistoricDragon.peak_date >= cutoff
            ).all()
            return [
                {
                    "code": d.code,
                    "name": d.name,
                    "max_lbc": d.max_lbc,
                    "peak_date": d.peak_date,
                    "peak_price": d.peak_price,
                    "board_name": d.board_name
                }
                for d in dragons
            ]
        finally:
            session.close()