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
    prev_close_price = Column(Float, default=0.0, comment="上一交易日收盘价，用于计算今日浮动盈亏")
    profit_rate = Column(Float, default=0.0, comment="实时收益率 (%) = (最新价 - 成本价) / 成本价 * 100")
    quantity = Column(Integer, nullable=False, default=100, comment="持仓数量")
    buy_date = Column(String(10), nullable=False, comment="买入日期 YYYY-MM-DD（注意：与其他表的 YYYYMMDD 格式不同）")
    buy_strategy = Column(String(32), default="手动/通用", comment="买入战法标签 (低吸/打板/二波/抱团/共振)")
    holding_type = Column(String(16), default="MANUAL", comment="持仓类型: MANUAL(手动持仓), AI_AUTO(AI自动持仓)")
    was_limit_up_today = Column(Boolean, default=False, comment="今日是否曾经封涨停")
    sell_price = Column(Float, default=0.0, comment="卖出/平仓价格")
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
    eval_note = Column(Text, comment="复盘评估备注（LLM 对次日表现的一言点评与评分）")
    eval_score = Column(Integer, comment="复盘评估胜率评分 0-100（逐标的，断链8修复）")
    auction_verdict = Column(String(8), comment="竞价 LLM 结论: 买入/观察/放弃（09:26 写入，盘中按此门控自动买入）")
    auction_premise = Column(String(8), comment="竞价 LLM 对开盘前提是否满足的声明: 满足/不满足（判断=买入 且 前提=满足 才执行）")
    auction_amount = Column(Float, comment="09:26 竞价成交额(元)，供盘中竞价量能校验（断链3修复）")
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
    total_amount = Column(Float, default=0.0, comment="全市场当日总成交额(亿元)")
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


class MarketIndex(Base):
    """
    大盘指数日线数据表
    每个交易日一行，存储上证/深证/创业板收盘价及涨跌幅。
    用于大盘熔断基准、偏离度计算、盈亏对比。
    """
    __tablename__ = "market_index"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True, comment="交易日期 YYYYMMDD")
    sh_close = Column(Float, default=0.0, comment="上证指数收盘价")
    sh_change_pct = Column(Float, default=0.0, comment="上证指数涨跌幅(%)")
    sz_close = Column(Float, default=0.0, comment="深证成指收盘价")
    sz_change_pct = Column(Float, default=0.0, comment="深证成指涨跌幅(%)")
    gem_close = Column(Float, default=0.0, comment="创业板指收盘价")
    gem_change_pct = Column(Float, default=0.0, comment="创业板指涨跌幅(%)")
    total_amount = Column(Float, default=0.0, comment="全市场成交额(亿元)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class DailyEquitySnapshot(Base):
    """
    每日净值快照表
    盘后保存当日总权益、浮动盈亏、已实现盈亏等，用于画资金曲线和绩效指标。
    """
    __tablename__ = "daily_equity_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True, comment="交易日期 YYYYMMDD")
    total_equity = Column(Float, default=0.0, comment="总权益(元)")
    available_cash = Column(Float, default=0.0, comment="可用资金(元)")
    position_value = Column(Float, default=0.0, comment="持仓市值(元)")
    unrealized_pnl = Column(Float, default=0.0, comment="浮动盈亏(元)")
    today_realized_pnl = Column(Float, default=0.0, comment="今日已实现盈亏(元)")
    total_realized_pnl = Column(Float, default=0.0, comment="累计已实现盈亏(元)")
    position_count = Column(Integer, default=0, comment="持仓数量")
    today_pnl_pct = Column(Float, default=0.0, comment="今日收益率(%)")
    cumulative_pnl_pct = Column(Float, default=0.0, comment="累计收益率(%)")
    sh_change_pct = Column(Float, default=0.0, comment="当日上证涨跌幅(%)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class DailyZtPool(Base):
    """
    每日涨停池明细表
    保存每天涨停个股的关键字段，用于回溯板块启动、连板晋级率、龙头更替。
    """
    __tablename__ = "daily_zt_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, index=True, comment="交易日期 YYYYMMDD")
    code = Column(String(12), nullable=False, index=True, comment="股票代码")
    name = Column(String(32), nullable=False, comment="股票名称")
    price = Column(Float, default=0.0, comment="封板价")
    change_pct = Column(Float, default=0.0, comment="涨跌幅(%)")
    lbc = Column(Integer, default=1, comment="连板数")
    seal_amount = Column(Float, default=0.0, comment="封单金额(元)")
    first_seal_time = Column(String(16), comment="首次封板时间 HH:MM:SS")
    open_count = Column(Integer, default=0, comment="炸板次数")
    industry = Column(String(64), comment="所属行业/板块")
    amount = Column(Float, default=0.0, comment="成交额(元)")
    turnover_rate = Column(Float, default=0.0, comment="换手率(%)")
    circ_market_cap = Column(Float, default=0.0, comment="流通市值(元)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class SectorStrength(Base):
    """
    每日板块强度表
    统计每个行业/概念板块的涨停家数、占比、加速情况，用于板块轮动分析。
    """
    __tablename__ = "sector_strength"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, index=True, comment="交易日期 YYYYMMDD")
    sector_name = Column(String(64), nullable=False, comment="板块名称")
    zt_count = Column(Integer, default=0, comment="涨停家数")
    prev_zt_count = Column(Integer, default=0, comment="上日涨停家数")
    acceleration = Column(Integer, default=0, comment="加速：今日-上日")
    total_stocks = Column(Integer, default=0, comment="板块成分股总数")
    zt_ratio_pct = Column(Float, default=0.0, comment="涨停占比(%)")
    top_stocks = Column(String(256), comment="领涨标的,逗号分隔 code:name")
    created_at = Column(DateTime, default=datetime.datetime.now)


class SectorCycle(Base):
    """
    板块情绪周期阶段表（"主线板块 → 板块阶段 → 个股机会"三层的板块层）
    每日从涨停池按行业聚合：判定每个活跃板块处于 冰点/启动/发酵/高潮/退潮 哪个阶段，
    并计算主线分（涨停×持续×加速×高度），主线板块供盘中个股机会打分使用。
    """
    __tablename__ = "sector_cycle"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, index=True, comment="交易日期 YYYYMMDD")
    sector_name = Column(String(64), nullable=False, index=True, comment="板块名称(东财行业)")
    phase = Column(String(16), default="冰点", comment="板块阶段: 冰点/启动/发酵/高潮/退潮")
    zt_count = Column(Integer, default=0, comment="当日板块涨停家数")
    max_lbc = Column(Integer, default=0, comment="板块内最高连板")
    prev_zt_count = Column(Integer, default=0, comment="上一交易日涨停家数")
    prev_phase = Column(String(16), comment="上一交易日阶段")
    is_mainline = Column(Boolean, default=False, comment="是否主线板块")
    mainline_score = Column(Float, default=0.0, comment="主线分(涨停×持续×加速×高度归一化)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class ConceptMember(Base):
    """
    概念板块成员映射表（切片3：概念主线识别数据底座）
    存储 概念 → 成分股 的当前映射（新浪 gn_ 代码）。refresh_date 标记快照日期，
    盘后按日刷新；成分股变化慢，刷新间隔由 CONCEPT_MEMBER_REFRESH_INTERVAL_DAYS 控制。
    """
    __tablename__ = "concept_member"

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_code = Column(String(16), nullable=False, index=True, comment="概念代码(新浪 gn_xxx)")
    concept_name = Column(String(64), nullable=False, index=True, comment="概念名称")
    stock_code = Column(String(12), nullable=False, index=True, comment="成分股代码")
    refresh_date = Column(String(10), index=True, comment="快照日期 YYYYMMDD")
    created_at = Column(DateTime, default=datetime.datetime.now)


class ConceptCycle(Base):
    """
    概念情绪周期阶段表（切片3：概念主线识别落库）
    与 sector_cycle 同构，但维度为「题材概念」（经 core.concept_filter 过滤非题材标签）：
    每日从涨停池按概念聚合，判定每个活跃概念的 冰点/启动/发酵/高潮/退潮 与主线分，
    供概念主线复盘与后续盘中概念因子使用。
    """
    __tablename__ = "concept_cycle"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, index=True, comment="交易日期 YYYYMMDD")
    concept_name = Column(String(64), nullable=False, index=True, comment="概念名称(题材型)")
    phase = Column(String(16), default="冰点", comment="概念阶段: 冰点/启动/发酵/高潮/退潮")
    zt_count = Column(Integer, default=0, comment="当日概念涨停家数")
    max_lbc = Column(Integer, default=0, comment="概念内最高连板")
    prev_zt_count = Column(Integer, default=0, comment="上一交易日涨停家数")
    prev_phase = Column(String(16), comment="上一交易日阶段")
    is_mainline = Column(Boolean, default=False, comment="是否主线概念")
    mainline_score = Column(Float, default=0.0, comment="主线分(涨停×持续×加速×高度归一化)")
    created_at = Column(DateTime, default=datetime.datetime.now)


class PreMarketReport(Base):
    """
    盘前简报持久化表（断链4修复）
    08:30 生成的盘前简报落库，09:26 竞价从库读取——进程重启不丢失盘前上下文。
    """
    __tablename__ = "pre_market_report"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True, comment="交易日期 YYYYMMDD")
    report = Column(Text, comment="盘前简报全文")
    created_at = Column(DateTime, default=datetime.datetime.now)


class InvestigationRecord(Base):
    """
    立案调查记录表
    存储证监会/交易所立案调查、违规处罚、监管警示等风险事件。
    每日从东方财富个股风险提示接口同步，用于源头过滤和风险预警。
    """
    __tablename__ = "investigation_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(12), nullable=False, index=True, comment="股票代码")
    name = Column(String(32), nullable=False, comment="股票名称")
    event_type = Column(String(32), nullable=False, comment="事件类型：立案调查/违规处罚/监管警示/问询函等")
    event_content = Column(Text, comment="事件详细内容")
    announce_date = Column(String(10), nullable=False, index=True, comment="公告日期 YYYYMMDD")
    detected_date = Column(String(10), nullable=False, comment="系统检测日期 YYYYMMDD")
    is_active = Column(Boolean, default=True, comment="是否仍处于调查/风险状态")
    created_at = Column(DateTime, default=datetime.datetime.now)


class SeatProfile(Base):
    """
    龙虎榜营业部画像表（席位行为画像，每日自动更新）
    记录每个上榜营业部的累计买卖统计与行为分类。
    分类优先级：人工标签(is_manual) > 自动行为分类(seat_type)。
    六一中路等名席位作为人工种子灌入，新出现的营业部靠行为统计自动定型。
    """
    __tablename__ = "seat_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seat_name = Column(String(64), nullable=False, unique=True, index=True, comment="营业部名称")
    first_seen = Column(String(10), nullable=False, comment="首次出现日期 YYYYMMDD")
    last_seen = Column(String(10), nullable=False, comment="最近出现日期 YYYYMMDD")
    appear_count = Column(Integer, default=0, comment="累计上榜次数")
    buy_amount_total = Column(Float, default=0.0, comment="累计买入总金额(元)")
    sell_amount_total = Column(Float, default=0.0, comment="累计卖出总金额(元)")
    net_amount_total = Column(Float, default=0.0, comment="累计净买入(元)")
    net_positive_days = Column(Integer, default=0, comment="净买入为正的天数")
    buy_stock_count_total = Column(Integer, default=0, comment="累计买入个股数")
    seat_type = Column(String(16), default="未知", comment="自动行为分类: 格局派/砸盘派/散户派/对倒派/未知")
    is_manual = Column(Boolean, default=False, comment="是否人工标签（人工优先于自动）")
    manual_type = Column(String(16), comment="人工标签类型（如 A类-格局派）")
    desc = Column(String(128), comment="人工标签描述")
    is_active = Column(Boolean, default=True, comment="近30天是否活跃")
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)


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


class SystemLog(Base):
    """
    系统运行日志表
    记录每日启动报告、市场风格切换、关键阈值等
    """
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    log_date = Column(String(10), nullable=False, index=True, comment="日期 YYYY-MM-DD")
    category = Column(String(32), nullable=False, index=True, comment="分类: startup/style_switch/daily_summary")
    title = Column(String(256), nullable=False, comment="标题")
    detail = Column(Text, comment="详细内容")
    created_at = Column(DateTime, default=datetime.datetime.now, index=True, comment="记录时间")


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

    def __init__(self, db_path: str = None):
        # 优先级: 1) 显式传参 > 2) 环境变量 DB_PATH > 3) settings 配置
        if db_path is None:
            db_path = os.environ.get("DB_PATH")
        if db_path is None:
            from config.settings import settings
            db_path = settings.DB_PATH
        self.db_path = db_path
        self._init_engine()

    def _init_engine(self):
        """根据 self.db_path 创建数据库引擎、会话工厂、建表并启用 WAL 模式"""
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

    def reinitialize(self, db_path: str):
        """
        切换数据库路径并重建引擎与会话工厂。
        关闭旧引擎连接后，指向新路径重新建库建表。
        用于测试环境动态切换至独立测试数据库。
        """
        import logging
        _log = logging.getLogger(__name__)
        _log.info(f"数据库引擎切换: {self.db_path} → {db_path}")
        self.engine.dispose()
        self.db_path = db_path
        self._init_engine()

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
        _ensure_column(self.engine, "holdings", "sell_price", "FLOAT DEFAULT 0.0")
        _ensure_column(self.engine, "holdings", "prev_close_price", "FLOAT DEFAULT 0.0")
        _ensure_column(self.engine, "daily_sentiment", "total_amount", "FLOAT DEFAULT 0.0")
        _ensure_column(self.engine, "recommendations", "eval_note", "TEXT")
        _ensure_column(self.engine, "recommendations", "auction_verdict", "VARCHAR(8)")
        _ensure_column(self.engine, "recommendations", "auction_premise", "VARCHAR(8)")
        _ensure_column(self.engine, "recommendations", "eval_score", "INTEGER")
        _ensure_column(self.engine, "recommendations", "auction_amount", "FLOAT")

    def get_session(self) -> Session:
        """获取数据库 Session"""
        return self.SessionLocal()