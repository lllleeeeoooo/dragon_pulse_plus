import os
from typing import Dict, Any, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    dragon_pulse_plus 全局配置类
    支持从环境变量或 .env 文件读取配置
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # ==================== 数据库配置 ====================
    DB_PATH: str = Field(default="dragon_pulse.db", description="SQLite 数据库文件路径（生产库）")
    TEST_DB_PATH: str = Field(default="dragon_pulse_test.db", description="测试用 SQLite 数据库文件路径")

    # ==================== LLM 大模型配置 ====================
    LLM_API_KEY: str = Field(default="your_llm_api_key_here", description="LLM API Key")
    LLM_BASE_URL: str = Field(default="https://api.deepseek.com/v1", description="LLM API Base URL")
    LLM_MODEL: str = Field(default="deepseek-chat", description="LLM 模型名称，例如 deepseek-chat, gpt-4o, claude-3-5-sonnet")
    LLM_TEMPERATURE: float = Field(default=0.3, description="生成随机性温度，短线量化分析建议 0.1 ~ 0.4")
    LLM_TIMEOUT: int = Field(default=180, description="LLM 请求超时时间(秒)")
    LLM_MAX_RETRIES: int = Field(default=3, description="LLM 请求失败重试次数")
    LLM_BACKUP_BASE_URL: str = Field(default="", description="备用 LLM API Base URL（主模型失败时自动切换）")
    LLM_BACKUP_MODEL: str = Field(default="", description="备用 LLM 模型名称")
    LLM_BACKUP_API_KEY: str = Field(default="", description="备用 LLM API Key（为空则复用主 Key）")

    # ==================== 情绪到顶/退潮预警阈值 ====================
    EMOTION_TOP_MAX_LBC: int = Field(default=8, description="全市场连板高度触发情绪到顶预警的最低板数")
    EMOTION_TOP_ZHABAN_RATE: float = Field(default=35.0, description="全市场炸板率触发情绪到顶预警的最低百分比(%)——顶部裂纹信号")
    EMOTION_TOP_SENTIMENT_MIN: float = Field(default=75.0, description="情绪到顶预警要求的最低情绪分（顶部时情绪仍热，而非崩溃后）")

    # ==================== 止损配置 ====================
    ABSOLUTE_STOP_LOSS_PCT: float = Field(default=-7.0, description="绝对止损线(%)，亏损超过此值无条件触发卖出")
    TIME_STOP_LOSS_DAYS: int = Field(default=3, description="时间止损天数，持仓超过N天且未盈利则触发警告")
    TAKE_PROFIT_WARN_PCT: float = Field(default=15.0, description="止盈提醒线(%)，盈利超过此值触发WARNING")
    TAKE_PROFIT_CRITICAL_PCT: float = Field(default=20.0, description="强止盈线(%)，盈利超过此值且从高点回落则触发CRITICAL")

    # ==================== Bark 推送配置 ====================
    BARK_TOKEN: str = Field(default="", description="Bark 推送 Device Key")
    BARK_SERVER_URL: str = Field(default="https://api.day.app", description="Bark 服务器地址，默认官方 api.day.app")
    BARK_GROUP: str = Field(default="DragonPulse", description="Bark 推送消息分组")
    BARK_SOUND: str = Field(default="minuet", description="Bark 提示音，如 minuet, glass, alarm 等")
    BARK_ENABLED: bool = Field(default=True, description="是否启用 Bark 推送")

    # ==================== 盘中监控轮询配置 ====================
    FUND_INFLOW_MIN: float = Field(default=2000.0, description="主力资金扫货绝对底线 (万元)，小盘股兜底阈值")
    FUND_INFLOW_CAP_RATIO: float = Field(default=0.0005, description="主力资金扫货流通市值比例，与 FUND_INFLOW_MIN 取较大值作为动态阈值")
    MONITOR_INTERVAL_SECONDS: int = Field(default=15, description="盘中实时快照轮询间隔(秒)")
    MONITOR_POOL_CACHE_SECONDS: int = Field(default=60, description="涨停/炸板池缓存刷新间隔(秒)")
    LLM_SELL_HOLD_COOLDOWN_SECONDS: int = Field(default=1800, description="卖出 LLM 判「持有」后冷却(秒)，冷却期内不重复咨询，避免持续信号每 15s 阻塞调 LLM（默认 30 分钟）")
    LLM_BUY_CONFIRM_PER_CYCLE: int = Field(default=1, description="每轮监控周期最多同步 LLM 买入确认次数；预算用尽后其余候选留待下轮评估，控制同步 LLM 对 15s 主循环的阻塞时长（审查#1）")
    MONITOR_NEAR_LIMIT_RATIO: float = Field(default=0.84, description="逼近封板区间 = 涨停线 × 比值")
    NEAR_LIMIT_VOL_RATIO: float = Field(default=5.0, description="逼近封板信号：量比下限")
    RALLY_VOL_RATIO: float = Field(default=3.0, description="低开猛拉/振幅放量信号：量比下限")
    RALLY_STRENGTH_MIN: float = Field(default=0.8, description="低开猛拉信号：拉升强度下限")
    LOW_OPEN_DEV: float = Field(default=0.98, description="低开猛拉信号：开盘价相对昨收的折扣上限（低于此视为低开）")
    AMPLITUDE_SIGNAL_MIN: float = Field(default=7.0, description="振幅放量信号：振幅下限(%)")
    AMPLITUDE_CHANGE_MIN: float = Field(default=3.0, description="振幅放量信号：涨幅下限(%)")
    ZHABAN_ALERT_CHANGE: float = Field(default=7.0, description="炸板预警：涨停池标的当前涨幅低于此值视为炸板(%)")
    ZHABAN_ALERT_VOL_RATIO: float = Field(default=2.0, description="炸板预警：量比下限")
    MA5_FALLBACK_RATIO: float = Field(default=0.97, description="MA5 获取失败时兜底为 现价×此比例")
    REC_FADE_MAX: float = Field(default=2.0, description="推荐标的高开买入：相对开盘回落超过此值(%)视为走弱，不自动买入")
    PATTERN_CHECK_MIN_AMOUNT: float = Field(default=5.0, description="分时形态检测的最低成交额(亿元)，低于此不检测（避免轮询内频繁联网）")
    PATTERN_CHECK_CACHE_SECONDS: int = Field(default=300, description="分时形态缓存 TTL(秒)，过期后重新拉取以跟上盘中走势（默认5分钟）")
    VOL_BURST_THRESHOLD: float = Field(default=3.0, description="点火异动成交量相比过去5日均值的倍数门槛")
    PRICE_BURST_THRESHOLD: float = Field(default=3.0, description="点火异动股价涨幅下限 (%)，低于此值不触发")
    PRICE_BURST_MAX: float = Field(default=9.5, description="点火异动股价涨幅上限 (%) - 主板 10cm，已涨停的不算点火")
    PRICE_BURST_MAX_20CM: float = Field(default=19.5, description="点火异动股价涨幅上限 (%) - 双创 20cm，已涨停的不算点火")
    MAIN_BOARD_LIMIT_PCT: float = Field(default=9.8, description="主板涨停线涨幅(%)，达到此值视为涨停")
    GEM_STAR_LIMIT_PCT: float = Field(default=19.8, description="创业板/科创板涨停线涨幅(%)")
    FETCH_RETRY_COUNT: int = Field(default=3, description="数据抓取重试次数")
    FETCH_RETRY_DELAY: float = Field(default=2.0, description="数据抓取重试延迟(秒)")
    SOURCE_FAIL_CIRCUIT_LIMIT: int = Field(default=3, description="数据源当日异常达此次数后熔断，当天不再调用该源（次日重置）")
    POOL_CACHE_FAIL_BACKOFF_SECONDS: int = Field(default=300, description="涨停/炸板池刷新失败后的退避重试间隔(秒)，避免对异常接口每60秒频繁轮询")

    # ==================== 仓位管理配置 ====================
    MAX_AI_POSITIONS: int = Field(default=5, description="AI自动持仓最大数量，超出不再买入")
    MAX_DAILY_BUYS: int = Field(default=3, description="AI每日最大自动买入次数")
    DAILY_LOSS_CIRCUIT_BREAKER: float = Field(default=-5.0, description="AI持仓当日平均亏损熔断阈值(%)，触发后停止买入")
    INDEX_DROP_CIRCUIT_BREAKER: float = Field(default=-2.0, description="大盘均涨幅跌破此值时触发系统级熔断，停止所有自动买入(%)")

    # ==================== AI 自动买卖模型配置 ====================
    MAX_AI_SECTOR_POSITIONS: int = Field(default=2, description="同一板块 AI 持仓数量上限（板块集中度控制）")
    AI_BUY_SLIPPAGE_PCT: float = Field(default=0.3, description="AI 自动买入滑点(%)，模拟真实成交高于快照价")
    AI_BUY_SLIPPAGE_HOT_PCT: float = Field(default=0.2, description="高位放量信号(逼近封板等)额外滑点(%)")
    AI_SELL_SLIPPAGE_PCT: float = Field(default=0.3, description="AI 自动卖出滑点(%)，模拟真实成交低于现价")

    # ==================== 股票过滤配置 ====================
    EXCLUDE_STAR_MARKET: bool = Field(default=True, description="是否排除科创板股票 (688开头)")
    EXCLUDE_BSE: bool = Field(default=True, description="是否排除北交所股票 (8开头/43/83/87等)")
    EXCLUDE_ST: bool = Field(default=True, description="是否排除 ST/*ST 股票")

    # ==================== 交易所监管异动红线配置 ====================
    REGULATORY_MONITOR_ENABLED: bool = Field(default=True, description="是否开启交易所监管异动计算与风险提示")
    MAIN_BOARD_3D_DEV_LIMIT: float = Field(default=20.0, description="主板 3 日偏离度异动红线 (%)")
    GEM_3D_DEV_LIMIT: float = Field(default=30.0, description="创业板 3 日偏离度异动红线 (%)")
    STAR_3D_DEV_LIMIT: float = Field(default=30.0, description="科创板 3 日偏离度异动红线 (%) — 科创板与创业板同为 20cm 涨停")
    REGULATORY_10D_LIMIT: float = Field(default=100.0, description="10 日严重异动累计涨幅偏离度红线 (%)")
    MAX_YIDONG_COUNT_10D: int = Field(default=4, description="10 日内触发异动次数上限 (达到4次触发严重异动)")

    # ==================== API Server 安全配置 ====================
    API_KEY: str = Field(default="", description="API Server 鉴权密钥，为空则跳过鉴权")
    API_HOST: str = Field(default="127.0.0.1", description="API 服务监听地址，默认本机回环（如需局域网访问改为 0.0.0.0）")
    BACKTEST_INITIAL_CAPITAL: float = Field(default=1000000.0, description="回测/净值计算初始资金(元)")
    INVESTIGATION_CACHE_SECONDS: int = Field(default=3600, description="立案调查黑名单缓存刷新间隔(秒)")

    # ==================== 策略引擎参数配置 ====================
    CORE_POOL_TOP_AMOUNT: int = Field(default=3, description="板块内选取的成交额 Top N 个股")
    CORE_POOL_TOP_MARKET_CAP: int = Field(default=5, description="板块内选取的总市值 Top N 个股")
    CORE_POOL_MIN_BETA: float = Field(default=0.8, description="中军相关性(Beta)判定阈值")
    CORE_POOL_MIN_AMOUNT: float = Field(default=12.0, description="中军日成交额门槛 (亿元)")

    # ==================== 板块联动监控配置 ====================
    SECTOR_LINKAGE_MIN_COUNT: int = Field(default=3, description="板块涨停家数达到此值时触发联动预警")
    SECTOR_LINKAGE_ACCEL_DELTA: int = Field(default=2, description="板块涨停家数较上轮增加此值时触发加速预警")
    SECTOR_LINKAGE_PUSH_ENABLED: bool = Field(default=True, description="板块联动预警是否推送(Bark)。关闭后仍记录日志与数据，仅不推送")

    # ==================== 概念主线识别配置（切片3） ====================
    CONCEPT_MEMBER_REFRESH_INTERVAL_DAYS: int = Field(default=1, description="概念成分股映射刷新间隔(天)。成分股变化慢，可调大省请求(新浪约175次/刷新)")
    CONCEPT_MAINLINE_SCORE_THRESHOLD: float = Field(default=0.5, description="概念主线分阈值，≥此值判为主线概念(与板块一致)")
    CONCEPT_GATE_ENABLED: bool = Field(default=True, description="盘中概念因子否决开关。关闭后概念不参与买入闸门(板块因子仍生效)。若发现过度否决可关")

    SECOND_WAVE_RETREAT_MIN: float = Field(default=0.30, description="二波战法龙头回撤最小比例 (30%)")
    SECOND_WAVE_RETREAT_MAX: float = Field(default=0.50, description="二波战法龙头回撤最大比例 (50%)")
    SECOND_WAVE_LOOKBACK_DAYS: int = Field(default=30, description="二波战法追溯人气龙头的天数")

    # ==================== 动态阈值配置 ====================
    CAPACITY_K_MIN: float = Field(default=0.7, description="容量因子 K 下限（流动性枯竭）")
    CAPACITY_K_MAX: float = Field(default=1.5, description="容量因子 K 上限（流动性泛滥）")
    PREMIUM_PANIC_THRESHOLD: float = Field(default=-2.5, description="溢价崩塌阈值(%)，低于此值触发抱团避险")
    # 盘中情绪分权重（盘后6维去掉力度/破规胆量后按比例归一化: 25/70, 20/70, 15/70, 10/70）
    PREMIUM_WEIGHT: float = Field(default=0.36, description="溢价在盘中情绪分中的权重")
    BREADTH_WEIGHT: float = Field(default=0.29, description="宽度在盘中情绪分中的权重")
    HEIGHT_WEIGHT: float = Field(default=0.21, description="高度在盘中情绪分中的权重")
    SUPPORT_WEIGHT: float = Field(default=0.14, description="承接在盘中情绪分中的权重")

    # ==================== 龙虎榜席位画像配置 ====================
    SEAT_CLASSIFY_MIN_SAMPLES: int = Field(default=3, description="席位自动分类所需最低上榜次数（样本不足判未知）")
    SEAT_AVG_BUY_THRESHOLD_WAN: float = Field(default=800.0, description="平均单只买入金额低于此(万元)视为散户/小单倾向")
    SEAT_NET_POSITIVE_ZHIGE: float = Field(default=0.6, description="格局派判定：净买入天数占比下限")
    SEAT_NET_POSITIVE_ZSHA: float = Field(default=0.3, description="砸盘派判定：净买入天数占比上限")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        weight_sum = self.PREMIUM_WEIGHT + self.BREADTH_WEIGHT + self.HEIGHT_WEIGHT + self.SUPPORT_WEIGHT
        if abs(weight_sum - 1.0) > 0.05:
            import logging
            logging.getLogger(__name__).warning(
                f"盘中情绪权重之和为 {weight_sum:.2f}，偏离1.0超过5%，请检查配置"
            )


# 全局单例配置实例
settings = Settings()
