# dragon_pulse_plus AkShare 接口速查文档

本文档详细记录了 `dragon_pulse_plus` AI智能情绪周期策略系统中调用的所有 **AkShare** 行情、短线数据及新闻接口，包含接口函数名、触发时间、系统封装方法及核心字段映射。

---

## 目录
1. [接口汇总一览表](#一接口汇总一览表)
2. [接口详细说明](#二接口详细说明)
3. [多数据源降级机制](#三多数据源降级机制)
4. [接口防封与重试异常处理机制](#四接口防封与重试异常处理机制)

---

## 一、接口汇总一览表

| 序号 | 分类 | AkShare 原生接口函数 | 系统封装方法 | 触发频率/时间 |
|---|---|---|---|---|
| **1a** | 全市场行情 | `stock_zh_a_spot()` | `DataFetcher._fetch_spot_sina()` | 盘中每15秒（主源） |
| **1b** | 全市场行情 | `stock_zh_a_spot_tx()` | `DataFetcher._fetch_spot_tencent()` | 备用源（新浪失败时降级） |
| **1c** | 全市场行情 | `stock_zh_a_spot_em()` | `DataFetcher._fetch_spot_eastmoney()` | 备用源（腾讯也失败时降级） |
| **2** | 涨停池 | `stock_zt_pool_em(date)` | `DataFetcher.get_zt_pool(date_str)` | 盘中每60秒 / 15:30盘后 |
| **3** | 炸板观察池 | `stock_zt_pool_zbgc_em(date)` | `DataFetcher.get_zhaban_pool(date_str)` | 盘中每60秒 / 15:30盘后 |
| **4** | 跌停池 | `stock_zt_pool_dtgc_em(date)` | `DataFetcher.get_dt_pool(date_str)` | 15:30盘后 |
| **5** | 昨日涨停溢价 | `stock_zt_pool_previous_em(date)` | `DataFetcher.get_yesterday_zt_premium()` | 盘中每2分钟 |
| **6a** | 龙虎榜个股 | `stock_lhb_detail_em(start_date, end_date)` | `DataFetcher.get_lhb_detail(date_str)` | 15:30盘后 |
| **6b** | 龙虎榜营业部 | `stock_lhb_hyyyb_em(start_date, end_date)` | `DataFetcher.get_lhb_seats(date_str)` | 15:30盘后 |
| **7** | 板块成分股 | `stock_board_industry_cons_em(symbol)` | `DataFetcher.get_board_cons(board_name)` | 盘后中军池更新 |
| **8** | 个股资金流向 | `stock_individual_fund_flow(stock, market)` | `DataFetcher.get_individual_fund_flow(code, market)` | 盘中点火标的校验 |
| **9a** | 个股日K线(新浪) | `stock_zh_a_daily(symbol, adjust)` | `DataFetcher.get_stock_ma_prices()` | 盘中均线缓存/盘后异动计算 |
| **9b** | 个股日K线(东财) | `stock_zh_a_hist(symbol, period, adjust)` | `DataFetcher.get_stock_ma_prices()` | 备用源 / 盘后异动计算 |
| **10a** | 个股5分钟K线(新浪) | `stock_zh_a_minute(symbol, period)` | `DataFetcher._fetch_intraday_5min_sina()` | 盘中分时形态/VWAP/LLM异动分析 |
| **10b** | 个股5分钟K线(东财) | `stock_zh_a_hist_min_em(symbol, period)` | `DataFetcher._fetch_intraday_5min_eastmoney()` | 备用源 |
| **11** | 大盘指数日K | `stock_zh_index_daily(symbol)` | 直接调用 | 盘后异动偏离度/流动性基准计算 |
| **12** | 新闻快讯 | `stock_info_global_cls()` | `NewsFetcher.get_cls_news(limit)` | 08:30盘前简报 / 20:00假日汇总 |
| **13** | 热搜榜 | `stock_hot_rank_em()` | `NewsFetcher.get_hot_search_words(limit)` | 08:30盘前简报 |
| **14** | 交易日历 | `tool_trade_date_hist_sina()` | `TradeCalendarManager.sync_calendar()` | 系统启动时 / 每周刷新 |

---

## 二、接口详细说明

### 1. 全市场 A 股实时行情快照（多源降级）

系统按优先级依次尝试3个数据源获取全市场行情：

| 优先级 | 数据源 | AkShare接口 | 特点 |
|--------|--------|-------------|------|
| 1 | 新浪 | `stock_zh_a_spot()` | 速度快，成交量单位=股 |
| 2 | 腾讯 | `stock_zh_a_spot_tx()` | 备用，缺少open/high/low |
| 3 | 东财 | `stock_zh_a_spot_em()` | 字段最全，成交量单位=手(系统自动*100转为股) |

- **系统封装**: `DataFetcher.get_realtime_spot()`
- **应用场景**:
  - 09:15-09:25 竞价监控（每30秒）
  - 09:30-15:00 盘中监控（每15秒）
  - 15:30 盘后复盘
- **统一输出字段**: `code, name, price, change_pct, volume(股), amount(元), volume_ratio, turnover_rate, amplitude, open, high, low, pre_close, total_market_cap, circ_market_cap`
- **过滤**: 自动剔除科创板(688)/北交所(8xx)/ST股

### 2. 每日涨停池

- **AkShare接口**: `stock_zt_pool_em(date="YYYYMMDD")`
- **系统封装**: `DataFetcher.get_zt_pool(date_str)`
- **触发时间**: 盘中每60秒刷新缓存 + 15:30盘后深度复盘
- **用途**: 计算高度(最高连板)、宽度(涨停家数)、封单力度、炸板率(扣除回封)、板块联动检测、连板晋级率
- **关键字段**:

| 系统字段 | 说明 |
|----------|------|
| `code/name` | 股票代码/名称 |
| `lbc` | 连板天数 |
| `seal_amount` | 封板资金(元) |
| `open_count` | 炸板次数(用于区分真炸板 vs 回封) |
| `first_seal_time` | 首次封板时间 |
| `industry` | 所属行业(板块联动监控用) |
| `turnover_rate` | 换手率 |
| `amount` | 成交额 |

### 3. 每日炸板观察池

- **AkShare接口**: `stock_zt_pool_zbgc_em(date="YYYYMMDD")`
- **系统封装**: `DataFetcher.get_zhaban_pool(date_str)`
- **用途**: 计算真炸板率 = (炸板池 - 涨停池交集) / (涨停池 + 真炸板)

### 4. 每日跌停池

- **AkShare接口**: `stock_zt_pool_dtgc_em(date="YYYYMMDD")`
- **系统封装**: `DataFetcher.get_dt_pool(date_str)`
- **用途**: 计算宽度(breadth = 涨停-跌停)、恐慌判定(跌停>=10触发冰点)

### 5. 昨日涨停今日表现（溢价率）

- **AkShare接口**: `stock_zt_pool_previous_em(date="YYYYMMDD")`
- **系统封装**: `DataFetcher.get_yesterday_zt_premium()`
- **触发时间**: 盘中每2分钟刷新
- **用途**: 计算情绪核心维度"反馈(Yield)" — 昨日涨停股今日的平均涨跌幅，是赚钱效应最直接的度量
- **输出**: `opening_premium(开盘溢价), intraday_premium(即时溢价), high_open_ratio(高开>3%占比), positive_ratio(红盘率)`

### 6. 龙虎榜数据

#### 6a. 个股聚合
- **AkShare接口**: `stock_lhb_detail_em(start_date, end_date)`
- **系统封装**: `DataFetcher.get_lhb_detail(date_str)`

#### 6b. 营业部明细
- **AkShare接口**: `stock_lhb_hyyyb_em(start_date, end_date)`
- **系统封装**: `DataFetcher.get_lhb_seats(date_str)`
- **用途**: 识别游资派系(格局派A类/砸盘派B类/对倒派C类)，判断筹码稳定性

### 7. 行业板块成分股

- **AkShare接口**: `stock_board_industry_cons_em(symbol="板块名称")`
- **系统封装**: `DataFetcher.get_board_cons(board_name)`
- **用途**: 中军池筛选 — 板块内成交额Top3 & 市值Top5

### 8. 个股主力资金流向

- **AkShare接口**: `stock_individual_fund_flow(stock="600519", market="sh")`
- **系统封装**: `DataFetcher.get_individual_fund_flow(code, market)`
- **用途**: 对盘中点火标的验证主力净流入

### 9. 个股日K线（多源降级）

| 优先级 | 接口 | 特点 |
|--------|------|------|
| 1 | `stock_zh_a_daily(symbol, adjust="qfq")` | 新浪源，需要sh/sz前缀 |
| 2 | `stock_zh_a_hist(symbol, period="daily", adjust="qfq")` | 东财源 |

- **系统封装**: `DataFetcher.get_stock_ma_prices(code, lookback=30)`
- **用途**: 计算MA5/MA10/MA20均线(止损判断) + 盘后3日/10日偏离度(监管异动计算)

### 10. 个股5分钟K线（多源降级）

| 优先级 | 接口 | 特点 |
|--------|------|------|
| 1 | `stock_zh_a_minute(symbol, period="5")` | 新浪源，需要sh/sz前缀 |
| 2 | `stock_zh_a_hist_min_em(symbol, period="5")` | 东财源 |

- **系统封装**: `DataFetcher._fetch_intraday_5min(code)`
- **用途**:
  - `get_intraday_vwap(code)`: 计算分时VWAP(止损基准)
  - `get_intraday_pattern(code)`: 分时走势描述(封板时间/回封/缩量等)
  - `detect_intraday_patterns(code)`: 经典形态识别(冲高回落/尾盘偷袭/横盘突破/放量滞涨等)
  - LLM异动分析: 将5分钟OHLCV喂给LLM判断买卖

### 11. 大盘指数日K线

- **AkShare接口**: `stock_zh_index_daily(symbol="sh000001")`
- **直接调用位置**: `llm/post_market.py`, `data/fetcher.py`(流动性基准)
- **用途**:
  - 计算大盘近3日/10日涨跌幅(监管异动偏离度基准)
  - 合成20日均成交额(自适应容量因子K的基准)

### 12. 财联社全球电报

- **AkShare接口**: `stock_info_global_cls()`
- **系统封装**: `NewsFetcher.get_cls_news(limit=15)`
- **用途**: 08:30盘前简报 + 20:00假日消息汇总

### 13. 东财热搜榜

- **AkShare接口**: `stock_hot_rank_em()`
- **系统封装**: `NewsFetcher.get_hot_search_words(limit=15)`
- **用途**: 08:30盘前简报辅助判断市场关注焦点

### 14. 交易日历

- **AkShare接口**: `tool_trade_date_hist_sina()`
- **系统封装**: `TradeCalendarManager.sync_calendar()`
- **用途**: 维护±30天交易日数据库缓存，支持 `is_trading_day()` / `get_previous_trading_day()` 查询

---

## 三、多数据源降级机制

系统通过 `multi_source_fetch()` 实现数据源自动降级：

```python
df = multi_source_fetch([
    ("新浪", lambda: fetch_from_sina()),
    ("腾讯", lambda: fetch_from_tencent()),
    ("东财", lambda: fetch_from_eastmoney()),
])
```

**降级规则**: 按优先级从高到低依次尝试，某个源返回空数据或抛异常时自动切换下一个。全部失败返回空DataFrame。

**适用接口**:
- 全市场行情快照: 新浪 → 腾讯 → 东财
- 个股日K线: 新浪 → 东财
- 个股5分钟K线: 新浪 → 东财

---

## 四、接口防封与重试异常处理机制

```python
@retry_on_exception(retries=3, delay=2.0, backoff=1.5)
```

- `retries`: 最大重试次数（可配置 `FETCH_RETRY_COUNT`）
- `delay`: 首次重试延迟（可配置 `FETCH_RETRY_DELAY`）
- `backoff`: 指数退避倍数（2s → 3s → 4.5s）

**应用于**: `get_zt_pool`, `get_zhaban_pool`, `get_dt_pool`, `get_board_cons`, `get_individual_fund_flow`

---

## 五、接口调用频率汇总

| 时间段 | 调用频率 | 主要接口 |
|--------|----------|----------|
| 09:15-09:25 | 每30秒 | 全市场快照(1次) |
| 09:30-15:00 | 每15秒 | 全市场快照(1次) |
| 09:30-15:00 | 每60秒 | 涨停池(1次) + 炸板池(1次) |
| 09:30-15:00 | 每120秒 | 昨日涨停溢价(1次) |
| 09:30-15:00 | 按需 | 个股5分钟K线(买入前形态检测) + 个股资金流向(最多3只/次) |
| 15:30 | 一次性 | 涨停/炸板/跌停池 + 全市场快照 + 龙虎榜 + 指数日K + 个股日K(连板股) + 个股5分钟K(核心标的) |
| 08:30 | 一次性 | 新闻快讯 + 热搜榜 |

**日均API调用估算**: 盘中4小时 ≈ 960次快照 + 240次涨停池 + 120次溢价 + 盘后≈50次 ≈ **1400次/日**
