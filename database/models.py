import os
import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, Index
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# 基础模型类
Base = declarative_base()


class Holding(Base):
    """
    持仓股票表
    存储当前持仓个股、买入成本、买入战法、持仓类型及实时收益率
    """
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(12), nullable=False, index=True, comment="股票代码，如 000001")
    name = Column(String(32), nullable=False, comment="股票名称")
    cost_price = Column(Float, nullable=False, default=0.0, comment="买入成本价")
    current_price = Column(Float, default=0.0, comment="最新实时价格")
    profit_rate = Column(Float, default=0.0, comment="实时收益率 (%) = (最新价 - 成本价) / 成本价 * 100")
    quantity = Column(Integer, nullable=False, default=100, comment="持仓数量")
    buy_date = Column(String(10), nullable=False, comment="买入日期 YYYY-MM-DD")
    buy_strategy = Column(String(32), default="手动/通用", comment="买入战法标签 (低吸/打板/二波/抱团/共振)")
    holding_type = Column(String(16), default="MANUAL", comment="持仓类型: MANUAL(手动持仓), AI_AUTO(AI自动持仓)")
    was_limit_up_today = Column(Boolean, default=False, comment="今日是否曾经封涨停")
    status = Column(String(16), default="HOLDING", comment="持仓状态: HOLDING(持仓中), CLOSED(已平仓)")
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


class Recommendation(Base):
    """
    复盘/竞价推荐标的表
    存储盘后复盘 LLM 推荐的观察标的及次日买入条件，供竞价观察与盘中轮询调用
    """
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, index=True, comment="推荐对应的交易日期 YYYYMMDD")
    code = Column(String(12), nullable=False, index=True, comment="股票代码")
    name = Column(String(32), nullable=False, comment="股票名称")
    strategy_type = Column(String(32), nullable=False, comment="推荐战法 (打板/弱转强/1进2/低吸/二波)")
    open_requirement = Column(String(128), comment="开盘要求 (如 高开 +3%~+6%)")
    auction_vol_ratio = Column(String(64), comment="竞价量能占比要求 (如 10%+)")
    buy_condition = Column(Text, comment="买入详细条件")
    sell_condition = Column(Text, comment="止盈止损条件")
    status = Column(String(16), default="PENDING", comment="状态: PENDING(待观察), TRIGGERED(已买入), EXPIRED(已失效)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class DailySentiment(Base):
    """
    每日情绪向量与周期历史表
    存储每日 5D 情绪多维向量分值与 LLM 周期定性结论，用于跨日趋势对比
    """
    __tablename__ = "daily_sentiment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True, comment="交易日期 YYYYMMDD")
    height = Column(Integer, default=0, comment="最高连板数")
    breadth = Column(Integer, default=0, comment="涨跌停差值")
    zt_count = Column(Integer, default=0, comment="涨停家数")
    dt_count = Column(Integer, default=0, comment="跌停家数")
    zhaban_count = Column(Integer, default=0, comment="炸板家数")
    yield_rate = Column(Float, default=0.0, comment="昨日涨停今日溢价率(%)")
    seal_force_ratio = Column(Float, default=0.0, comment="封单占全市场成交额比例(%)")
    zhaban_rate = Column(Float, default=0.0, comment="炸板率(%)")
    sentiment_index = Column(Float, default=0.0, comment="综合情绪分值(0-100)")
    cycle_stage = Column(String(32), comment="情绪周期定性 (冰点/启动/发酵/高潮/退潮)")
    summary = Column(Text, comment="复盘总结摘要")
    created_at = Column(DateTime, default=datetime.datetime.now)


class HistoricDragon(Base):
    """
    历史人气龙头标的表 (用于二波战法溯源)
    记录过去 30 天出现过的连板龙头、见顶日期与最高价，用于精准计算 30%-50% 回撤止跌
    """
    __tablename__ = "historic_dragons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(12), nullable=False, index=True, comment="股票代码")
    name = Column(String(32), nullable=False, comment="股票名称")
    max_lbc = Column(Integer, nullable=False, comment="最高连板数")
    peak_date = Column(String(10), nullable=False, comment="见顶日期 YYYYMMDD")
    peak_price = Column(Float, nullable=False, comment="见顶最高价")
    board_name = Column(String(64), comment="所属主线题材板块")
    is_active = Column(Boolean, default=True, comment="是否处于二波观察期 (30天内)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class PushLog(Base):
    """
    推送通知日志表
    记录每一次 Bark 推送的标题、内容、分组、优先级及发送结果
    """
    __tablename__ = "push_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(256), nullable=False, comment="推送标题")
    body = Column(Text, nullable=False, comment="推送正文")
    push_group = Column(String(64), comment="推送分组 (盘前简报/竞价指令/盘后复盘/盘中异动/AI自动持仓/卖出提醒/炸板提醒)")
    level = Column(String(32), default="active", comment="优先级 (active/timeSensitive/passive)")
    send_success = Column(Boolean, default=False, comment="是否发送成功")
    error_msg = Column(String(256), comment="发送失败时的错误信息")
    created_at = Column(DateTime, default=datetime.datetime.now, index=True, comment="推送时间")

    __table_args__ = (
        Index("idx_push_log_created_at", "created_at"),
        Index("idx_push_log_group", "push_group"),
    )


class LLMLog(Base):
    """
    LLM 调用日志表
    记录每一次大模型请求的输入、输出、调用模块及 Token 消耗
    """
    __tablename__ = "llm_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String(32), nullable=False, index=True, comment="调用模块: pre_market/call_auction/post_market/sell_advisor")
    model = Column(String(32), comment="模型名称")
    system_prompt = Column(Text, comment="系统提示词")
    user_prompt = Column(Text, comment="用户输入")
    response = Column(Text, comment="AI 返回内容")
    tokens_used = Column(Integer, default=0, comment="消耗 Token 数")
    success = Column(Boolean, default=True, comment="是否成功")
    error_msg = Column(String(256), comment="失败原因")
    created_at = Column(DateTime, default=datetime.datetime.now, index=True, comment="调用时间")

    __table_args__ = (
        Index("idx_llm_log_module", "module"),
        Index("idx_llm_log_created_at", "created_at"),
    )


class ErrorLog(Base):
    """
    系统错误日志表
    自动拦截所有 logger.error / logger.warning 写入数据库
    覆盖：akshare 接口异常、推送异常、代码运行异常等
    """
    __tablename__ = "error_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(16), nullable=False, index=True, comment="日志级别: ERROR / WARNING")
    module = Column(String(64), index=True, comment="来源模块 logger name")
    message = Column(Text, nullable=False, comment="错误/警告信息")
    traceback = Column(Text, comment="异常堆栈（如有）")
    created_at = Column(DateTime, default=datetime.datetime.now, index=True, comment="发生时间")

    __table_args__ = (
        Index("idx_error_log_level", "level"),
        Index("idx_error_log_created_at", "created_at"),
    )


class TradeCalendar(Base):
    """
    交易日历表
    缓存过去30天+未来30天的交易日，每日自动维护
    """
    __tablename__ = "trade_calendar"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True, comment="交易日 YYYY-MM-DD")


def _ensure_column(engine, table_name: str, column_name: str, column_def: str):
    """
    安全添加列：仅在列不存在时执行 ALTER TABLE。
    精确捕获 'duplicate column' 错误而非吞掉所有异常。
    """
    import logging
    _log = logging.getLogger(__name__)
    from sqlalchemy import text, inspect
    try:
        insp = inspect(engine)
        existing = [c["name"] for c in insp.get_columns(table_name)]
        if column_name not in existing:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def};"))
                conn.commit()
            _log.info(f"数据库自动迁移: {table_name}.{column_name} 字段已添加")
    except Exception as e:
        err_msg = str(e).lower()
        if "duplicate" in err_msg or "already exists" in err_msg:
            return  # 预期内的重复列
        _log.warning(f"数据库迁移 {table_name}.{column_name} 出现非预期异常: {e}")


# 数据库引擎与会话工厂封装
class DatabaseManager:
    """
    SQLite 数据库管理器
    """

    def __init__(self, db_path: str = "dragon_pulse.db"):
        self.db_path = db_path
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={
                "check_same_thread": False,  # 允许多线程/定时任务访问
            },
            echo=False,
            # 连接池配置：减少并发写冲突
            pool_size=1,
            max_overflow=3,
            pool_pre_ping=True,
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.create_tables()
        # 启用 WAL 模式提升并发读/写性能，降低 "database is locked" 概率
        self._enable_wal()

    def _enable_wal(self):
        try:
            with self.engine.connect() as conn:
                from sqlalchemy import text
                conn.execute(text("PRAGMA journal_mode=WAL;"))
                conn.execute(text("PRAGMA busy_timeout=5000;"))
                conn.commit()
        except Exception:
            pass

    def create_tables(self):
        """创建所有数据表，并自动补充新增字段"""
        Base.metadata.create_all(bind=self.engine)
        # SQLite 表结构兼容性补全（仅忽略"字段已存在"错误，其他异常应暴露）
        _ensure_column(self.engine, "holdings", "current_price", "FLOAT DEFAULT 0.0")
        _ensure_column(self.engine, "holdings", "profit_rate", "FLOAT DEFAULT 0.0")
        _ensure_column(self.engine, "holdings", "holding_type", "VARCHAR(16) DEFAULT 'MANUAL'")

    def get_session(self) -> Session:
        """获取数据库 Session"""
        return self.SessionLocal()