# -*- coding: utf-8 -*-
"""
同花顺概念板块指数趋势拉取
============================
375 个同花顺概念逐只拉板块指数历史（stock_board_concept_index_ths），
算 单日涨幅/5日涨幅/量能比 后落库 ths_concept_trend。
零依赖现成：akshare 接口，逐只 try/except + 礼貌性间隔防限流（仿 concept_cycle.refresh_membership）。
"""
import datetime
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from config.settings import settings
from database.ths_concept import ThsConceptTrendManager

logger = logging.getLogger(__name__)


def _compute_trends(concept_code: str, concept_name: str,
                    hist_df: Optional[pd.DataFrame]) -> Optional[Dict[str, Any]]:
    """从同花顺概念指数历史 DataFrame 计算趋势。
    列：日期/今开价/最高价/最低价/昨收价/成交量/成交额 —— 其中「昨收价」实为当日收盘价。"""
    if hist_df is None or len(hist_df) < 6:
        return None
    close_col = "昨收价" if "昨收价" in hist_df.columns else hist_df.columns[3]
    closes = pd.to_numeric(hist_df[close_col], errors="coerce").dropna().tolist()
    if len(closes) < 6:
        return None
    close_today, close_prev, close_5ago = closes[-1], closes[-2], closes[-6]
    chg_1d = (close_today - close_prev) / close_prev * 100 if close_prev else 0.0
    chg_5d = (close_today - close_5ago) / close_5ago * 100 if close_5ago else 0.0
    vol_ratio = 1.0
    if "成交量" in hist_df.columns:
        vols = pd.to_numeric(hist_df["成交量"], errors="coerce").dropna().tail(5).tolist()
        if vols and sum(vols) > 0:
            vol_ratio = vols[-1] / (sum(vols) / len(vols))
    return {
        "concept_code": str(concept_code),
        "concept_name": str(concept_name),
        "close": round(close_today, 2),
        "chg_pct_1d": round(chg_1d, 2),
        "chg_pct_5d": round(chg_5d, 2),
        "volume_ratio_5d": round(vol_ratio, 2),
    }


def fetch_ths_concept_trends(trade_date: Optional[str] = None,
                             progress_cb: Optional[Callable] = None,
                             max_concepts: Optional[int] = None) -> dict:
    """拉同花顺概念板块指数历史 → 算趋势 → 落库。
    :param trade_date: YYYYMMDD，默认今天
    :param progress_cb: 每 25 只回调 progress_cb(pulled, total)
    :param max_concepts: 限拉数量（冒烟/测试用），默认全量 375
    返回 {success, failed, total, saved}。"""
    import akshare as ak
    trade_date = trade_date or datetime.date.today().strftime("%Y%m%d")
    try:
        boards = ak.stock_board_concept_name_ths()
    except Exception as e:
        logger.warning(f"同花顺概念名单拉取失败: {e}")
        return {"success": 0, "failed": 0, "total": 0, "saved": 0, "error": str(e)[:120]}
    if boards is None or boards.empty:
        logger.warning("同花顺概念名单为空，跳过")
        return {"success": 0, "failed": 0, "total": 0, "saved": 0, "error": "概念名单为空"}

    names = list(zip(boards["code"].astype(str), boards["name"].astype(str)))
    if max_concepts:
        names = names[:max_concepts]

    rows: List[Dict[str, Any]] = []
    failed = 0
    for i, (code, name) in enumerate(names):
        try:
            hist = ak.stock_board_concept_index_ths(symbol=name)
            trend = _compute_trends(code, name, hist)
            if trend:
                rows.append(trend)
        except Exception as e:
            failed += 1
            if failed <= 3 or (i + 1) % 50 == 0:
                logger.warning(f"同花顺概念[{name}]指数拉取失败: {e}")
        time.sleep(settings.THS_FETCH_PACING_SECONDS)
        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(pulled=len(rows), total=len(names))

    saved = ThsConceptTrendManager.save_trends(trade_date, rows) if rows else 0
    logger.info(f"同花顺概念指数趋势: 成功 {len(rows)}/{len(names)}, 失败 {failed}, 落库 {saved} ({trade_date})")
    if progress_cb:
        progress_cb(pulled=len(rows), total=len(names))
    return {"success": len(rows), "failed": failed, "total": len(names), "saved": saved}
