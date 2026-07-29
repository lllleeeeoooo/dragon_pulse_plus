import os
import time
import logging
from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from scheduler.daily_runner import job_pre_market, job_call_auction, job_post_market, job_holiday_news_summary
from scheduler.market_monitor import MarketMonitor

# 配置统一 Log 输出格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("DragonPulsePlus")

# 注册数据库日志 Handler：自动将 WARNING+ 级别日志写入 error_logs 表
from database.log_handler import DatabaseLogHandler
logging.getLogger().addHandler(DatabaseLogHandler(level=logging.WARNING))
logger.info("系统错误日志 Handler 已注册（WARNING/ERROR 级别自动落库）")


def _validate_startup_config():
    """启动前关键配置校验，缺少必要配置时给出明确警告"""
    warnings = []

    if not settings.LLM_API_KEY or settings.LLM_API_KEY == "your_llm_api_key_here":
        warnings.append("LLM_API_KEY 未配置！AI 决策将无法工作，请编辑 .env 文件或设"
                       "置 LLM_API_KEY 环境变量。")

    if settings.BARK_ENABLED and (not settings.BARK_TOKEN or settings.BARK_TOKEN == "your_bark_token_here"):
        warnings.append("BARK_TOKEN 未配置！推送通知将无法发送，请编辑 .env 文件或设"
                       "置 BARK_TOKEN 环境变量。")

    for w in warnings:
        logger.warning(f"⚠️  {w}")


def main():
    logger.info("==================================================")
    logger.info("  🚀 dragon_pulse_plus AI智能策略系统 启动中... ")
    logger.info("==================================================")

    # ---- 启动前配置校验 ----
    _validate_startup_config()

    logger.info(f"LLM 模型: {settings.LLM_MODEL} | BaseURL: {settings.LLM_BASE_URL}")
    logger.info(f"Bark 推送状态: {'已启用' if settings.BARK_ENABLED else '未启用'}")
    logger.info(f"盘中轮询间隔: {settings.MONITOR_INTERVAL_SECONDS} 秒")
    logger.info(f"情绪到顶预警阈值: 连板>={settings.EMOTION_TOP_MAX_LBC}板 & 炸板率>{settings.EMOTION_TOP_ZHABAN_RATE}%")

    # 1. 初始化 APScheduler 定时任务
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    # 08:30 盘前简报
    scheduler.add_job(
        job_pre_market,
        trigger="cron",
        hour=8,
        minute=30,
        id="job_pre_market",
        name="08:30 盘前简报"
    )

    # 09:26 竞价观察与指令
    scheduler.add_job(
        job_call_auction,
        trigger="cron",
        hour=9,
        minute=26,
        id="job_call_auction",
        name="09:26 竞价观察"
    )

    # 15:30 盘后深度复盘
    scheduler.add_job(
        job_post_market,
        trigger="cron",
        hour=15,
        minute=30,
        id="job_post_market",
        name="15:30 盘后深度复盘"
    )

    # 20:00 假日消息汇总（仅假期/周末最后一天执行，内部自动判断）
    scheduler.add_job(
        job_holiday_news_summary,
        trigger="cron",
        hour=20,
        minute=0,
        id="job_holiday_summary",
        name="20:00 假日消息汇总"
    )

    scheduler.start()
    logger.info("定时任务调度器已启动 (08:30 简报 / 09:26 竞价 / 15:30 复盘)。")

    # 2. 在后台异步启动持仓管理 FastAPI API Web 服务 (端口 8000)
    import threading
    from api_server import run_server
    api_thread = threading.Thread(target=run_server, kwargs={"host": "0.0.0.0", "port": 8000}, daemon=True)
    api_thread.start()
    logger.info("持仓管理 HTTP API 服务已在后台启动 (端口 8000)。")

    # 3. 启动盘中 15 秒轮询监控线程
    monitor = MarketMonitor()
    try:
        # 在主线程中持续运行盘中监控
        monitor.run_polling_loop()
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到底层终止信号，正在关闭系统...")
        scheduler.shutdown()
        logger.info("系统已安全退出。")


if __name__ == "__main__":
    main()
