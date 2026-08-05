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

# 文件日志 Handler：logs/dragon_pulse.log，7 天滚动保留（完整 INFO 历史）
try:
    import logging.handlers
    os.makedirs("logs", exist_ok=True)
    _file_handler = logging.handlers.TimedRotatingFileHandler(
        "logs/dragon_pulse.log", when="midnight", backupCount=7, encoding="utf-8")
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(_file_handler)
    logger.info("文件日志已就绪: logs/dragon_pulse.log (保留7天)")
except Exception as _e:
    logger.warning(f"文件日志初始化失败: {_e}")

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
    logger.info(f"情绪到顶预警阈值: 连板>={settings.EMOTION_TOP_MAX_LBC}板 & 情绪>={settings.EMOTION_TOP_SENTIMENT_MIN}分 & 炸板率>{settings.EMOTION_TOP_ZHABAN_RATE}%")

    # 0. 启动时同步交易日历 + 建表，确保后续任务不会在假期触发
    from database import TradeCalendarManager
    TradeCalendarManager.sync_calendar()
    logger.info("交易日历已同步，数据库表结构已就绪。")

    # 0.5 龙虎榜名席位种子初始化（幂等，六一中路等人工标签优先于自动分类）
    from database import SeatProfileManager
    SeatProfileManager.seed_famous_seats()
    logger.info("龙虎榜名席位种子已初始化。")

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

    # 18:01 盘后深度复盘
    scheduler.add_job(
        job_post_market,
        trigger="cron",
        hour=18,
        minute=1,
        id="job_post_market",
        name="18:01 盘后深度复盘"
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

    # 04:00 日志保留清理
    from database import LogRetentionCleaner
    def _cleanup_with_track():
        from scheduler.daily_runner import _record_job_run
        _record_job_run("job_log_cleanup", "日志清理")
        LogRetentionCleaner.cleanup()
    scheduler.add_job(
        _cleanup_with_track,
        trigger="cron",
        hour=4,
        minute=0,
        id="job_log_cleanup",
        name="04:00 日志清理(系统/LLM/错误保留15天，推送保留30天)"
    )

    # 04:05 龙头过期标记（超过 30 天无人气自动失效）
    def _expire_dragons():
        from scheduler.daily_runner import _record_job_run
        _record_job_run("job_dragon_expire", "龙头过期标记")
        from database import db_manager
        from database.models import HistoricDragon
        import datetime
        session = db_manager.get_session()
        try:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y%m%d")
            updated = session.query(HistoricDragon).filter(
                HistoricDragon.is_active == True,
                HistoricDragon.peak_date < cutoff
            ).update({"is_active": False}, synchronize_session="fetch")
            if updated:
                session.commit()
                logger.info(f"龙头过期标记: {updated} 只超过30天，标记为失效")
        except Exception as e:
            session.rollback()
            logger.warning(f"龙头过期标记失败: {e}")
        finally:
            session.close()

    scheduler.add_job(
        _expire_dragons,
        trigger="cron",
        hour=4,
        minute=5,
        id="job_dragon_expire",
        name="04:05 龙头过期(>30天自动失效)"
    )

    scheduler.start()
    logger.info("定时任务调度器已启动：")
    logger.info("  04:00  日志清理（系统/LLM/错误 15天，推送 30天）")
    logger.info("  04:05  龙头过期标记（>30天自动失效）")
    logger.info("  08:30  盘前简报（新闻+热搜→LLM 预测板块→Bark 推送）")
    logger.info("  09:26  竞价观察（竞价快照+推荐标的→LLM 买卖指令→Bark 推送）")
    logger.info("  09:30  盘中实时监控（15秒轮询，点火异动+板块联动+AI自动交易）")
    logger.info("  18:01  盘后深度复盘（情绪→风格→LLM复盘→指数/涨停池/板块落库→盈亏报告推送）")
    logger.info("  20:00  假日消息汇总（假期最后一天推送）")
    logger.info("Web 服务: http://127.0.0.1:8000 | 看板: /monitor | API文档: /docs")

    # 2. 在后台异步启动持仓管理 FastAPI API Web 服务 (端口 8000)
    import threading
    from api_server import run_server
    api_thread = threading.Thread(target=run_server, kwargs={"host": settings.API_HOST, "port": 8000}, daemon=True)
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
    except Exception as e:
        logger.error(f"系统运行异常: {e}", exc_info=True)
        scheduler.shutdown()


if __name__ == "__main__":
    main()
