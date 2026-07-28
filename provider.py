import asyncio
from datetime import datetime
import akshare as ak

class AkshareProvider:
    """Akshare 数据抓取封装（异步适配层）"""

    @staticmethod
    async def get_spot_data_async():
        """异步获取全市场实时行情快照"""
        loop = asyncio.get_event_loop()
        try:
            df = await loop.run_in_executor(None, ak.stock_zh_a_spot_em)
            return df
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Akshare 行情拉取异常: {e}")
            return None

    @staticmethod
    async def get_zt_pool_async():
        """异步获取涨停池数据"""
        loop = asyncio.get_event_loop()
        try:
            date_str = datetime.now().strftime('%Y%m%d')
            df = await loop.run_in_executor(None, lambda: ak.stock_zt_pool_em(date=date_str))
            return df
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Akshare 涨停池拉取异常: {e}")
            return None
