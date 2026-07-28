import asyncio
import time
from datetime import datetime

from config import POLL_INTERVAL, WATCH_LIST, MY_STOCKS
from db import Database
from provider import AkshareProvider
from tracker import SignalTracker
from strategies.buy_strategy import BuyStrategyEngine
from strategies.sell_strategy import SellStrategyEngine

class DragonPulseMain:
    """龙魂智策系统盘中核心引擎"""
    def __init__(self):
        self.db = Database()
        self.tracker = SignalTracker()
        self.provider = AkshareProvider()

        self.buy_engine = BuyStrategyEngine(self.tracker, self.db)
        self.sell_engine = SellStrategyEngine(self.tracker, self.db)

    async def run(self):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 龙魂智策 AI 系统启动 (Akshare 模式)...")
        print(f"📌 当前观察池: {WATCH_LIST}")
        print(f"📌 当前持仓池: {list(MY_STOCKS.values())}\n")

        while True:
            now = datetime.now()
            # A 股交易时间段判定 (09:25-11:30, 13:00-15:00)
            is_trade_time = (
                (now.hour == 9 and now.minute >= 25) or
                (now.hour == 10) or
                (now.hour == 11 and now.minute <= 30) or
                (13 <= now.hour < 15)
            )

            if is_trade_time:
                start_time = time.time()

                # 1. 异步拉取行情数据
                spot_df = await self.provider.get_spot_data_async()

                # 2. 并行评估买入与卖出逻辑
                if spot_df is not None:
                    await asyncio.gather(
                        self.buy_engine.process(spot_df),
                        self.sell_engine.process(spot_df)
                    )

                cost_time = round(time.time() - start_time, 2)
                print(f"[{now.strftime('%H:%M:%S')}] 扫描完成，耗时 {cost_time}s")

                # 3. 动态休眠以维持 15s 轮询周期
                sleep_time = max(1.0, POLL_INTERVAL - cost_time)
                await asyncio.sleep(sleep_time)
            else:
                # 非交易时间，每 30 秒休眠检查一次
                await asyncio.sleep(30)

if __name__ == "__main__":
    app = DragonPulseMain()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\n系统已手动停止。")
