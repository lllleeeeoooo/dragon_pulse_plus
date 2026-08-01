import logging
from typing import List, Dict, Optional, Any
from database.models import DailySentiment
from database.connection import db_manager
logger = logging.getLogger(__name__)

class SentimentManager:
    """
    每日情绪历史向量数据库管理服务
    """

    @staticmethod
    def save_daily_sentiment(trade_date: str, sentiment_data: Dict[str, Any], cycle_stage: str = "", summary: str = "", total_amount: float = 0.0):
        """保存每日情绪分值与周期定性"""
        session = db_manager.get_session()
        try:
            # 存在则更新，不存在则插入
            record = session.query(DailySentiment).filter(DailySentiment.trade_date == trade_date).first()
            if not record:
                record = DailySentiment(trade_date=trade_date)
                session.add(record)

            record.height = sentiment_data.get("height", 0)
            record.breadth = sentiment_data.get("breadth", 0)
            record.zt_count = sentiment_data.get("zt_count", 0)
            record.dt_count = sentiment_data.get("dt_count", 0)
            record.zhaban_count = sentiment_data.get("zhaban_count", 0)
            record.yield_rate = sentiment_data.get("yield_rate", 0.0)
            record.seal_force_ratio = sentiment_data.get("seal_force_ratio", 0.0)
            record.zhaban_rate = sentiment_data.get("zhaban_rate", 0.0)
            record.sentiment_index = sentiment_data.get("sentiment_index", 0.0)
            record.cycle_stage = cycle_stage
            record.summary = summary
            record.total_amount = total_amount

            session.commit()
            logger.info(f"已保存 {trade_date} 每日情绪向量与周期结论")
        except Exception as e:
            session.rollback()
            logger.error(f"保存每日情绪数据失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_recent_sentiments(days_lookback: int = 5) -> List[Dict[str, Any]]:
        """查询最近N个交易日的情绪记录，按日期降序排列（最新的在前）"""
        import datetime
        session = db_manager.get_session()
        try:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days_lookback + 10)).strftime("%Y%m%d")
            records = session.query(DailySentiment).filter(
                DailySentiment.trade_date >= cutoff
            ).order_by(DailySentiment.trade_date.desc()).limit(days_lookback).all()
            return [
                {
                    "trade_date": r.trade_date,
                    "height": r.height,
                    "breadth": r.breadth,
                    "zt_count": r.zt_count,
                    "dt_count": r.dt_count,
                    "zhaban_count": r.zhaban_count,
                    "yield_rate": r.yield_rate,
                    "seal_force_ratio": r.seal_force_ratio,
                    "zhaban_rate": r.zhaban_rate,
                    "sentiment_index": r.sentiment_index,
                    "cycle_stage": r.cycle_stage or "",
                    "total_amount": r.total_amount,
                }
                for r in records
            ]
        finally:
            session.close()