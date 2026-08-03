# 🐉 dragon_pulse_plus：AI 智能短线情绪周期量化策略与实盘推送系统

`dragon_pulse_plus` 是一款基于 **A 股情绪周期理论** 的 AI 智能量化策略应用。系统深度整合 **AkShare** 行情数据源、**SQLite** 本地数据库持久化、**LLM 大模型决策引擎** (DeepSeek / OpenAI / Claude) 及 **Bark** 手机实时推送，打造了一个从“盘前简报 ➔ 竞价撮合确认 ➔ 盘中 15 秒点火与 AI 自动持仓监控 ➔ 盘后深度复盘”的交易日全流程自动化决策闭环。

---

## 目录
1. [系统核心架构与设计哲学](#一系统核心架构与设计哲学)
2. [情绪周期与五大核心战法](#二情绪周期与五大核心战法)
3. [交易日全流程自动化流转线](#三交易日全流程自动化流转线)
4. [交易所监管异动红线与“破规胆量”计算](#四交易所监管异动红线与破规胆量计算)
5. [五大卖出风控触发场景](#五五大卖出风控触发场景)
6. [持仓管理体系与 AI 自动交易规则](#六持仓管理体系与-ai-自动交易规则)
7. [数据库结构设计 (SQLite)](#七数据库结构设计-sqlite)
8. [GET HTTP 持仓管理 API 接口](#八get-http-持仓管理-api-接口)
9. [全局配置说明 (.env & settings.py)](#九全局配置说明-env--settingspy)
10. [快速开始与部署指南](#十快速开始与部署指南)
11. [项目目录结构](#十一项目目录结构)

---

## 一、系统核心架构与设计哲学

系统遵循 **“三层过滤器”** 架构，将量化计算的硬核指标与大模型的逻辑神韵完美结合：

```text
                              ┌────────────────────────┐
                              │  AkShare & 新闻数据源  │
                              └───────────┬────────────┘
                                          │ 实时/定时抓取
                                          ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ 1. 底层：数据与状态管理层 (Data Layer)                                               │
│  - 源头过滤科创板/北交所/ST 股票                                                    │
│  - 全市场快照 / 涨跌停池 / 连板梯队 / 龙虎榜席位 / 主力资金流向                      │
└─────────────────────────┬─────────────────────────────────────────────────────────┘
                          │ 标准化 Pandas DataFrame / SQLite 状态持久化
                          ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ 2. 中层：量化逻辑与策略引擎层 (Logic Layer)                                           │
│  - 6D 情绪多维向量评分 (高度/宽度/反馈/力度/承接/破规胆量)                            │
│  - 动态中军池维护 (成交额 Top3 & 市值 Top5 & Beta 相关性 > 0.8)                     │
│  - 战法标签化归因 (低吸/打板/二波/抱团/共振)                                        │
│  - 龙虎榜席位派系画像 (格局派/砸盘派/量化派)                                        │
│  - 交易所 3 日/10 日监管异动偏离度与残余空间计算                                   │
└─────────────────────────┬─────────────────────────────────────────────────────────┘
                          │ 结构化 Json / 异动标签 / 监管警告
                          ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ 3. 顶层：LLM 智能决策引擎 (Decision Layer)                                           │
│  - 可配置 Prompt 模板工程 (复盘/简报/竞价/风控)                                     │
│  - 深度归因、胜率复盘、周期定性、次日详细操作指导 (开盘幅度/竞价量能/买卖条件)      │
└─────────────────────────┬─────────────────────────────────────────────────────────┘
                          │ 润色与结构化推送文案
                          ▼
                              ┌────────────────────────┐
                              │   Bark 消息推送系统    │
                              └────────────────────────┘
```

---

## 二、情绪周期与五大核心战法

### 1. 情绪周期五大阶段划分
- **冰点期**：连板高度降至极低、涨停数极少、跌停增加、赚钱效应极差。
- **启动期**：冰点后出现第一个放量共振主线，首板/1进2梯队形成。
- **发酵期**：赚钱效应扩散，板块龙一龙头连板，中位股跟风强劲。
- **高潮期**：一致性极强，连板高度高，缩量一字板变多，分歧风险积聚。
- **退潮期**：高位龙头炸板/跌停，杀中位股，赚钱效应快速恶化。

### 2. 五大核心战法量化逻辑

| 战法名称 | 适用情绪阶段 | 核心量化逻辑与筛选规则 |
|---|---|---|
| **低吸战法** | 题材中期波动 | 必须是 **动态中军池成员** (成交>12亿，Beta>0.8)，缩量回踩 10日/20日均线，分时止跌时低吸。 |
| **打板战法** | 启动期与发酵期 | 启动期打**首板与1进2**抢先手；发酵期打**板块龙一、龙二**拿次日高溢价。 |
| **二波战法** | 退潮后的修复期 | **只做近30天内的人气总龙头** ($\ge 4$ 连板)，股价从最高点**回撤 30%~50%**，换手 5-10 天后止跌出现反包大阳线。 |
| **抱团战法** | 存量博弈/缩量阴跌 | 全市场成交额极度萎缩 ($<7000$ 亿)，指数阴跌，资金避险躲入特立独行、流动性极佳的红利或超级妖股。 |
| **共振战法** | 冰点期放量大阳 | 指数放量大阳起跳当天，新题材与指数**齐飞**，抢先封板且带板块效应的“共振龙”。 |

---

## 三、交易日全流程自动化流转线

```text
[08:30 盘前简报] ➔ [09:15-09:25 竞价量价监控] ➔ [09:26 竞价指令] ➔ [09:30-15:00 15秒轮询&AI实盘] ➔ [15:30 盘后深度复盘]
```

### 1. 08:30 盘前简报 (Pre-Market)
- 抓取隔夜及早间财联社快讯与同花顺/东财热搜词。
- 调用 LLM 评估政策级别（国家级 vs 地方级/企业级），提取逻辑关键词。
- 预测今日最可能爆发的 1-3 个热点板块与潜在受益标的，通过 Bark 推送。

### 2. 09:15-09:25 竞价量价监控 (Auction Monitor)
- 每 30 秒采集全市场快照，追踪竞价量能趋势（放量/缩量/平稳）。
- 监控高开>5%家数变化、全市场均涨幅趋势、竞价龙头标的。
- 09:25 自动推送竞价预判摘要（量能趋势+情绪预判+龙头Top5）。

### 3. 09:26 竞价观察 (Call Auction)
- 获取 09:25 全市场集合竞价撮合快照。
- 自动从数据库提取上一交易日复盘推荐的标的，计算**竞价开盘涨幅**与**竞价成交额占昨日全天的百分比**。
- 验证推荐标的是否满足 `open_requirement` 条件（如 "+3%~+6%"）。
- LLM 评估今日竞价风格，生成【竞价直接挂单买入】/【开盘观察分时再定】/【放弃介入】指令。

### 4. 09:30-15:00 盘中 15 秒实时轮询与 AI 自动持仓 (Market Monitor)
- **15 秒全市场快照监控**：
  - **点火预警**（4种信号）：量比放量、逼近封板、低开猛拉、振幅放量。
  - **板块联动监控**：实时追踪涨停池按行业分组变化，板块加速时推送预警。
  - **连板晋级率**：每日计算昨日连板股今日晋级比例，反映接力赚钱效应。
  - **封单衰减监控**：跟踪涨停股封单金额变化，衰减>70%时预警。
  - **炸板预警**：涨停池中标的回落至<7%时发出分歧预警。
- **AI 自动买入（7层过滤）**：
  1. 情绪周期允许？（冰点/退潮禁止买入）
  2. 大盘未熔断？（均涨幅>-2%）
  3. 仓位未满？（<=5只）
  4. 日买入未超限？（<=3次）
  5. 持仓亏损未熔断？（均亏<-5%）
  6. 分时形态正常？（排除冲高回落/放量滞涨）
  7. 推荐标的条件满足？（验证open_requirement）
- **持仓风控与自动卖出（6种规则）**：
  - 绝对止损：亏损>=-7%无条件卖出
  - 断板必卖：炸板+跌破VWAP（允许正常二封）
  - 破位止损：打板/接力股跌破VWAP；低吸股跌破MA5
  - 情绪到顶：连板>=8板且炸板率>35%
  - 时间止损：持仓>3天仍亏损
  - 逢高止盈：盈利>=15%提醒，>=20%强止盈

### 5. 15:30 盘后深度复盘 (Post-Market)
- 统计当日涨跌停数、炸板率（扣除回封）、最高连板梯队分布及成交额 Top 20。
- 计算 6D 情绪多维向量（含真实"破规胆量"维度）。
- **情绪周期状态机判定**：基于前一交易日周期阶段 + 今日数据，严格按转换规则推进（冰点→启动→发酵→高潮→退潮→冰点）。
- 自动提取上一交易日推荐标的进行**胜率复盘**。
- 自动计算今日**动态中军池**与高位龙头**交易所监管异动残余空间**。
- 龙虎榜游资席位画像分析（格局派/砸盘派/量化派）。
- LLM 生成次日详细操作指南并保存推荐标的至数据库。
- 周期转换时自动推送 Bark 通知（如"发酵→高潮，建议逢高减仓"）。

---

## 四、交易所监管异动红线与“破规胆量”计算

针对交易所“股票交易异常波动”监管规则，系统内置了定量计算与资金博弈评估引擎：

### 1. 监管红线计算公式
- **3 日涨跌幅偏离度**: $\text{Dev}_{3d} = \text{近3日股票累计涨幅} - \text{近3日指数涨幅}$
  - 主板触线红线：$\ge 20\%$
  - 创业板触线红线：$\ge 30\%$
- **10 日严重异动偏离度**: $\text{Dev}_{10d} = \text{近10日股票累计涨幅} - \text{近10日指数涨幅}$
  - 严重异动红线：$\ge 100\%$ 或 10日内触发 4 次常规异动。

### 2. 残余异动空间计算
- 系统每日盘后自动计算龙头股的**残余 3日异动空间 (%)** 与 **残余 10日严重异动空间 (%)**。若空间 $<6\%$，Prompt 强制约束 LLM 提示“注意主力控异动砸盘，不宜追高打板”。

### 3. 资金“破规胆量”维度 (`YidongBravery`)
- 评估龙头股触及监管异动红线后，次日资金**是否敢于强行弱转强/顶板**。
- 若资金有胆量顶板：定性为超级牛市主升/高潮期，放宽仓位；
- 若资金无胆量抢跑砸盘：定性为脆弱退潮期，严控仓位防守。

---

## 五、卖出风控触发场景

系统提供 6 种量化卖出风控规则，覆盖绝对风险、标的盘口与全市场环境：

1. 🛑 **规则 0：绝对止损**（`CRITICAL` 级）
   - 亏损达到 $-7\%$（可配置），无条件立即止损离场。短线第一铁律。
2. 💥 **规则 1：龙头断板必卖**（`CRITICAL` 级）
   - 标的今日曾封涨停，但炸板后**价格跌破分时均线(VWAP)**。排除正常"二封"过程的干扰。
3. 📉 **规则 2：破位止损（策略分级）**（`HIGH` 级）
   - **打板/接力策略**：跌破分时均价线(VWAP)即触发（短线不容忍水下）
   - **低吸/中军策略**：跌破5日均线(MA5)才触发（VWAP日内波动大）
4. 🚨 **规则 3：全市场情绪到顶预警**（`WARNING` 级）
   - 全市场最高连板 $\ge 8$ 板且炸板率 $> 35\%$+情绪分<40，预示退潮。
5. ⏰ **规则 4：时间止损**（`WARNING` 级）
   - 持仓超过 3 天仍未盈利，短线资金效率低下，建议换股。
6. 💰 **规则 5：逢高止盈**（`WARNING`/`HIGH` 级）
   - 盈利 $\ge 15\%$：WARNING 提醒设置移动止盈
   - 盈利 $\ge 20\%$：HIGH 强止盈，建议锁定利润

---

## 六、持仓管理体系与 AI 自动交易规则

持仓表划分两种持仓类型：

```text
                     ┌────────────────────────────────┐
                     │    holdings 统一持仓数据库     │
                     └───────────────┬────────────────┘
                                     │
             ┌───────────────────────┴───────────────────────┐
             ▼                                               ▼
   【MANUAL 手动持仓】                             【AI_AUTO 自动持仓】
 - 通过 HTTP API 请求录入                        - 复盘推荐标的盘中触发点火异动
 - 成本价与战法手动指定                         - 自动排除”一字板”(买不进)
 - 触发卖出信号发送 Bark 提示                   - 自动录入模拟成本价与收益率 (%)
                                                 - 触发离场信号系统自动平仓 (CLOSED)
```

### AI 自动交易风控约束

| 约束项 | 默认值 | 说明 |
|--------|--------|------|
| 最大持仓数 | 5只 | 超出不再买入 (`MAX_AI_POSITIONS`) |
| 单日最大买入 | 3次 | 超出不再买入 (`MAX_DAILY_BUYS`) |
| 持仓亏损熔断 | -5% | 全部AI持仓平均亏损超限停止买入 (`DAILY_LOSS_CIRCUIT_BREAKER`) |
| 大盘熔断 | -2% | 全市场均涨幅跌破此值停止买入 (`INDEX_DROP_CIRCUIT_BREAKER`) |
| 情绪周期约束 | — | 冰点/退潮期禁止买入，启动期仅买推荐标的 |
| 分时形态过滤 | — | 冲高回落/放量滞涨的标的不买入 |
| 推荐条件验证 | — | 推荐标的必须满足open_requirement条件 |
| 推送频率限制 | 8条/分钟 | 极端行情防刷屏 |

- **实时收益率自动计算**:
  $$\text{profit\_rate} = \frac{\text{最新实时价} - \text{买入成本价}}{\text{成本价}} \times 100\%$$

---

## 七、数据库结构设计 (SQLite)

系统使用本地 `dragon_pulse.db` 存储 14 张核心数据表：

### 1. `holdings`（持仓管理表）
- `id`: 主键
- `code` / `name`: 股票代码 / 名称 (自动匹配)
- `cost_price` / `current_price` / `profit_rate`: 成本价 / 最新价 / 实时收益率 (%)
- `quantity`: 持仓股数 (默认 100)
- `buy_date` / `buy_strategy`: 买入日期 / 战法标签
- `holding_type`: `MANUAL` (手动持仓) / `AI_AUTO` (AI 自动持仓)
- `was_limit_up_today`: 今日是否曾封涨停 (Boolean)
- `status`: `HOLDING` (持仓中) / `CLOSED` (已平仓)

### 2. `recommendations`（复盘与竞价推荐标的表）
- `trade_date`: 交易日期 YYYYMMDD
- `code` / `name`: 推荐股票代码 / 名称
- `strategy_type`: 五大战法类型
- `open_requirement`: 开盘要求 (如 高开 +3%~+6%)
- `auction_vol_ratio`: 竞价量能比例要求 (如 10%+)
- `buy_condition` / `sell_condition`: 详细买卖条件
- `status`: `PENDING` (待观察) / `TRIGGERED` / `EXPIRED`

### 3. `daily_sentiment`（每日情绪向量表）
- `trade_date`: 交易日期 YYYYMMDD (唯一)
- `height` / `breadth` / `zt_count` / `dt_count` / `zhaban_count`
- `yield_rate` / `seal_force_ratio` / `zhaban_rate` / `yidong_bravery`
- `sentiment_index`: 综合情绪分值 (0-100)
- `cycle_stage`: 情绪周期定性
- `summary`: 深度总结摘要

### 4. `historic_dragons`（历史人气龙头表）
- `code` / `name`: 龙头代码 / 名称
- `max_lbc`: 第一波最高连板数 ($\ge 4$ 板)
- `peak_date` / `peak_price`: 第一波见顶日期 / 价格 (用于计算 30%-50% 二波回撤)
- `is_active`: 是否处于二波观察期 (30天内)

### 5. `market_index`（大盘指数日线表）
- `trade_date`: 交易日期 (唯一)
- `sh_close` / `sh_change_pct`: 上证指数收盘价 / 涨跌幅
- `sz_close` / `sz_change_pct`: 深证成指收盘价 / 涨跌幅
- `gem_close` / `gem_change_pct`: 创业板指收盘价 / 涨跌幅
- `total_amount`: 全市场成交额(亿元)

### 6. `daily_equity_snapshot`（每日净值快照表）
- `trade_date`: 交易日期 (唯一)
- `total_equity` / `position_value` / `available_cash`: 总权益 / 持仓市值 / 可用资金
- `unrealized_pnl` / `today_realized_pnl` / `total_realized_pnl`: 浮动盈亏 / 今日已实现 / 累计已实现
- `today_pnl_pct` / `cumulative_pnl_pct`: 今日收益率 / 累计收益率
- `sh_change_pct`: 当日上证涨跌幅（对比基准）

### 7. `daily_zt_pool`（每日涨停池明细表）
- `trade_date` / `code` / `name`: 交易日期 / 股票代码 / 名称
- `lbc`: 连板数
- `seal_amount` / `first_seal_time` / `open_count`: 封单金额 / 首封时间 / 炸板次数
- `industry`: 所属行业
- `amount` / `turnover_rate` / `circ_market_cap`: 成交额 / 换手率 / 流通市值

### 8. `sector_strength`（每日板块强度表）
- `trade_date` / `sector_name`: 交易日期 / 板块名称
- `zt_count` / `prev_zt_count` / `acceleration`: 涨停数 / 上日数 / 加速值
- `top_stocks`: 领涨标的

### 9-13. 日志与辅助表
- `push_logs`: Bark 推送通知日志
- `llm_logs`: LLM 调用全记录（prompt/response/tokens）
- `error_logs`: 系统错误/警告日志
- `system_logs`: 系统运行日志
- `trade_calendar`: 交易日历缓存

### 14. `investigation_records`（立案调查/违规处罚记录表）
- `code` / `name`: 股票代码 / 名称
- `event_type`: 事件类型（立案调查/违规处罚/监管警示/问询函/通报批评/纪律处分）
- `event_content`: 事件详细内容（公告原文）
- `announce_date` / `detected_date`: 公告日期 / 系统检测日期
- `is_active`: 是否仍处于风险状态

---

## 八、Web 服务地址

系统启动后，以下地址可直接访问：

| 页面 | 地址 | 说明 |
|---|---|---|
| **系统综合看板** | `http://127.0.0.1:8000/monitor` | 大盘指数/市场风格/情绪分/持仓明细/板块轮动/涨停龙头/净值曲线/定时任务 |
| **API 交互文档** | `http://127.0.0.1:8000/docs` | FastAPI Swagger UI，所有接口可直接在线调试 |
| **情绪看板 JSON** | `http://127.0.0.1:8000/dashboard` | 结构化 JSON 数据，供程序调用 |
| **API 文档 (ReDoc)** | `http://127.0.0.1:8000/redoc` | 备选 API 文档样式 |

### HTTP 持仓管理 API 接口

### 1. 查看持仓列表 `/holdings`
- **URL**: `http://127.0.0.1:8000/holdings?type=MANUAL`
- **参数**: `type` 可选 `MANUAL` / `AI_AUTO`，不填返回全部。

### 2. 添加新持仓 `/holdings/add` (名称自动匹配)
- **URL**: `http://127.0.0.1:8000/holdings/add?code=600519&price=1750&strategy=中军低吸`
- **特点**: **无需输入股票名称**，系统根据代码自动拉取全市场快照匹配。
- **响应 JSON**:
  ```json
  {
    "code": 200,
    "msg": "成功添加持仓 贵州茅台(600519)",
    "data": { "code": "600519", "name": "贵州茅台", "price": 1750.0, "strategy": "中军低吸" }
  }
  ```

### 3. 平仓/卖出指定股票 `/holdings/close`
- **URL**: `http://127.0.0.1:8000/holdings/close?code=600519`

---

## 九、全局配置说明 (.env & settings.py)

复制 `.env.example` 重命名为 `.env` 即可配置：

```ini
# ==================== 1. LLM 大模型配置 ====================
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.3
LLM_TIMEOUT=180
LLM_MAX_RETRIES=3
LLM_BACKUP_BASE_URL=                 # 备用LLM (主模型全部失败时自动切换)
LLM_BACKUP_MODEL=

# ==================== 2. Bark 消息推送配置 ====================
BARK_TOKEN=your_bark_device_key
BARK_SERVER_URL=https://api.day.app
BARK_GROUP=DragonPulse
BARK_SOUND=minuet
BARK_ENABLED=true

# ==================== 3. 盘中 15 秒实时轮询配置 ====================
MONITOR_INTERVAL_SECONDS=15
VOL_BURST_THRESHOLD=3.0
PRICE_BURST_THRESHOLD=3.0

# ==================== 4. 仓位管理与风控 ====================
MAX_AI_POSITIONS=5                   # AI最大持仓数
MAX_DAILY_BUYS=3                     # 每日最大买入次数
DAILY_LOSS_CIRCUIT_BREAKER=-5.0      # 持仓亏损熔断线(%)
INDEX_DROP_CIRCUIT_BREAKER=-2.0      # 大盘熔断线(%)
ABSOLUTE_STOP_LOSS_PCT=-7.0          # 绝对止损线(%)
TIME_STOP_LOSS_DAYS=3                # 时间止损天数
TAKE_PROFIT_WARN_PCT=15.0            # 止盈提醒线(%)
TAKE_PROFIT_CRITICAL_PCT=20.0        # 强止盈线(%)

# ==================== 5. 股票过滤配置 (源头剔除) ====================
EXCLUDE_STAR_MARKET=true   # 排除科创板 (688)
EXCLUDE_BSE=true           # 排除北交所 (8/43/83/87/920)
EXCLUDE_ST=true            # 排除 ST/*ST 股

# ==================== 6. 交易所监管异动与胆量风控 ====================
REGULATORY_MONITOR_ENABLED=true
MAIN_BOARD_3D_DEV_LIMIT=20.0
GEM_3D_DEV_LIMIT=30.0
REGULATORY_10D_LIMIT=100.0

# ==================== 7. 策略引擎参数 ====================
CORE_POOL_MIN_AMOUNT=12.0            # 中军池成交额门槛(亿)
SECOND_WAVE_RETREAT_MIN=0.30         # 二波回撤最小
SECOND_WAVE_RETREAT_MAX=0.50         # 二波回撤最大
SECTOR_LINKAGE_MIN_COUNT=3           # 板块联动涨停下限
SECTOR_LINKAGE_ACCEL_DELTA=2         # 板块联动加速增量

# ==================== 8. 点火异动（按板块区分涨停线） ====================
PRICE_BURST_MAX=9.5                  # 主板10cm涨停线
PRICE_BURST_MAX_20CM=19.5            # 双创20cm涨停线
FUND_INFLOW_MIN=2000.0               # 主力资金绝对底线(万元)
FUND_INFLOW_CAP_RATIO=0.0005         # 主力资金流通市值比例

# ==================== 9. 盘中情绪分权重 ====================
PREMIUM_WEIGHT=0.36                  # 溢价权重
BREADTH_WEIGHT=0.29                  # 宽度权重
HEIGHT_WEIGHT=0.21                   # 高度权重
SUPPORT_WEIGHT=0.14                  # 承接权重
```

---

## 十、快速开始与部署指南

### 1. 克隆代码与安装依赖
```bash
# 激活 Python 环境变量
pip install -r requirements.txt
```

### 2. 配置密钥环境
```bash
# 复制配置文件模板
cp .env.example .env
# 编辑 .env 文件，填入 LLM_API_KEY 和 BARK_TOKEN
```

### 3. 运行单元测试
```bash
python -m pytest tests/ -v
```

### 4. 启动系统
```bash
python main.py
```
启动后系统将：
- 自动在后台启动 Web 服务，浏览器访问：
  - **实时风控看板**：`http://127.0.0.1:8000/monitor`
  - **API 交互文档**：`http://127.0.0.1:8000/docs`
- 启动 APScheduler 并在 **08:30**、**09:26**、**15:30** 定时执行简报、竞价与复盘；
- 在交易时间内以 **15 秒** 间隔进行全市场快照点火轮询与持仓卖出监控！

---

## 十一、项目目录结构

```text
dragon_pulse_plus/
├── README.md                # 本超详细系统说明文档
├── SYSTEM_PLAN.md           # 开发与测试计划文档
├── AKSHARE_APIS.md          # AkShare 接口封装速查文档
├── API_SERVER_DOCS.md       # GET HTTP 持仓 API 文档
├── .env.example             # 全局环境变量配置模板
├── requirements.txt         # 项目 Python 依赖列表
├── main.py                  # 系统主流程统一启动入口
├── api_server.py            # FastAPI 持仓管理 HTTP Web API
├── dragon_pulse.db          # SQLite 策略持久化数据库
├── config/                  # 配置文件与 Prompt 模板
│   ├── settings.py          # pydantic 全局配置对象
│   └── prompt_templates.py  # 深度复盘/简报/竞价/异动 Prompt 模板
├── core/                    # 量化策略与风控引擎
│   ├── emotion_index.py     # 6D 情绪多维向量计算 (含破规胆量)
│   ├── cycle_machine.py     # 情绪周期状态机 (冰点→启动→发酵→高潮→退潮)
│   ├── core_pool.py         # 动态中军池与相关性 Beta 筛选
│   ├── strategies.py        # 市场风格分类 & 五大战法标签化归因引擎
│   ├── seat_analyzer.py     # 龙虎榜游资席位画像库
│   ├── holding_monitor.py   # 持仓风控卖出条件检测 (6规则)
│   ├── regulatory_yidong.py # 交易所监管异动红线计算
│   └── trade_calendar.py    # A股交易日历工具 (交易日判断/前一交易日查询)
├── data/                    # 数据采集与清洗（mixin模式拆分）
│   ├── core.py              # 重试装饰器、多源降级工具函数
│   ├── fetcher.py           # DataFetcher 主类（22行壳 + 3个mixin）
│   ├── fetcher_spot.py      # 实时行情 + 溢价 + 流动性基线
│   ├── fetcher_pool.py      # 涨跌停池 + 龙虎榜 + 板块 + 资金流向
│   ├── fetcher_history.py   # 历史K线 + 分时数据 + 形态检测
│   └── news_fetcher.py      # 财联社新闻电报与同花顺热搜榜抓取
├── database/                # SQLite 数据库模型与 ORM 服务（按领域拆分）
│   ├── models.py            # SQLAlchemy 13 张核心表模型定义
│   ├── connection.py        # DatabaseManager 单例
│   ├── holdings.py          # 持仓管理服务
│   ├── recommendations.py   # 推荐标的服务
│   ├── sentiment.py         # 情绪向量服务
│   ├── market_data.py       # 指数/净值/涨停池/板块强度服务
│   ├── calendar.py          # 交易日历服务
│   ├── logs.py              # 推送/LLM/错误日志服务
│   └── ...
├── llm/                     # LLM 大模型决策引擎
│   ├── client.py            # 统一 OpenAI/DeepSeek/Claude 客户端
│   ├── post_market.py       # 盘后深度复盘生成器
│   ├── pre_market.py        # 08:30 盘前简报生成器
│   ├── call_auction.py      # 09:26 竞价观察与指令生成器
│   └── sell_advisor.py      # 盘中异动与卖出提示润色器
├── notifier/                # 消息推送模块
│   └── bark.py              # Bark 消息推送封装
├── scheduler/               # 时间线调度与盘中轮询（按功能拆分）
│   ├── market_monitor.py    # 盘中 15s 轮询监控引擎（38行壳 + 4个mixin）
│   ├── pre_market.py        # 08:30 盘前简报任务
│   ├── auction.py           # 09:26 竞价观察任务
│   ├── post_market.py       # 15:30 盘后复盘任务
│   ├── reporting.py         # 每日盈亏报告推送
│   ├── holiday.py           # 假日消息汇总
│   └── helpers.py           # 推荐解析、龙头填充等辅助函数
├── dashboard/               # 系统看板模块
│   ├── data.py              # 看板数据聚合层
│   └── templates.py         # 看板 HTML 渲染（模块化板块函数）
└── tests/                   # 单元测试集 (43个)
    ├── test_core.py         # 核心量化算法
    ├── test_database.py     # 数据库 CRUD
    ├── test_config.py       # 配置校验 + 阈值逻辑
    ├── test_monitor.py      # 持仓监控卖出信号全覆盖
    └── test_strategies.py   # 战法标签 + 板块区分
```
