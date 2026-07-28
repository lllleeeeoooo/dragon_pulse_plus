from config import MY_STOCKS, SELL_FALL_FROM_HIGH_PCT, SELL_STOP_LOSS_PCT
from push import send_bark

class SellStrategyEngine:
    """卖出策略引擎"""
    def __init__(self, tracker, db):
        self.tracker = tracker
        self.db = db

    async def process(self, spot_df):
        if spot_df is None or spot_df.empty or not MY_STOCKS:
            return

        holdings_code = list(MY_STOCKS.keys())
        my_spot = spot_df[spot_df['代码'].isin(holdings_code)]

        for _, row in my_spot.iterrows():
            code = str(row['代码'])
            name = MY_STOCKS.get(code, str(row['名称']))
            price = float(row['最新价'])
            high = float(row['最高'])
            pct = float(row['涨跌幅'])

            # 1. 高位跳水止盈 (从最高点回落比例)
            if high > 0 and (high - price) / high >= SELL_FALL_FROM_HIGH_PCT:
                if self.tracker.is_cooled_down(code, "SELL_FALL"):
                    msg = f"{name} 从最高{high}元回落至{price}元，回落过大！"
                    title = f"🟢 卖出预警：【{name}】高位跳水"

                    send_bark(title, msg, sound="alarm")
                    self.tracker.mark_pushed(code, "SELL_FALL")
                    self.db.record_signal(code, name, "SELL_FALL", price, pct)

            # 2. 硬性破位止损
            if pct <= SELL_STOP_LOSS_PCT:
                if self.tracker.is_cooled_down(code, "SELL_STOP"):
                    msg = f"{name} 当前跌幅{pct}%，触及止损线，按纪律离场！"
                    title = f"🟢 强制止损：【{name}】触及止损"

                    send_bark(title, msg, sound="emergency")
                    self.tracker.mark_pushed(code, "SELL_STOP")
                    self.db.record_signal(code, name, "SELL_STOP", price, pct)
