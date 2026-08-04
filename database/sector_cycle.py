"""
板块情绪周期管理服务
====================
每日从涨停池按行业聚合，计算每个活跃板块的"阶段"（冰点/启动/发酵/高潮/退潮）
与"主线分"，落库 sector_cycle 表。供盘后复盘、盘中个股机会打分使用。
"""
import datetime
import logging
from typing import Dict, List, Optional, Any

import pandas as pd

from database.models import SectorCycle, DailyZtPool
from database.connection import db_manager
from core.sector_cycle import SectorCycleMachine

logger = logging.getLogger(__name__)


class SectorCycleManager:
    """板块情绪周期管理服务"""

    @staticmethod
    def sync_from_zt_pool(trade_date: str, zt_df):
        """
        从当日涨停池按行业聚合，计算板块阶段与主线分，落库 sector_cycle。
        :param trade_date: 日期 YYYYMMDD
        :param zt_df:      当日涨停池 DataFrame（含 industry/lbc/code 列）
        """
        if zt_df is None or zt_df.empty or "industry" not in zt_df.columns:
            return
        session = db_manager.get_session()
        try:
            # 幂等：同日已算过则跳过
            if session.query(SectorCycle).filter(SectorCycle.trade_date == trade_date).first():
                return

            df = zt_df.copy()
            df["industry"] = df["industry"].astype(str).str.strip()
            df = df[df["industry"].notna() & (df["industry"] != "") & (df["industry"] != "nan")]
            if df.empty:
                return

            # 上一交易日各板块阶段/涨停数
            prev_records: Dict[str, SectorCycle] = {
                r.sector_name: r for r in session.query(SectorCycle).all()
            }
            # 近5日(不含当日)各行业涨停家数序列
            recent = SectorCycleManager._recent_zt_by_sector(session, trade_date, lookback=5)
            lookback = 5

            groups = df.groupby("industry")
            saved = 0
            for sector, group in groups:
                zt_count = len(group)
                max_lbc = 1
                if "lbc" in group.columns:
                    max_lbc = int(pd.to_numeric(group["lbc"], errors="coerce").fillna(1).max())
                history = recent.get(sector, [])
                prev = prev_records.get(sector)
                prev_phase = prev.phase if prev else ""
                prev_zt = prev.zt_count if prev else 0
                phase = SectorCycleMachine.derive_phase(zt_count, max_lbc, history, prev_phase)
                appear_days = len(history) + 1  # 含当日
                accel = zt_count - prev_zt
                score = SectorCycleMachine.mainline_score(zt_count, appear_days, accel, max_lbc, lookback)
                session.add(SectorCycle(
                    trade_date=trade_date,
                    sector_name=sector,
                    phase=phase,
                    zt_count=zt_count,
                    max_lbc=max_lbc,
                    prev_zt_count=prev_zt,
                    prev_phase=prev_phase,
                    is_mainline=score >= 0.5,
                    mainline_score=score,
                ))
                saved += 1
            session.commit()
            logger.info(f"板块周期已同步: {trade_date} {saved} 个活跃板块")
        except Exception as e:
            session.rollback()
            logger.warning(f"板块周期同步失败: {e}")
        finally:
            session.close()

    @staticmethod
    def _recent_zt_by_sector(session, trade_date: str, lookback: int = 5) -> Dict[str, List[int]]:
        """近 N 个交易日（不含当日）每个行业的涨停家数序列"""
        from sqlalchemy import func
        d0 = datetime.datetime.strptime(trade_date, "%Y%m%d").date()
        cutoff = (d0 - datetime.timedelta(days=int(lookback * 1.6))).strftime("%Y%m%d")
        rows = session.query(
            DailyZtPool.trade_date, DailyZtPool.industry, func.count(DailyZtPool.id)
        ).filter(
            DailyZtPool.trade_date < trade_date,
            DailyZtPool.trade_date >= cutoff,
            DailyZtPool.industry.isnot(None),
            DailyZtPool.industry != "",
        ).group_by(DailyZtPool.trade_date, DailyZtPool.industry).all()

        by_sector: Dict[str, Dict[str, int]] = {}
        for d, ind, cnt in rows:
            by_sector.setdefault(ind, {})[d] = cnt
        return {sector: [v for _, v in sorted(days.items())] for sector, days in by_sector.items()}

    @staticmethod
    def get_sector_cycle(trade_date: Optional[str] = None, top: int = 20) -> List[Dict[str, Any]]:
        """查询某日板块周期（默认最新），按主线分降序"""
        session = db_manager.get_session()
        try:
            query = session.query(SectorCycle)
            if trade_date:
                query = query.filter(SectorCycle.trade_date == trade_date)
            else:
                latest = session.query(SectorCycle.trade_date).order_by(
                    SectorCycle.trade_date.desc()
                ).first()
                if latest:
                    query = query.filter(SectorCycle.trade_date == latest[0])
                else:
                    return []
            records = query.order_by(SectorCycle.mainline_score.desc()).limit(top).all()
            return [
                {
                    "trade_date": r.trade_date,
                    "sector": r.sector_name,
                    "phase": r.phase,
                    "zt_count": r.zt_count,
                    "max_lbc": r.max_lbc,
                    "prev_zt_count": r.prev_zt_count,
                    "prev_phase": r.prev_phase,
                    "is_mainline": bool(r.is_mainline),
                    "mainline_score": r.mainline_score,
                }
                for r in records
            ]
        finally:
            session.close()
