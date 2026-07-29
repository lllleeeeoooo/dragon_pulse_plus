"""
数据库日志 Handler
自动将所有 logger.error() 和 logger.warning() 写入 error_logs 表。
在 main.py 启动时注册，零侵入拦截全系统异常。
"""
import logging
import traceback
from logging import Handler


class DatabaseLogHandler(Handler):
    """
    自定义 logging Handler，将 ERROR/WARNING 级别日志写入 ErrorLog 表。
    数据库写入失败时降级到 stderr，避免循环。
    """

    def __init__(self, level=logging.WARNING):
        super().__init__(level=level)
        self._fallback = logging.StreamHandler()

    def emit(self, record: logging.LogRecord):
        try:
            # 延迟导入避免循环依赖
            from database.services import ErrorLogManager

            tb_text = ""
            if record.exc_info and record.exc_info[1]:
                tb_text = "".join(traceback.format_exception(*record.exc_info))

            ErrorLogManager.add_log(
                level=record.levelname,
                module=record.name,
                message=self.format(record),
                traceback=tb_text
            )
        except Exception:
            # 数据库写入失败，降级到 stderr
            self._fallback.emit(record)
