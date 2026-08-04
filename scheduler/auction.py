import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from notifier.bark import bark_notifier
from core.trade_calendar import is_trading_day, is_last_non_trading_day

logger = logging.getLogger(__name__)
import scheduler.helpers as _helpers
from database import RecommendationManager
from llm.call_auction import CallAuctionAnalyzer
def job_call_auction():
    """
    09:26 竞价观察与指令定时任务。非交易日自动跳过。
    """
    _helpers._record_job_run("job_call_auction", "竞价观察")
    if not is_trading_day():
        logger.info("今日非交易日，跳过竞价观察")
        return

    logger.info(">>> 触发 09:26 竞价观察定时任务...")
    try:
        today = datetime.datetime.now()
        today_str = today.strftime("%Y%m%d")
        from core.trade_calendar import get_previous_trading_day
        yesterday_str = get_previous_trading_day(today.date())

        # ---- 1. 昨日 LLM 复盘推荐标的 ----
        # 复盘在 T 日 18:01 以 trade_date=T 落库，T+1 日需按"上一交易日"查询
        pending_recs = RecommendationManager.get_pending_recommendations(trade_date=yesterday_str)
        lines = []
        if pending_recs:
            for r in pending_recs:
                lines.append(f"- [复盘推荐] {r['name']}({r['code']}): {r['strategy_type']}"
                             f" | 要求: {r['open_requirement']}")

        # ---- 2. 昨日涨停池中连板/首板标的（修复 #1）----
        try:
            yesterday_zt = DataFetcher.get_zt_pool(date_str=yesterday_str)
            if yesterday_zt is not None and not yesterday_zt.empty and "lbc" in yesterday_zt.columns:
                # 取连板>=2的高标和首板，按连板数降序
                zt_obs = yesterday_zt[yesterday_zt["lbc"].astype(int) >= 1].head(15)
                for _, zrow in zt_obs.iterrows():
                    zcode = str(zrow.get("code", ""))
                    zname = str(zrow.get("name", ""))
                    zlbc = int(zrow.get("lbc", 1))
                    tag = f"{zlbc}连板" if zlbc >= 2 else "首板"
                    lines.append(f"- [昨涨停-{tag}] {zname}({zcode})")
        except Exception as e:
            logger.warning(f"获取昨日涨停池连板/首板标的失败: {e}")

        recs_summary = "\n".join(lines) if lines else "暂无待观察标的"

        # ---- 3. 盘前简报预测板块 ----
        predicted_summary = _helpers._cached_pre_market_report[:800] if _helpers._cached_pre_market_report else ""

        # ---- 4. 抓取实时竞价快照 ----
        spot_df = DataFetcher.get_realtime_spot()

        # ---- 5. LLM 竞价观察指令 ----
        # 读取 09:25 规则引擎竞价预判作为 LLM 上下文
        from scheduler.monitor_auction import _auction_prediction_cache
        auction_prediction = _auction_prediction_cache or ""

        # 获取昨日涨停溢价，用于竞价风控
        premium_info = DataFetcher.get_yesterday_zt_premium()
        result = CallAuctionAnalyzer.run_auction_analysis(
            trade_date=today_str,
            auction_df=spot_df,
            yesterday_zt_auction_yield=premium_info.get("intraday_premium", 1.5),
            recommended_targets_summary=recs_summary,
            predicted_sectors_summary=predicted_summary,
            auction_prediction=auction_prediction
        )

        bark_notifier.send(
            title="🎯 09:26 竞价超预期指令",
            body=result,
            group="竞价指令",
            level="timeSensitive"
        )
    except Exception as e:
        logger.error(f"09:26 竞价观察执行异常: {e}")


