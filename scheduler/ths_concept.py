# -*- coding: utf-8 -*-
"""同花顺概念指数趋势同步任务：15:35 后台拉 375 概念板块指数历史 → 落库，
供 18:01 复盘概念周期(sync_from_zt_pool)按名叠加指数趋势维度（5日涨幅/主线加分/阶段修正）。"""
import logging
import threading

from core.trade_calendar import is_trading_day
from scheduler.helpers import _record_job_run

logger = logging.getLogger(__name__)


def _sync_ths_concept_trends() -> None:
    """同花顺概念指数趋势同步线程体：逐只拉指数历史算 5 日涨幅/量能，进度经 job_run 落库。"""
    def _on_progress(pulled=None, total=None, done=False):
        _record_job_run("job_ths_concept_sync", "同花顺概念指数",
                        state="完成" if done else "运行中",
                        progress=f"{pulled or 0}/{total or 0}" if total else "")
    try:
        from data.ths_concept import fetch_ths_concept_trends
        result = fetch_ths_concept_trends(progress_cb=_on_progress)
        if isinstance(result, dict) and result.get("error"):
            logger.error(f"同花顺概念指数同步未执行: {result['error']}")
            _record_job_run("job_ths_concept_sync", "同花顺概念指数",
                            state="失败", progress=result["error"][:100])
        else:
            _record_job_run("job_ths_concept_sync", "同花顺概念指数", state="完成",
                            progress=f"{result.get('success', 0)}/{result.get('total', 0)}")
            logger.info(f"同花顺概念指数同步完成: {result}")
    except Exception as e:
        logger.error(f"同花顺概念指数同步异常: {e}")
        _record_job_run("job_ths_concept_sync", "同花顺概念指数", state="失败", progress=str(e)[:120])


def job_ths_concept_sync():
    """15:35 同花顺概念指数趋势独立任务：后台线程拉 375 概念板块指数历史（约 15-20 分钟），
    供 18:01 复盘概念周期叠加指数趋势维度。非交易日跳过。"""
    _record_job_run("job_ths_concept_sync", "同花顺概念指数", state="运行中")
    if not is_trading_day():
        logger.info("今日非交易日，跳过同花顺概念指数同步")
        _record_job_run("job_ths_concept_sync", "同花顺概念指数", state="跳过")
        return
    logger.info(">>> 触发 15:35 同花顺概念指数同步任务...")
    try:
        threading.Thread(target=_sync_ths_concept_trends, daemon=True,
                         name="ths-concept").start()
    except Exception as e:
        logger.error(f"同花顺概念指数同步启动失败: {e}")
        _record_job_run("job_ths_concept_sync", "同花顺概念指数", state="失败", progress=str(e)[:100])
