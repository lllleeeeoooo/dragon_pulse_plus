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
from scheduler.helpers import _cached_pre_market_report, _record_job_run
from llm.pre_market import PreMarketAnalyzer
def job_pre_market():
    """
    08:30 盘前简报定时任务。非交易日自动跳过。
    """
    _record_job_run("job_pre_market", "盘前简报")
    if not is_trading_day():
        logger.info("今日非交易日，跳过盘前简报")
        return

    global _cached_pre_market_report
    logger.info(">>> 触发 08:30 盘前简报定时任务...")
    try:
        # ---- 同步立案调查/违规处罚数据 ----
        from database import InvestigationManager
        new_risks = InvestigationManager.sync_from_gsrl()
        if new_risks:
            blacklist = InvestigationManager.get_blacklist_codes()
            logger.warning(f"立案调查黑名单已更新，当前 {len(blacklist)} 只风险股票")

        report = PreMarketAnalyzer.run_report()
        _cached_pre_market_report = report  # 缓存供 09:26 竞价使用（修复 #2）
        # 发送 Bark 推送
        bark_notifier.send(
            title="☀️ 盘前简报与爆发题材预测",
            body=report,
            group="盘前简报"
        )
    except Exception as e:
        logger.error(f"08:30 盘前简报执行异常: {e}")


