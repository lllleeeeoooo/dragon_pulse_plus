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
                    "sell_condition": r.sell_condition,
                    "auction_verdict": r.auction_verdict,
                    "auction_premise": r.auction_premise,
                    "auction_amount": r.auction_amount,
                }
                for r in recs
            ]
        finally:
            session.close()

    @staticmethod
    def update_auction_verdicts(verdicts: dict, trade_date: str = None):
        """将竞价 LLM 结论写入对应 PENDING 推荐标的的 auction_verdict / auction_premise 字段（09:26 调用）。
        trade_date 限定目标日期，避免多日 PENDING 累积时 verdict 写错到旧记录（断链7）。"""
        if not verdicts:
            return
        session = db_manager.get_session()
        try:
            for code, info in verdicts.items():
                query = session.query(Recommendation).filter(
                    Recommendation.code == code,
                    Recommendation.status == "PENDING",
                )
                if trade_date:
                    query = query.filter(Recommendation.trade_date == trade_date)
                rec = query.order_by(Recommendation.trade_date.desc()).first()
                if rec:
                    rec.auction_verdict = info.get("verdict", "观察")
                    if info.get("premise"):
                        rec.auction_premise = info["premise"]
            session.commit()
            logger.info(f"竞价结论落库 {len(verdicts)} 条")
        except Exception as e:
            session.rollback()
            logger.warning(f"竞价结论落库失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_recommendations_by_date(trade_date: str) -> List[Dict[str, Any]]:
        """按日期查询推荐标的（含 PENDING/TRIGGERED/EXPIRED），用于胜率复盘（不漏掉已买入的）"""
        session = db_manager.get_session()
        try:
            recs = session.query(Recommendation).filter(
                Recommendation.trade_date == trade_date
            ).all()
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
                    "sell_condition": r.sell_condition,
                    "status": r.status,
                    "auction_verdict": r.auction_verdict,
                    "auction_premise": r.auction_premise,
                    "auction_amount": r.auction_amount,
                }
                for r in recs
            ]
        finally:
            session.close()

    @staticmethod
    def save_auction_amounts(amounts: dict, trade_date: str = None):
        """保存 09:26 竞价成交额(元)到对应 PENDING 推荐（断链3：供盘中竞价量能校验）。"""
        if not amounts:
            return
        session = db_manager.get_session()
        try:
            for code, amount in amounts.items():
                query = session.query(Recommendation).filter(
                    Recommendation.code == code,
                    Recommendation.status == "PENDING",
                )
                if trade_date:
                    query = query.filter(Recommendation.trade_date == trade_date)
                rec = query.order_by(Recommendation.trade_date.desc()).first()
                if rec and amount:
                    rec.auction_amount = float(amount)
            session.commit()
            logger.info(f"竞价金额保存 {len(amounts)} 条")
        except Exception as e:
            session.rollback()
            logger.warning(f"竞价金额保存失败: {e}")
        finally:
            session.close()

    @staticmethod
    def expire_old_recommendations(before_date: str):
        """将指定日期之前的 PENDING 推荐标记为 EXPIRED。
        未评估(eval_note 为空)的先标记"已过期未评估"，让跳评窗口可见而非静默消失（断链8）。"""
        session = db_manager.get_session()
        try:
            from sqlalchemy import or_
            unevaluated = session.query(Recommendation).filter(
                Recommendation.status == "PENDING",
                Recommendation.trade_date < before_date,
                or_(Recommendation.eval_note.is_(None), Recommendation.eval_note == ""),
            ).update({"eval_note": "已过期未评估(盘后评估未执行)", "status": "EXPIRED"},
                     synchronize_session="fetch")
            updated = session.query(Recommendation).filter(
                Recommendation.status == "PENDING",
                Recommendation.trade_date < before_date
            ).update({"status": "EXPIRED"}, synchronize_session="fetch")
            total = (unevaluated or 0) + (updated or 0)
            if total:
                session.commit()
                logger.info(f"已过期 {total} 条旧推荐标的 (早于 {before_date})，"
                            f"其中 {unevaluated or 0} 条未评估已标记")
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