"""
看板数据聚合层
从各服务模块收集数据，统一返回给 API 和模板。
"""

import datetime
import logging
from typing import Dict, Any, List, Optional

from config.settings import settings
from database.services import (
    HoldingManager, MarketIndexManager, DailySnapshotManager,
    SectorStrengthManager, ZtPoolManager, db_manager,
)
from database.models import DailyZtPool

logger = logging.getLogger(__name__)


def build_dashboard_data() -> Dict[str, Any]:
    """
    收集看板所需的全部数据，返回统一结构。
    各模块可通过此函数获取最新状态，无需逐个调用。
    """

    # ---- 盘中实时状态（来自 market_monitor 全局缓存） ----
    from scheduler.monitor_core import (
        _current_market_style_global, _monitor_running, _last_monitor_cycle,
        _circuit_breaker_alerted, _index_breaker_alerted,
    )
    style = dict(_current_market_style_global)

    # ---- 持仓 + 盈亏 ----
    holdings = HoldingManager.get_active_holdings()
    pnl_report = HoldingManager.get_daily_pnl_report()
    ai_count = sum(1 for h in holdings if h.get("holding_type") == "AI_AUTO")

    # ---- 大盘指数 ----
    idx = MarketIndexManager.get_latest()

    # ---- 净值曲线 ----
    equity = DailySnapshotManager.get_equity_curve(days=20)
    equity_date = equity[-1]["date"] if equity else ""

    # ---- 热门板块 ----
    sectors = SectorStrengthManager.get_hot_sectors(top_n=8)
    sectors_date = sectors[0].get("_date", "") if sectors else ""

    # ---- 涨停龙头（回退逻辑：今日 → 最新历史） ----
    today_str = datetime.datetime.now().strftime("%Y%m%d")
    dragons, dragons_date = _get_dragons_with_fallback(today_str)

    # ---- 系统时间 ----
    now = datetime.datetime.now()
    from core.trade_calendar import is_trading_day as check_td
    is_td = check_td()

    # ---- 定时任务 ----
    from scheduler.daily_runner import _get_job_status
    jobs = _get_job_status()

    # ---- 组装 ----
    return {
        "timestamp": now.strftime("%H:%M:%S"),
        "trading_day": is_td,
        "market": {
            "style": style.get("style", "未知"),
            "strategy": style.get("priority_strategy", ""),
            "reason": style.get("reason", ""),
            "cycle_stage": style.get("cycle_stage", ""),
        },
        "index": _build_index_section(idx),
        "emotion": _build_emotion_section(style),
        "breadth": _build_breadth_section(style),
        "portfolio": _build_portfolio_section(holdings, pnl_report, ai_count),
        "sectors": sectors,
        "sectors_date": sectors_date or dragons_date,
        "dragons": dragons,
        "dragons_date": dragons_date,
        "equity_curve": equity[-20:] if equity else [],
        "equity_date": equity_date,
        "ai_status": {
            "monitor_running": _monitor_running,
            "last_cycle": _last_monitor_cycle,
            "circuit_breaker": _circuit_breaker_alerted,
            "index_breaker": _index_breaker_alerted,
            "max_positions": settings.MAX_AI_POSITIONS,
            "max_daily_buys": settings.MAX_DAILY_BUYS,
        },
        "jobs": jobs,
        "updated": style.get("style", "") != "",
    }


# ---------------------------------------------------------------------------
# 板块构建函数
# ---------------------------------------------------------------------------

def _build_index_section(idx: Optional[Dict]) -> Dict[str, Any]:
    if not idx:
        return {}
    return {
        "sh_close": idx.get("sh_close", 0),
        "sh_change_pct": idx.get("sh_change_pct", 0),
        "sz_change_pct": idx.get("sz_change_pct", 0),
        "gem_change_pct": idx.get("gem_change_pct", 0),
        "total_amount": f"{idx.get('total_amount', 0):.0f}亿" if idx.get("total_amount") else "",
    }


def _build_emotion_section(style: dict) -> Dict[str, Any]:
    return {
        "sentiment_index": style.get("sentiment_index", 0),
        "score_premium": style.get("score_premium", 0),
        "score_breadth": style.get("score_breadth", 0),
        "score_height": style.get("score_height", 0),
        "score_support": style.get("score_support", 0),
        "premium_intraday": f"{style.get('premium_intraday', 0)}%",
        "red_rate": f"{style.get('positive_ratio', 0)}%",
    }


def _build_breadth_section(style: dict) -> Dict[str, Any]:
    return {
        "up_count": style.get("up_count", 0),
        "down_count": style.get("down_count", 0),
        "zt_count": style.get("zt_count", 0),
        "dt_count": style.get("dt_count", 0),
        "zhaban_count": style.get("zhaban_count", 0),
        "zhaban_rate": f"{style.get('zhaban_rate', 0)}%",
        "height": style.get("height", 0),
        "zt_source": style.get("zt_source", ""),
    }


def _build_portfolio_section(
    holdings: list, pnl_report: dict, ai_count: int
) -> Dict[str, Any]:
    return {
        "active": len(holdings),
        "ai_auto": ai_count,
        "manual": len(holdings) - ai_count,
        "today_pnl": pnl_report.get("today_total_pnl", 0),
        "today_pnl_pct": pnl_report.get("today_total_pnl_pct", 0),
        "cumulative_pnl": pnl_report.get("cumulative_total_pnl", 0),
        "cumulative_pnl_pct": pnl_report.get("cumulative_total_pnl_pct", 0),
        "profit_count": pnl_report.get("profit_count", 0),
        "loss_count": pnl_report.get("loss_count", 0),
        "positions": [{
            "code": h["code"], "name": h["name"],
            "profit_pct": h["profit_pct"],
            "today_change": h.get("today_change_pct", 0),
            "cost_price": h["cost_price"],
            "current_price": h["current_price"],
            "strategy": h.get("strategy", ""),
            "type": h.get("type", ""),
            "buy_date": h.get("buy_date", ""),
        } for h in pnl_report.get("holdings", [])[:20]],
    }


def _get_dragons_with_fallback(today_str: str) -> tuple:
    """涨停龙头：优先今日，回退到最新历史"""
    dragons = ZtPoolManager.get_top_dragons(limit=10)
    dragons_date = dragons[0].get("_date", "") if dragons else ""

    if dragons:
        return dragons, dragons_date

    # 回退到最新有数据的交易日
    session = db_manager.get_session()
    try:
        latest = session.query(DailyZtPool.trade_date).order_by(
            DailyZtPool.trade_date.desc()
        ).first()
        if latest:
            records = session.query(DailyZtPool).filter(
                DailyZtPool.trade_date == latest[0]
            ).order_by(DailyZtPool.lbc.desc()).limit(10).all()
            return [{"code": r.code, "name": r.name, "lbc": r.lbc,
                     "industry": r.industry or "", "change_pct": r.change_pct,
                     "_date": latest[0]} for r in records], latest[0]
    except Exception:
        pass
    finally:
        session.close()
    return [], ""
