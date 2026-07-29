import logging
from typing import List, Dict, Any, Optional
import pandas as pd
import akshare as ak

from data.fetcher import retry_on_exception
from config.settings import settings

logger = logging.getLogger(__name__)


class NewsFetcher:
    """
    新闻快讯与同花顺/东财热搜数据抓取类
    """

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_cls_news(limit: int = 20) -> List[Dict[str, Any]]:
        """
        抓取财联社电报/快讯 (stock_info_global_cls)
        """
        try:
            df = ak.stock_info_global_cls()
            if df is not None and not df.empty:
                # 保留最近 limit 条
                records = df.head(limit).to_dict(orient="records")
                news_list = []
                for item in records:
                    news_list.append({
                        "title": item.get("title", "") or item.get("content", "")[:30],
                        "content": item.get("content", ""),
                        "time": str(item.get("pub_time", item.get("time", "")))
                    })
                return news_list
        except Exception as e:
            logger.error(f"抓取财联社新闻失败: {e}")
        return []

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_hot_search_words(limit: int = 15) -> List[str]:
        """
        抓取同花顺/东财热搜个股与关键词
        """
        try:
            # 获取热搜个股排名
            df = ak.stock_hot_rank_em()
            if df is not None and not df.empty:
                codes_names = df.head(limit)[["代码", "股票名称"]].to_dict(orient="records")
                return [f"{item['股票名称']}({item['代码']})" for item in codes_names]
        except Exception as e:
            logger.error(f"抓取热搜榜单失败: {e}")
        return []
