import time
from config import COOL_DOWN_SECONDS, MY_STOCKS

class SignalTracker:
    """信号追踪管理与冷却去重引擎"""
    def __init__(self):
        # 记录已被推送的信号时间戳: { "000099_BUY": timestamp }
        self.pushed_signals = {}
        # 记录股票当前状态
        self.stock_states = {code: "HOLDING" for code in MY_STOCKS.keys()}

    def is_cooled_down(self, code: str, signal_type: str) -> bool:
        """判断信号是否超出冷却期"""
        key = f"{code}_{signal_type}"
        last_time = self.pushed_signals.get(key, 0)
        return (time.time() - last_time) > COOL_DOWN_SECONDS

    def mark_pushed(self, code: str, signal_type: str):
        """标记信号已推送并刷新时间戳"""
        key = f"{code}_{signal_type}"
        self.pushed_signals[key] = time.time()
        self.stock_states[code] = f"{signal_type}_PUSHED"
