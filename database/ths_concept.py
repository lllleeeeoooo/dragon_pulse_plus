# -*- coding: utf-8 -*-
"""
同花顺概念板块指数趋势管理（独立补充维度）
============================================
每日从同花顺概念板块指数历史计算：收盘/单日涨幅/5日涨幅/量能比，落库 ths_concept_trend。
与 concept_cycle 的"涨停池成员聚合"口径独立，供概念阶段与主线强弱交叉验证。
"""
import datetime
import logging
from typing import Any, Dict, List, Optional

from database.models import ThsConceptTrend
from database.connection import db_manager

logger = logging.getLogger(__name__)

# 概念名规范化：去后缀/空格/标点，用于同花顺↔新浪概念名的最佳努力匹配
_SUFFIXES = ("概念", "板块", "产业链", "指数", "行业")


def _normalize_name(name: str) -> str:
    """概念名规范化：去后缀(概念/板块/产业链/指数)、去空格/横线。"""
    s = str(name).strip()
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.replace(" ", "").replace("-", "").replace("_", "")


class ThsConceptTrendManager:
    """同花顺概念指数趋势：每日快照 upsert + 查询"""

    @staticmethod
    def save_trends(trade_date: str, rows: List[Dict[str, Any]]) -> int:
        """批量 upsert 当日概念指数趋势（按 concept_code+trade_date 覆盖）。"""
        session = db_manager.get_session()
        saved = 0
        try:
            for r in rows:
                code = str(r.get("concept_code", "")).strip()
                if not code:
                    continue
                existing = session.query(ThsConceptTrend).filter(
                    ThsConceptTrend.concept_code == code,
                    ThsConceptTrend.trade_date == trade_date,
                ).first()
                vals = dict(
                    concept_name=str(r.get("concept_name", "")),
                    close=float(r.get("close", 0.0)),
                    chg_pct_1d=float(r.get("chg_pct_1d", 0.0)),
                    chg_pct_5d=float(r.get("chg_pct_5d", 0.0)),
                    volume_ratio_5d=float(r.get("volume_ratio_5d", 0.0)),
                )
                if existing:
                    for k, v in vals.items():
                        setattr(existing, k, v)
                    existing.updated_at = datetime.datetime.now()
                else:
                    session.add(ThsConceptTrend(
                        concept_code=code, trade_date=trade_date, **vals))
                saved += 1
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"同花顺概念趋势落库失败: {e}")
        finally:
            session.close()
        return saved

    @staticmethod
    def get_trend_map(trade_date: str) -> Dict[str, Dict[str, float]]:
        """当日概念指数趋势 {concept_name(原始): {chg_5d, vol_ratio, chg_1d, close}}"""
        session = db_manager.get_session()
        try:
            rows = session.query(ThsConceptTrend).filter(
                ThsConceptTrend.trade_date == trade_date).all()
            return {
                r.concept_name: {
                    "chg_5d": r.chg_pct_5d,
                    "vol_ratio": r.volume_ratio_5d,
                    "chg_1d": r.chg_pct_1d,
                    "close": r.close,
                }
                for r in rows
            }
        finally:
            session.close()

    @staticmethod
    def get_trend_map_normalized(trade_date: str) -> Dict[str, Dict[str, float]]:
        """当日概念指数趋势，键为**规范化概念名**（供 concept_cycle 按名匹配）。"""
        raw = ThsConceptTrendManager.get_trend_map(trade_date)
        out: Dict[str, Dict[str, float]] = {}
        for name, trend in raw.items():
            key = _normalize_name(name)
            if key:
                out[key] = trend
        return out

    @staticmethod
    def get_strong_concepts(top_n: int = 10, min_chg_5d: float = 5.0,
                            trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """同花顺强势概念榜：按 5 日涨幅降序（独立于涨停池的雷达）。"""
        session = db_manager.get_session()
        try:
            q = session.query(ThsConceptTrend)
            if trade_date:
                q = q.filter(ThsConceptTrend.trade_date == trade_date)
            else:
                latest = session.query(ThsConceptTrend.trade_date).order_by(
                    ThsConceptTrend.trade_date.desc()).first()
                if latest is None:
                    return []
                q = q.filter(ThsConceptTrend.trade_date == latest[0])
            rows = q.filter(ThsConceptTrend.chg_pct_5d >= min_chg_5d) \
                .order_by(ThsConceptTrend.chg_pct_5d.desc()).limit(top_n).all()
            return [
                {"concept": r.concept_name, "code": r.concept_code,
                 "chg_5d": r.chg_pct_5d, "chg_1d": r.chg_pct_1d,
                 "volume_ratio_5d": r.volume_ratio_5d}
                for r in rows
            ]
        finally:
            session.close()
