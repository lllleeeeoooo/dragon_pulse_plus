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
from scheduler.helpers import _record_job_run
def job_holiday_news_summary():
    """
    节假日/周末最后一天 20:00 执行。
    汇总休市期间的重要新闻，让开盘前有充足的信息准备。
    """
    _record_job_run("job_holiday_summary", "假日消息汇总")
    if not is_last_non_trading_day():
        logger.info("今日非假期最后一天，跳过假日消息汇总")
        return

    logger.info(">>> 触发假日消息汇总（假期最后一天 20:00）...")
    try:
        # 拉取近期新闻
        news_items = NewsFetcher.get_cls_news(limit=30)
        hot_words = NewsFetcher.get_hot_search_words(limit=20)

        news_text = "\n".join(
            [f"- [{item.get('time', '')}] {item.get('title', '')}: {item.get('content', '')[:120]}..."
             for item in news_items]
        ) if news_items else "暂无新闻快讯"
        hot_text = ", ".join(hot_words) if hot_words else "暂无热搜"

        system_prompt = """你是A股策略分析师。下方是休市期间的重要新闻和热搜，请汇总为一份简洁的开盘前简报。
格式要求：
1. 最重要的 3-5 条政策/行业新闻（标题+一句话解读）
2. 可能影响的开盘板块和方向判断
3. 总体情绪评估（偏多/偏空/中性）
字数不超过300字。
"""
        user_prompt = f"""休市期间新闻快讯：
{news_text}

热搜关键词：
{hot_text}

请汇总为开盘前简报。"""

        summary = llm_client.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            module="holiday_summary"
        )

        bark_notifier.send(
            title="📰 假日消息汇总 | 开盘前简报",
            body=summary,
            group="假日汇总",
            level="timeSensitive"
        )
        logger.info("假日消息汇总完成，已推送")
    except Exception as e:
        logger.error(f"假日消息汇总执行异常: {e}")
