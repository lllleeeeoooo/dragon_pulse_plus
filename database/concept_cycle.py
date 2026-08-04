"""
概念情绪周期管理服务（切片3：概念主线识别）
============================================
数据底座：概念成分股映射（concept_member，新浪源）→ 按概念聚合涨停池 → 仅题材型概念
（core.concept_filter 过滤事件/属性标签）→ 复用 SectorCycleMachine 判定
冰点/启动/发酵/高潮/退潮 与主线分 → 落库 concept_cycle 表。
"""
import datetime
import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from database.models import ConceptMember, ConceptCycle
from database.connection import db_manager
from core.sector_cycle import SectorCycleMachine
from core.concept_filter import is_thematic
from config.settings import settings

logger = logging.getLogger(__name__)


class ConceptCycleManager:
    """概念情绪周期管理服务"""

    # ---------------------------------------------------------------
    # 1) 概念成分股映射刷新（数据底座）
    # ---------------------------------------------------------------
    @staticmethod
    def last_refresh_date() -> Optional[str]:
        """最近一次成分股快照日期 YYYYMMDD"""
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            return session.query(func.max(ConceptMember.refresh_date)).scalar()
        finally:
            session.close()

    @staticmethod
    def _refresh_due(trade_date: str) -> bool:
        """是否到期需刷新（距上次快照超过间隔天数）"""
        last = ConceptCycleManager.last_refresh_date()
        if not last:
            return True
        d0 = datetime.datetime.strptime(trade_date, "%Y%m%d").date()
        d1 = datetime.datetime.strptime(last, "%Y%m%d").date()
        return (d0 - d1).days >= settings.CONCEPT_MEMBER_REFRESH_INTERVAL_DAYS

    @staticmethod
    def refresh_membership(trade_date: Optional[str] = None, force: bool = False) -> bool:
        """
        刷新 概念→成分股 映射（新浪源，约175个概念逐次拉取）。
        逐概念 upsert：拉取成功才删旧插新，失败的概念保留旧快照，不整体清库。
        返回是否完成一次成功刷新（成功比例≥80% 视为完成）。
        """
        today = trade_date or datetime.date.today().strftime("%Y%m%d")
        session = db_manager.get_session()
        try:
            if not force and not ConceptCycleManager._refresh_due(today):
                logger.info(f"概念成分股快照({today})未到期，复用旧快照")
                return True
            # 惰性导入避免 data↔database 循环导入
            from data.fetcher_pool import _PoolMixin as Pool
            boards = Pool.get_concept_boards()
            if boards is None or boards.empty:
                logger.warning("概念列表为空，刷新失败")
                return False
            gn_list = [(str(c), str(n)) for c, n in
                       zip(boards["code"], boards["name"]) if str(c).startswith("gn_")]
            if not gn_list:
                logger.warning("无新浪 gn_ 概念代码，刷新失败")
                return False

            ok = 0
            for code, name in gn_list:
                try:
                    cons = Pool.get_concept_cons(code)
                    if cons is None or cons.empty:
                        continue
                    # upsert：删除该概念旧快照，写入新快照
                    session.query(ConceptMember).filter(
                        ConceptMember.concept_code == code).delete()
                    for c in cons["code"]:
                        session.add(ConceptMember(
                            concept_code=code, concept_name=name,
                            stock_code=str(c).zfill(6), refresh_date=today))
                    ok += 1
                except Exception as e:
                    logger.warning(f"概念[{name}]成分股刷新失败: {e}")
                time.sleep(0.15)  # 礼貌性间隔，避免被新浪限流
            session.commit()
            total = session.query(ConceptMember).filter(
                ConceptMember.refresh_date == today).count()
            logger.info(f"概念成分股刷新: {ok}/{len(gn_list)} 个概念, 快照 {total} 条")
            return ok >= max(1, int(len(gn_list) * 0.8))
        except Exception as e:
            session.rollback()
            logger.warning(f"概念成分股刷新异常: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def _membership_map(session) -> Dict[str, List[str]]:
        """{股票代码: [概念名...]} 当前全部快照并集（每概念只保留最新快照，故并集即现状）"""
        rows = session.query(ConceptMember.stock_code, ConceptMember.concept_name).all()
        m: Dict[str, List[str]] = {}
        for code, name in rows:
            m.setdefault(str(code).zfill(6), []).append(name)
        return m

    # ---------------------------------------------------------------
    # 2) 概念情绪周期同步
    # ---------------------------------------------------------------
    @staticmethod
    def sync_from_zt_pool(trade_date: str, zt_df):
        """
        从当日涨停池按概念聚合（仅题材型概念，非题材标签过滤），
        计算概念阶段与主线分，落库 concept_cycle。
        :param trade_date: 日期 YYYYMMDD
        :param zt_df:      当日涨停池 DataFrame（含 code/lbc 列）
        """
        if zt_df is None or zt_df.empty or "code" not in zt_df.columns:
            return
        session = db_manager.get_session()
        try:
            # 幂等：同日已算过则跳过
            if session.query(ConceptCycle).filter(
                    ConceptCycle.trade_date == trade_date).first():
                return

            # 成分股快照到期则刷新（失败不阻断，沿用旧快照）
            if ConceptCycleManager._refresh_due(trade_date):
                ConceptCycleManager.refresh_membership(trade_date)
            membership = ConceptCycleManager._membership_map(session)
            if not membership:
                logger.warning("无概念成分股映射，跳过概念周期同步")
                return

            df = zt_df.copy()
            df["code"] = df["code"].astype(str).str.zfill(6)
            df = df[df["code"].isin(membership.keys())]
            if df.empty:
                logger.info(f"{trade_date} 涨停池无概念命中，无概念周期数据")
                return

            # 展开 股票→概念 长表
            expanded: List[Dict[str, Any]] = []
            for _, row in df.iterrows():
                for name in membership.get(row["code"], []):
                    expanded.append({
                        "concept": name,
                        "code": row["code"],
                        "lbc": row.get("lbc", 1),
                    })
            edf = pd.DataFrame(expanded)
            # 仅题材型概念（过滤 事件/属性 标签）
            edf = edf[edf["concept"].map(is_thematic)]
            if edf.empty:
                logger.info(f"{trade_date} 概念全部为非题材标签，无概念周期数据")
                return
            edf["lbc"] = pd.to_numeric(edf["lbc"], errors="coerce").fillna(1).astype(int)

            # 上一交易日各概念阶段/涨停数
            prev_records: Dict[str, ConceptCycle] = {
                r.concept_name: r for r in session.query(ConceptCycle).all()
            }
            # 近5日(不含当日)各概念涨停家数序列
            recent = ConceptCycleManager._recent_zt_by_concept(session, trade_date, lookback=5)
            lookback = 5

            saved = 0
            for concept, group in edf.groupby("concept"):
                zt_count = len(group)
                max_lbc = int(group["lbc"].max())
                history = recent.get(concept, [])
                prev = prev_records.get(concept)
                prev_phase = prev.phase if prev else ""
                prev_zt = prev.zt_count if prev else 0
                phase = SectorCycleMachine.derive_phase(zt_count, max_lbc, history, prev_phase)
                appear_days = len(history) + 1  # 含当日
                accel = zt_count - prev_zt
                score = SectorCycleMachine.mainline_score(
                    zt_count, appear_days, accel, max_lbc, lookback)
                session.add(ConceptCycle(
                    trade_date=trade_date,
                    concept_name=concept,
                    phase=phase,
                    zt_count=zt_count,
                    max_lbc=max_lbc,
                    prev_zt_count=prev_zt,
                    prev_phase=prev_phase,
                    is_mainline=score >= settings.CONCEPT_MAINLINE_SCORE_THRESHOLD,
                    mainline_score=score,
                ))
                saved += 1
            session.commit()
            logger.info(f"概念周期已同步: {trade_date} {saved} 个活跃题材概念")
        except Exception as e:
            session.rollback()
            logger.warning(f"概念周期同步失败: {e}")
        finally:
            session.close()

    @staticmethod
    def _recent_zt_by_concept(session, trade_date: str, lookback: int = 5) -> Dict[str, List[int]]:
        """近 N 个交易日（不含当日）每个概念的涨停家数序列（来自 concept_cycle 历史）"""
        d0 = datetime.datetime.strptime(trade_date, "%Y%m%d").date()
        cutoff = (d0 - datetime.timedelta(days=int(lookback * 1.6))).strftime("%Y%m%d")
        rows = session.query(
            ConceptCycle.trade_date, ConceptCycle.concept_name, ConceptCycle.zt_count
        ).filter(
            ConceptCycle.trade_date < trade_date,
            ConceptCycle.trade_date >= cutoff,
        ).all()

        by_concept: Dict[str, Dict[str, int]] = {}
        for d, name, cnt in rows:
            by_concept.setdefault(name, {})[d] = cnt
        return {name: [v for _, v in sorted(days.items())] for name, days in by_concept.items()}

    # ---------------------------------------------------------------
    # 3) 查询
    # ---------------------------------------------------------------
    @staticmethod
    def get_concept_cycle(trade_date: Optional[str] = None, top: int = 20) -> List[Dict[str, Any]]:
        """查询某日概念周期（默认最新），按主线分降序"""
        session = db_manager.get_session()
        try:
            query = session.query(ConceptCycle)
            if trade_date:
                query = query.filter(ConceptCycle.trade_date == trade_date)
            else:
                latest = session.query(ConceptCycle.trade_date).order_by(
                    ConceptCycle.trade_date.desc()
                ).first()
                if latest:
                    query = query.filter(ConceptCycle.trade_date == latest[0])
                else:
                    return []
            records = query.order_by(ConceptCycle.mainline_score.desc()).limit(top).all()
            return [
                {
                    "trade_date": r.trade_date,
                    "concept": r.concept_name,
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
