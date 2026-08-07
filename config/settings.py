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
    TAKE_PROFIT_CRITICAL_PCT: float = Field(default=20.0, description="强止盈线(%)，盈利超过此值触发CRITICAL")
    TAKE_PROFIT_HIGH_PULLBACK_PCT: float = Field(default=5.0, description="强止盈从当日最高点回落比例下限(%)——盈利≥强止盈线且从高点回落≥此值才触发CRITICAL(防打断主升浪连板)；高点数据缺失时按触发处理")

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
    MONITOR_CYCLE_BUDGET_SECONDS: int = Field(default=90, description="单轮监控周期时间预算(秒)——超过后跳过资金流/尾盘/二波/预警等非关键步骤提前收尾，防止慢源把单轮拖过看门狗120s阈值触发自动重启；须小于 WATCHDOG_STALL_SECONDS")
    SPOT_FETCH_PARALLEL: bool = Field(default=True, description="腾讯全市场行情是否并行分页抓取(串行28页~30s→并行~13s，东财限流降级时显著缓解慢源)")
    SPOT_SINA_PARALLEL: bool = Field(default=False, description="新浪全市场行情是否并行分页抓取——默认关闭：新浪反爬严格，59并发请求实测触发HTTP 456限流(并行虽~3s但易封IP)；开启有风险")
    WATCHDOG_STALL_SECONDS: int = Field(default=120, description="看门狗：主循环心跳超过此秒数未更新视为疑似卡死(数据源挂起/网络阻塞)，推送告警")
    WATCHDOG_CHECK_SECONDS: int = Field(default=20, description="看门狗检查间隔(秒)")
    WATCHDOG_AUTO_RESTART: bool = Field(default=True, description="看门狗检测卡死后自动拉起新 main.py 进程并退出当前进程")
    WATCHDOG_RESTART_COOLDOWN_MINUTES: int = Field(default=10, description="自动重启冷却：距上次自动重启不足此分钟再次卡死则停止自动重启(防循环)，交人工")
    LLM_SELL_HOLD_COOLDOWN_SECONDS: int = Field(default=1800, description="卖出 LLM 判「持有」后冷却(秒)，冷却期内不重复咨询，避免持续信号每 15s 阻塞调 LLM（默认 30 分钟）")
    LLM_SELL_COOLDOWN_BREAK_PCT: float = Field(default=3.0, description="卖出 LLM 冷却破除：冷却期内现价较 LLM 判持有时的决策价急跌≥此比例(%)则打破冷却，立即重新评估卖出（防闪崩风控盲区）")
    LLM_BUY_CONFIRM_PER_CYCLE: int = Field(default=1, description="每轮监控周期最多同步 LLM 买入确认次数；预算用尽后其余候选留待下轮评估，控制同步 LLM 对 15s 主循环的阻塞时长（审查#1）")
    MONITOR_NEAR_LIMIT_RATIO: float = Field(default=0.84, description="逼近封板区间 = 涨停线 × 比值")
    NEAR_LIMIT_VOL_RATIO: float = Field(default=5.0, description="逼近封板信号：量比下限")
    RALLY_VOL_RATIO: float = Field(default=3.0, description="低开猛拉/振幅放量信号：量比下限")
    RALLY_STRENGTH_MIN: float = Field(default=0.8, description="低开猛拉信号：拉升强度下限")
    RALLY_DENOM_MIN_RATIO: float = Field(default=0.01, description="低开猛拉拉升强度分母保护：最高最低价差低于昨收×此比例时按昨收×此比例计(防早盘微小区间把强度刷到1.0误触发虚假点火)")
    RALLY_MIN_PCT: float = Field(default=2.0, description="低开猛拉信号：现价相对开盘的拉升幅度下限(%昨收)，过滤单笔数百手拉高的假信号")
    LOW_OPEN_DEV: float = Field(default=0.98, description="低开猛拉信号：开盘价相对昨收的折扣上限（低于此视为低开）")
    AMPLITUDE_SIGNAL_MIN: float = Field(default=7.0, description="振幅放量信号：振幅下限(%)")
    AMPLITUDE_CHANGE_MIN: float = Field(default=3.0, description="振幅放量信号：涨幅下限(%)")
    TAIL_GAME_SELL_TIME: str = Field(default="09:35", description="尾盘博弈次日统一卖出时间(HH:MM)——到点按现价清仓所有AI_TAIL持仓(简化版，不再动态止盈/区分高开低开)；回测日线无分时数据，按开盘价近似")
    TAIL_GAME_WINDOW_START: str = Field(default="14:45", description="尾盘博弈买入窗口开始(HH:MM)——窗口内候选先入池")
    TAIL_GAME_WINDOW_END: str = Field(default="14:52", description="尾盘博弈买入窗口结束(HH:MM)")
    TAIL_GAME_SELECT_TIME: str = Field(default="14:51", description="尾盘博弈选时点(HH:MM)——从入池候选中统一选最优买入，留 1 分钟给 LLM+下单；窗口末(14:52)前完成")
    TAIL_GAME_CHANGE_MIN: float = Field(default=2.0, description="尾盘博弈候选：当日涨幅下限(%)——低吸强势股，不追高(指南: 2%~5%)")
    TAIL_GAME_CHANGE_MAX: float = Field(default=5.0, description="尾盘博弈候选：当日涨幅上限(%)，超过视为追高剔除")
    TAIL_GAME_VOL_RATIO_MIN: float = Field(default=1.2, description="尾盘博弈候选：量比下限(评审：低涨幅+量比≥3是放量滞涨/出货，改为温和放量1.2~2.5)")
    TAIL_GAME_VOL_RATIO_MAX: float = Field(default=2.5, description="尾盘博弈候选：量比上限——超过视为放量滞涨/抛压重，不买")
    TAIL_GAME_SHORT_UPPER_RATIO: float = Field(default=0.3, description="尾盘博弈候选：上影线占比上限((最高-收盘)/(最高-最低)≤此值，收盘卖压小)")
    TAIL_MAX_DAILY_BUYS: int = Field(default=2, description="尾盘博弈当日最大买入次数(独立预算，不吃白天 AI 的 MAX_DAILY_BUYS)")
    TAIL_MAX_POSITIONS: int = Field(default=2, description="尾盘博弈最大持仓数量(独立，不吃 AI_MAX_POSITIONS)")
    TAIL_SELECT_POOL_SIZE: int = Field(default=10, description="尾盘博弈统一选：入池后排序取前 N 条给 LLM 统一选")
    TAIL_SELECT_BACKUP: int = Field(default=2, description="尾盘博弈统一选：LLM 主选之外最多再给的备选数(主选被闸门/复核拦时按序顶)")
    TAIL_GAME_REQUIRE_MA: bool = Field(default=True, description="尾盘博弈候选：要求现价站上 5/10 日均线(指南: 上升趋势站均线)；MA 数据缺失时放行(未知即放行)")
    KLINE_ETL_WORKERS: int = Field(default=3, description="全市场日线 ETL 并行拉取线程数（仅 CLI/手动 run 用；盘后自动任务已改串行摊速，不再并发）")
    KLINE_ETL_BOOTSTRAP_DAYS: int = Field(default=30, description="盘后增量 ETL 每次拉取的历史窗口(近 N 天)到今天；断点续传跳过已完整覆盖的 code 并自动补齐历史缺失")
    KLINE_ETL_PACED_DEADLINE: str = Field(default="23:30", description="盘后日线同步最晚完成时刻(HH:MM)——自动任务从启动(约18:01)串行逐只摊速到该时刻前完成，源 QPS 最低防限流")
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
    AI_BUY_NEAR_LIMIT_FILL_LIMIT: bool = Field(default=True, description="逼近封板/高位放量买入按涨停价撮合成交(打板资金实际多撮合在涨停附近，原0.5%滑点对回测过于乐观)；false 时退回 AI_BUY_SLIPPAGE_PCT+HOT_PCT 滑点模型")
    AI_SELL_SLIPPAGE_PCT: float = Field(default=0.3, description="AI 自动卖出滑点(%)，模拟真实成交低于现价")

    # ==================== 股票过滤配置 ====================
    EXCLUDE_STAR_MARKET: bool = Field(default=True, description="是否排除科创板股票 (688开头)")
    EXCLUDE_BSE: bool = Field(default=True, description="是否排除北交所股票 (8开头/43/83/87等)")
    EXCLUDE_ST: bool = Field(default=True, description="是否排除 ST/*ST 股票")

    # ==================== 交易所监管异动红线配置 ====================
    REGULATORY_MONITOR_ENABLED: bool = Field(default=True, description="是否开启交易所监管异动计算与风险提示")
    REGULATORY_GATE_ENABLED: bool = Field(default=True, description="盘中买入监管异动闸门：今日涨停将触发交易所异动公告/停牌核查的标的禁止自动买入；数据不足不拦截(未知即放行)")
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
    THS_FETCH_PACING_SECONDS: float = Field(default=0.3, description="同花顺概念指数拉取逐只间隔(秒)——375概念防限流，调大可更稳但更慢")
    THS_STRONG_CHG_PCT: float = Field(default=5.0, description="同花顺概念指数强/弱判定：5日涨幅≥此值(%)算强(阶段升档/主线加分)，≤-此值算弱(阶段降档)")
    THS_SCORE_BONUS_WEIGHT: float = Field(default=0.1, description="同花顺概念指数趋势对主线分的加分权重(0~1)，设0关闭指数维度")

    SECOND_WAVE_RETREAT_MIN: float = Field(default=0.30, description="二波战法龙头回撤最小比例 (30%)")
    SECOND_WAVE_RETREAT_MAX: float = Field(default=0.50, description="二波战法龙头回撤最大比例 (50%)")
    SECOND_WAVE_LOOKBACK_DAYS: int = Field(default=30, description="二波战法追溯人气龙头的天数")
    SECOND_WAVE_CHANGE_MIN: float = Field(default=3.0, description="二波战法止跌信号：当日涨幅下限(%)")
    SW_MAX_DAILY_BUYS: int = Field(default=2, description="二波战法当日最大买入次数(独立预算)")
    SW_MAX_POSITIONS: int = Field(default=2, description="二波战法最大持仓数量(独立)")
    SW_LLM_PER_CYCLE: int = Field(default=1, description="二波战法每轮监控周期最多 LLM 买入确认次数")
    SW_HOLD_DAYS: int = Field(default=5, description="二波战法：N 天内未达止盈位则清仓离场")
    SW_TAKE_PROFIT_RATIO: float = Field(default=0.618, description="二波战法止盈：谷底+(前高-谷底)×此黄金分割比例(多数二波双顶在0.618~0.8反弹位，不再死守突破前高)")
    SW_TAKE_PROFIT_HIGH_RATIO: float = Field(default=0.95, description="二波战法止盈2：前高×此比例(次高点附近全清，绝不赌突破)")
    SW_REQUIRE_GROUND_BOTTOM: bool = Field(default=True, description="二波战法买入需地量止跌确认(评审：单纯回撤30-50%+单日涨3%易买在A杀半山腰/死猫跳；需前期峰值缩量+振幅收敛+不创新低)；数据不足放行")
    SW_GROUND_VOL_PEAK_RATIO: float = Field(default=0.35, description="地量判定：止跌窗口最小成交量 ≤ 近30日峰值量×此比例")
    SW_GROUND_AMPLITUDE_MAX: float = Field(default=4.0, description="地量止跌：止跌窗口单日振幅 ≤ 此值(%)视为小阴小阳收敛")
    SW_GROUND_MIN_DAYS: int = Field(default=2, description="地量止跌：止跌窗口内振幅收敛≥此天数(天)")

    # ==================== 动态阈值配置 ====================
    CAPACITY_K_MIN: float = Field(default=0.7, description="容量因子 K 下限（流动性枯竭）")
    CAPACITY_K_MAX: float = Field(default=1.5, description="容量因子 K 上限（流动性泛滥）")
    PREMIUM_PANIC_THRESHOLD: float = Field(default=-2.5, description="溢价崩塌阈值(%)，低于此值触发抱团避险")
    STYLE_HYSTERESIS_ENABLED: bool = Field(default=True, description="市场风格判定滞后缓冲：已处某风格且分数未跌破退出阈值则保持，防评分在阈值附近每15s频闪横跳")
    STYLE_ENTER_ATTACK_SCORE: float = Field(default=0.60, description="滞后：进入攻击风格(共振/打板/低吸)需分数≥此值（比基础0.55更严，防弱信号误切）")
    STYLE_ENTER_RISK_SCORE: float = Field(default=0.55, description="滞后：进入风险风格(抱团/高潮)需分数≥此值（比基础0.5更严）")
    STYLE_EXIT_SCORE: float = Field(default=0.45, description="滞后：已处某风格，分数跌破此值才退出/重新选择（低于进入阈值，滞回区间防频闪）")
    STYLE_BAOTUAN_K_MAX: float = Field(default=0.8, description="抱团风格最高优先级需 K<此值(流动性枯竭)；K≥此值时主线进攻/高潮优先，抱团降为后备（评审B6：主线爆发时杂毛跌停不再强制防守）")
    STYLE_BAOTUAN_ZT_MAX: int = Field(default=15, description="抱团风格最高优先级需 涨停家数<此值(主线活跃度低)；涨停≥此值时不强制防守")
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
