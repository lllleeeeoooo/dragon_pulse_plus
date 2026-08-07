# -*- coding: utf-8 -*-
"""
盘前数据完整性检查：每天早晨检查上一交易日盘后数据是否齐全。
复盘(18:01)或日线同步(15:30)失败/未跑时，早盘及时发现并 Bark 告警，避免带残缺数据开盘。
"""
import logging
from typing import Dict, List, Tuple

from database.connection import db_manager
from database.models import (
    DailySentiment, MarketIndex, DailyEquitySnapshot, DailyKline,
    DailyZtPool, SectorStrength, SectorCycle, ConceptCycle, Recommendation,
)
from core.trade_calendar import get_previous_trading_day
from notifier.bark import bark_notifier
from scheduler.helpers import _record_job_run

logger = logging.getLogger(__name__)

# 上一交易日盘后「必须有」的数据项（缺失 = 复盘/日线同步未完成，直接告警）
HARD_CHECKS: List[Tuple[str, type]] = [
    ("情绪向量", DailySentiment),
    ("大盘指数", MarketIndex),
    ("盈亏快照", DailyEquitySnapshot),
    ("日线", DailyKline),
]
# 「应有但允许缺失」的数据项（如当日无涨停/无推荐标的等极端情形，仅提示不告警）
SOFT_CHECKS: List[Tuple[str, type]] = [
    ("涨停池", DailyZtPool),
    ("板块强度", SectorStrength),
    ("板块周期", SectorCycle),
    ("概念周期", ConceptCycle),
    ("推荐标的", Recommendation),
]

_last_check_date: str = ""  # 本进程已检查过的交易日，避免 08:15 定时 + 启动调用重复告警


def _count_for_date(model, trade_date: str) -> int:
    """指定交易日该表记录数"""
    session = db_manager.get_session()
    try:
        return session.query(model).filter(model.trade_date == trade_date).count()
    finally:
        session.close()


def check_prev_day_data() -> Dict:
    """核对上一交易日盘后数据完整性。
    返回 {trade_date, counts, missing, soft_missing, complete}。"""
    prev = get_previous_trading_day()
    counts: Dict[str, int] = {}
    for name, model in HARD_CHECKS + SOFT_CHECKS:
        counts[name] = _count_for_date(model, prev)
    missing = [n for n, _ in HARD_CHECKS if counts[n] == 0]
    soft_missing = [n for n, _ in SOFT_CHECKS if counts[n] == 0]
    return {
        "trade_date": prev,
        "complete": not missing,
        "counts": counts,
        "missing": missing,
        "soft_missing": soft_missing,
    }


def job_data_check(force: bool = False):
    """08:15 盘前数据完整性检查（进程启动时也会后台调用一次）：
    检查**上一交易日**盘后数据是否齐全，**不管结果如何都 Bark 推送**（齐全推✅/缺失推⚠️）。"""
    global _last_check_date
    _record_job_run("job_data_check", "盘前数据检查")
    try:
        r = check_prev_day_data()
        # 同一交易日已检查过则不重复推送（避免定时 + 启动调用重复）
        if not force and r["trade_date"] == _last_check_date:
            return
        _last_check_date = r["trade_date"]
        counts = r["counts"]
        prev = r["trade_date"]

        log_line = " | ".join(f"{n}:{counts[n]}" for n, _ in HARD_CHECKS)
        if r["complete"]:
            msg = f"上一交易日 {prev} 盘后数据齐全 ✅\n" + log_line
            if r["soft_missing"]:
                msg += "\n可选缺失(可忽略): " + "、".join(r["soft_missing"])
            logger.info(msg)
            bark_notifier.send(
                title=f"✅ 上一交易日数据齐全 | {prev}",
                body=msg,
                group="数据检查",
                level="passive"
            )
            return

        # 硬项缺失 → 告警
        lines = [f"- {n}（{counts[n]} 条）" for n in r["missing"]]
        msg = f"上一交易日 {prev} 盘后数据缺失:\n" + "\n".join(lines)
        if r["soft_missing"]:
            msg += "\n可选缺失(可忽略): " + "、".join(r["soft_missing"])
        msg += "\n建议: 重新跑该日复盘 / 日线同步"
        logger.error(msg)
        bark_notifier.send(
            title=f"⚠️ 上一交易日数据不完整 | {prev}",
            body=msg,
            group="数据检查",
            level="timeSensitive"
        )
    except Exception as e:
        logger.error(f"盘前数据检查执行异常: {e}")
