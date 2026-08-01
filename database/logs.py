import logging
from typing import List, Dict, Optional, Any
from database.models import PushLog, LLMLog, ErrorLog, SystemLog
from database.connection import db_manager
logger = logging.getLogger(__name__)

class PushLogManager:
    """
    推送通知日志数据库管理服务
    """

    @staticmethod
    def add_log(
        title: str,
        body: str,
        push_group: str = "",
        level: str = "active",
        send_success: bool = False,
        error_msg: str = ""
    ):
        """新增一条推送日志"""
        session = db_manager.get_session()
        try:
            log = PushLog(
                title=title,
                body=body,
                push_group=push_group,
                level=level,
                send_success=send_success,
                error_msg=error_msg
            )
            session.add(log)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"保存推送日志失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_logs(
        date_str: Optional[str] = None,
        push_group: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        查询推送历史日志，支持按日期 (YYYY-MM-DD) 和分组筛选
        """
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            query = session.query(PushLog).order_by(PushLog.created_at.desc())
            if date_str:
                # SQLite 下用 strftime 提取日期部分做比较
                query = query.filter(func.strftime("%Y-%m-%d", PushLog.created_at) == date_str)
            if push_group:
                query = query.filter(PushLog.push_group == push_group)
            logs = query.limit(limit).all()
            return [
                {
                    "id": l.id,
                    "title": l.title,
                    "body": l.body[:200],  # 摘要截断，完整内容按需查询
                    "push_group": l.push_group,
                    "level": l.level,
                    "send_success": l.send_success,
                    "error_msg": l.error_msg,
                    "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
                }
                for l in logs
            ]
        finally:
            session.close()


class LLMLogManager:
    """LLM 调用日志管理服务"""

    @staticmethod
    def add_log(
        module: str,
        model: str = "",
        system_prompt: str = "",
        user_prompt: str = "",
        response: str = "",
        tokens_used: int = 0,
        success: bool = True,
        error_msg: str = ""
    ):
        """新增一条 LLM 调用日志"""
        session = db_manager.get_session()
        try:
            log = LLMLog(
                module=module,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=response,
                tokens_used=tokens_used,
                success=success,
                error_msg=error_msg or ""
            )
            session.add(log)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"保存 LLM 日志失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_logs(
        module: Optional[str] = None,
        date_str: Optional[str] = None,
        success_only: Optional[bool] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """查询 LLM 调用历史"""
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            query = session.query(LLMLog).order_by(LLMLog.created_at.desc())
            if module:
                query = query.filter(LLMLog.module == module)
            if date_str:
                query = query.filter(func.strftime("%Y-%m-%d", LLMLog.created_at) == date_str)
            if success_only is not None:
                query = query.filter(LLMLog.success == success_only)
            logs = query.limit(limit).all()
            return [
                {
                    "id": l.id,
                    "module": l.module,
                    "model": l.model,
                    "system_prompt": l.system_prompt[:300] if l.system_prompt else "",
                    "user_prompt": l.user_prompt[:500] if l.user_prompt else "",
                    "response": l.response[:500] if l.response else "",
                    "tokens_used": l.tokens_used,
                    "success": l.success,
                    "error_msg": l.error_msg,
                    "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
                }
                for l in logs
            ]
        finally:
            session.close()


class ErrorLogManager:
    """系统错误日志管理服务"""

    @staticmethod
    def add_log(level: str = "ERROR", module: str = "", message: str = "", traceback: str = ""):
        """新增一条错误日志"""
        session = db_manager.get_session()
        try:
            log = ErrorLog(
                level=level,
                module=module,
                message=message,
                traceback=traceback or ""
            )
            session.add(log)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    @staticmethod
    def get_logs(
        level: Optional[str] = None,
        date_str: Optional[str] = None,
        module: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """查询错误日志"""
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            query = session.query(ErrorLog).order_by(ErrorLog.created_at.desc())
            if level:
                query = query.filter(ErrorLog.level == level.upper())
            if date_str:
                query = query.filter(func.strftime("%Y-%m-%d", ErrorLog.created_at) == date_str)
            if module:
                query = query.filter(ErrorLog.module.like(f"%{module}%"))
            logs = query.limit(limit).all()
            return [
                {
                    "id": l.id,
                    "level": l.level,
                    "module": l.module,
                    "message": l.message[:500] if l.message else "",
                    "traceback": l.traceback[:1000] if l.traceback else "",
                    "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
                }
                for l in logs
            ]
        finally:
            session.close()


class LogRetentionCleaner:
    """
    日志保留策略清理器
    - system_logs / error_logs / llm_logs: 保留 15 天
    - push_logs: 保留 30 天
    """

    @staticmethod
    def cleanup():
        """清理所有过期日志，每日调用一次"""
        import datetime
        today = datetime.date.today()
        cutoff_15 = (today - datetime.timedelta(days=15)).isoformat()
        cutoff_30 = (today - datetime.timedelta(days=30)).isoformat()

        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            # 15 天保留的表：用 func.date() 做日期级别比较，避免字符串拼接
            for model, label in [(SystemLog, "system_logs"),
                                  (ErrorLog, "error_logs"),
                                  (LLMLog, "llm_logs")]:
                deleted = session.query(model).filter(
                    func.date(model.created_at) < cutoff_15
                ).delete(synchronize_session="fetch")
                if deleted:
                    logger.info(f"日志清理: {label} 删除 {deleted} 条 (>15天)")

            # 30 天保留
            deleted_push = session.query(PushLog).filter(
                func.date(PushLog.created_at) < cutoff_30
            ).delete(synchronize_session="fetch")
            if deleted_push:
                logger.info(f"日志清理: push_logs 删除 {deleted_push} 条 (>30天)")

            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"日志清理失败: {e}")
        finally:
            session.close()
