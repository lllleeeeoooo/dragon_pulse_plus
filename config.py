import os

# ==================== 系统配置 ====================
# Bark 推送 Key (请替换为你的专属 Key)
BARK_KEY = os.getenv("BARK_KEY", "QnW8Cbmg6HCsdoGc7i6r5c")

# 盘中轮询间隔 (秒) - 建议 15s 以防 Akshare 被封 IP
POLL_INTERVAL = 15

# 信号冷却时间 (秒) - 30 分钟内同一股票同一信号不重复推送
COOL_DOWN_SECONDS = 1800

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), "dragon_pulse.db")

# ==================== 默认池配置 ====================
# 观察池 (盘前选股或手动关注的标的)
WATCH_LIST = ["603613", "002761", "000099"]

# 当前持仓池 {股票代码: 股票名称}
MY_STOCKS = {
    "002085": "万丰奥威"
}

# ==================== 策略阈值配置 ====================
# 打板买入触发阈值
BUY_LIMIT_UP_MIN_PCT = 8.5   # 最小涨幅
BUY_LIMIT_UP_MAX_PCT = 9.8   # 最大涨幅 (未涨停)
BUY_LIMIT_UP_MIN_VR = 1.8    # 最小量比
BUY_LIMIT_UP_MIN_AMOUNT = 400_000_000  # 最小成交额 (4亿)

# 卖出触发阈值
SELL_FALL_FROM_HIGH_PCT = 0.035  # 高位跳水回落比例 (3.5%)
SELL_STOP_LOSS_PCT = -4.5        # 硬止损跌幅比例 (-4.5%)
