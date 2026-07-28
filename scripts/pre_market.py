import asyncio
from datetime import datetime
import akshare as ak
from config import WATCH_LIST
from push import send_bark

async def run_pre_market():
    """盘前竞价选股简报 (建议 09:20 - 09:26 运行)"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 运行盘前简报...")
    loop = asyncio.get_event_loop()

    try:
        # 获取昨日涨停池
        date_str = datetime.now().strftime('%Y%m%d')
        zt_df = await loop.run_in_executor(None, lambda: ak.stock_zt_pool_previous_em())

        if zt_df is not None and not zt_df.empty:
            # 提取前 3 只表现强势标的作为今日备选
            top_stocks = zt_df.head(3)
            names = "、".join(top_stocks['名称'].tolist())
            msg = f"昨日强龙头竞价关注：{names}。已自动拉入今日重点观察池。"
            send_bark("☀️ 盘前简报：今日竞价观察股", msg, sound="minuet")
        else:
            send_bark("☀️ 盘前简报", "昨日涨停池数据暂未更新，保持默认观察池监控。", sound="minuet")
    except Exception as e:
        print(f"盘前简报运行异常: {e}")

if __name__ == "__main__":
    asyncio.run(run_pre_market())
