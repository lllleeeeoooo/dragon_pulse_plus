import logging
from typing import List, Dict, Optional, Any
from database.models import Recommendation
from database.connection import db_manager
logger = logging.getLogger(__name__)

class RecommendationManager:
    """
    复盘/竞价推荐标的数据库管理服务
    """

    @staticmethod
    def add_recommendations(trade_date: str, items: List[Dict[str, Any]]):
        """保存盘后复盘推荐标的"""
        session = db_manager.get_session()
        try:
            for item in items:
                # 幂等：同日期同代码已存在则跳过，避免复盘任务重复执行插入重复 PENDING
                existing = session.query(Recommendation).filter(
                    Recommendation.trade_date == trade_date,
                    Recommendation.code == item.get("code"),
                ).first()
                if existing:
                    continue
                rec = Recommendation(
                    trade_date=trade_date,
                    code=item.get("code"),
                    name=item.get("name"),
                    strategy_type=item.get("strategy_type", "观察"),
                    open_requirement=item.get("open_requirement", ""),
                    auction_vol_ratio=item.get("auction_vol_ratio", ""),
                    buy_condition=item.get("buy_condition", ""),
                    sell_condition=item.get("sell_condition", ""),
                    status="PENDING"
                )
                session.add(rec)
            session.commit()
            logger.info(f"成功保存 {trade_date} 推荐标的 {len(items)} 个")
        except Exception as e:
            session.rollback()
            logger.error(f"保存推荐标的失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_pending_recommendations(trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取待观察的推荐标的列表"""
        session = db_manager.get_session()
        try:
            query = session.query(Recommendation).filter(Recommendation.status == "PENDING")
            if trade_date:
                query = query.filter(Recommendation.trade_date == trade_date)
            recs = query.all()
            return [
                {
                    "id": r.id,
                    "trade_date": r.trade_date,
                    "code": r.code,
                    "name": r.name,
                    "strategy_type": r.strategy_type,
                    "open_requirement": r.open_requirement,
                    "auction_vol_ratio": r.auction_vol_ratio,
                    "buy_condition": r.buy_condition,
                    "sell_condition": r.sell_condition
                }
                for r in recs
            ]
        finally:
            session.close()

    @staticmethod
    def expire_old_recommendations(before_date: str):
        """将指定日期之前的 PENDING 推荐标记为 EXPIRED"""
        session = db_manager.get_session()
        try:
            updated = session.query(Recommendation).filter(
                Recommendation.status == "PENDING",
                Recommendation.trade_date < before_date
            ).update({"status": "EXPIRED"}, synchronize_session="fetch")
            if updated:
                session.commit()
                logger.info(f"已过期 {updated} 条旧推荐标的 (早于 {before_date})")
        except Exception as e:
            session.rollback()
            logger.warning(f"过期旧推荐失败: {e}")
        finally:
            session.close()

    @staticmethod
    def mark_triggered(rec_id: int):
        """将推荐标的标记为 TRIGGERED（已买入），用于推荐胜率闭环统计"""
        session = db_manager.get_session()
        try:
            rec = session.query(Recommendation).filter(
                Recommendation.id == rec_id
            ).first()
            if rec:
                rec.status = "TRIGGERED"
                session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"标记推荐 TRIGGERED 失败: {e}")
        finally:
            session.close()