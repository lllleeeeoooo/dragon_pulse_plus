import logging
import datetime
from typing import List, Dict, Any

from llm.client import llm_client
from config.prompt_templates import PRE_MARKET_SYSTEM_PROMPT, PRE_MARKET_USER_TEMPLATE
from data.news_fetcher import NewsFetcher

logger = logging.getLogger(__name__)


class PreMarketAnalyzer:
    """
    盘前简报分析器 (08:30 触发)
    提取政策热点关键词，预测可能爆发的板块与潜在标的
    """

    @classmethod
    def run_report(cls) -> str:
        """
        运行盘前简报生成
        """
        fetch_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"开始生成盘前简报 ({fetch_time})...")

        # 1. 抓取新闻快讯与热搜词
        news_items = NewsFetcher.get_cls_news(limit=15)
        hot_words = NewsFetcher.get_hot_search_words(limit=15)

        # 格式化文本
        news_text = "\n".join([f"- [{item['time']}] {item['title']}: {item['content'][:100]}..." for item in news_items]) if news_items else "暂无新闻快讯"
        hot_search_text = ", ".join(hot_words) if hot_words else "暂无热搜榜单"

        # 2. 组装 Prompt
        user_prompt = PRE_MARKET_USER_TEMPLATE.format(
            fetch_time=fetch_time,
            news_text=news_text,
            hot_search_text=hot_search_text
        )

        # 3. 调用 LLM 生成简报
        result = llm_client.generate(
            system_prompt=PRE_MARKET_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            module="pre_market"
        )

        return result
