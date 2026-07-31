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
    LLM_TIMEOUT: int = Field(default=60, description="LLM 请求超时时间(秒)")
    LLM_MAX_RETRIES: int = Field(default=3, description="LLM 请求失败重试次数")
    LLM_BACKUP_BASE_URL: str = Field(default="", description="备用 LLM API Base URL（主模型失败时自动切换）")
    LLM_BACKUP_MODEL: str = Field(default="", description="备用 LLM 模型名称")
    LLM_BACKUP_API_KEY: str = Field(default="", description="备用 LLM API Key（为空则复用主 Key）")

    # ==================== 情绪到顶/退潮预警阈值 ====================
    EMOTION_TOP_MAX_LBC: int = Field(default=8, description="全市场连板高度触发情绪到顶预警的最低板数")
    EMOTION_TOP_ZHABAN_RATE: float = Field(default=35.0, description="全市场炸板率触发情绪到顶预警的最低百分比(%)")

    # ==================== Bark 推送配置 ====================
    BARK_TOKEN: str = Field(default="", description="Bark 推送 Device Key")
    BARK_SERVER_URL: str = Field(default="https://api.day.app", description="Bark 服务器地址，默认官方 api.day.app")
    BARK_GROUP: str = Field(default="DragonPulse", description="Bark 推送消息分组")
    BARK_SOUND: str = Field(default="minuet", description="Bark 提示音，如 minuet, glass, alarm 等")
    BARK_ENABLED: bool = Field(default=True, description="是否启用 Bark 推送")

    # ==================== 盘中监控轮询配置 ====================
    FUND_INFLOW_THRESHOLD: float = Field(default=5000.0, description="主力资金合力扫货阈值 (万元)")
    MONITOR_INTERVAL_SECONDS: int = Field(default=15, description="盘中实时快照轮询间隔(秒)")
    VOL_BURST_THRESHOLD: float = Field(default=3.0, description="点火异动成交量相比过去5日均值的倍数门槛")
    PRICE_BURST_THRESHOLD: float = Field(default=3.0, description="点火异动股价涨幅下限 (%)，低于此值不触发")
    PRICE_BURST_MAX: float = Field(default=9.5, description="点火异动股价涨幅上限 (%)，已涨停(>=9.5%)的不算点火")
    FETCH_RETRY_COUNT: int = Field(default=3, description="数据抓取重试次数")
    FETCH_RETRY_DELAY: float = Field(default=2.0, description="数据抓取重试延迟(秒)")

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

    # ==================== 策略引擎参数配置 ====================
    CORE_POOL_TOP_AMOUNT: int = Field(default=3, description="板块内选取的成交额 Top N 个股")
    CORE_POOL_TOP_MARKET_CAP: int = Field(default=5, description="板块内选取的总市值 Top N 个股")
    CORE_POOL_MIN_BETA: float = Field(default=0.8, description="中军相关性(Beta)判定阈值")
    CORE_POOL_MIN_AMOUNT: float = Field(default=20.0, description="中军日成交额门槛 (亿元)")

    SECOND_WAVE_RETREAT_MIN: float = Field(default=0.30, description="二波战法龙头回撤最小比例 (30%)")
    SECOND_WAVE_RETREAT_MAX: float = Field(default=0.50, description="二波战法龙头回撤最大比例 (50%)")
    SECOND_WAVE_LOOKBACK_DAYS: int = Field(default=30, description="二波战法追溯人气龙头的天数")

    # ==================== 动态阈值配置 ====================
    CAPACITY_K_MIN: float = Field(default=0.7, description="容量因子 K 下限（流动性枯竭）")
    CAPACITY_K_MAX: float = Field(default=1.5, description="容量因子 K 上限（流动性泛滥）")
    PREMIUM_PANIC_THRESHOLD: float = Field(default=-2.5, description="溢价崩塌阈值(%)，低于此值触发抱团避险")
    PREMIUM_WEIGHT: float = Field(default=0.40, description="溢价在盘中情绪分中的权重")
    BREADTH_WEIGHT: float = Field(default=0.25, description="宽度在盘中情绪分中的权重")
    HEIGHT_WEIGHT: float = Field(default=0.20, description="高度在盘中情绪分中的权重")
    SUPPORT_WEIGHT: float = Field(default=0.15, description="承接在盘中情绪分中的权重")


# 全局单例配置实例
settings = Settings()
