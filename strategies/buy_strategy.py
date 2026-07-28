from config import WATCH_LIST, BUY_LIMIT_UP_MIN_PCT, BUY_LIMIT_UP_MAX_PCT, BUY_LIMIT_UP_MIN_VR, BUY_LIMIT_UP_MIN_AMOUNT
from push import send_bark

class BuyStrategyEngine:
    """买入策略引擎"""
    def __init__(self, tracker, db):
        self.tracker = tracker
        self.db = db

    async def process(self, spot_df):
        if spot_df is None or spot_df.empty:
            return

        # 筛选符合打板冲锋条件的标的
        candidates = spot_df[
            (spot_df['涨跌幅'] >= BUY_LIMIT_UP_MIN_PCT) &
            (spot_df['涨跌幅'] <= BUY_LIMIT_UP_MAX_PCT) &
            (spot_df['量比'] >= BUY_LIMIT_UP_MIN_VR) &
            (spot_df['成交额'] >= BUY_LIMIT_UP_MIN_AMOUNT)
        ]

        for _, row in candidates.iterrows():
            code = str(row['代码'])
            name = str(row['名称'])
            price = float(row['最新价'])
            pct = float(row['涨跌幅'])
            vr = float(row['量比'])
            amount = float(row['成交额'])

            # 条件：属于观察池，或者属于全场成交额超过 15 亿的焦点龙头
            if code in WATCH_LIST or amount >= 1_500_000_000:
                if self.tracker.is_cooled_down(code, "BUY_LIMIT_UP"):
                    msg = f"{name}({code}) 报{price}元, 涨幅{pct}%, 量比{vr}"
                    title = f"🔴 买入信号：【{name}】打板冲锋"

                    send_bark(title, msg, sound="anticipate")
                    self.tracker.mark_pushed(code, "BUY_LIMIT_UP")
                    self.db.record_signal(code, name, "BUY_LIMIT_UP", price, pct, vr)
