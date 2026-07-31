# AkShare API 接口文档

> 本文档记录 dragon_pulse_plus 项目中所有使用的 akshare 接口，按功能分类整理。

---

## 一、行情快照

### 1. `ak.stock_zh_a_spot_em()`

**全市场 A 股实时行情快照**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:116` → `DataFetcher.get_realtime_spot()` |
| 参数 | 无 |
| 返回字段 | 代码、名称、最新价、涨跌幅、涨跌额、成交量、成交额、振幅、最高、最低、今开、昨收、量比、换手率、市盈率-动态、市净率、总市值、流通市值 |
| 使用场景 | 盘中 15 秒轮询、盘后复盘全市场扫描、竞价快照 |

### 2. `ak.stock_zh_index_daily(symbol="sh000001")`

**上证指数日 K 线数据**

| 项目 | 说明 |
|---|---|
| 调用位置 | `llm/post_market.py:30` → `PostMarketAnalyzer._get_recent_index_pct()` |
| 参数 | `symbol="sh000001"` (上证指数) |
| 返回字段 | 日期、开盘、收盘、最高、最低、成交量 |
| 使用场景 | 计算大盘近 3 日/10 日涨跌幅，用于偏离度计算 |


## 二、涨停/炸板/跌停池

### 3. `ak.stock_zt_pool_em(date)`

**涨停池（含连板数、封板资金、炸板次数）**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:152` → `DataFetcher.get_zt_pool()` |
| 参数 | `date="YYYYMMDD"` |
| 关键字段 | 代码、名称、涨跌幅、最新价、成交额、换手率、封板资金(`seal_amount`)、首次封板时间、炸板次数(`open_count`)、连板数(`lbc`)、所属行业 |
| 使用场景 | 盘后情绪向量计算、连板梯队分析、监管异动评估、盘中炸板监控 |

### 4. `ak.stock_zt_pool_zbgc_em(date)`

**炸板观察池**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:182` → `DataFetcher.get_zhaban_pool()` |
| 参数 | `date="YYYYMMDD"` |
| 关键字段 | 代码、名称、涨跌幅、最新价、成交额、涨停价、首次封板时间、炸板次数、涨停统计、所属行业 |
| 使用场景 | 盘后炸板率计算（炸板数 / (涨停数 + 炸板数)）、烂板弱转强标的筛选 |
| 备注 | 旧版 `stock_zt_pool_zhaban_em` 已废弃，新版 zbgc 合并了涨停+炸板信息 |

### 5. `ak.stock_zt_pool_dtgc_em(date)`

**跌停池**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:208` → `DataFetcher.get_dt_pool()` |
| 参数 | `date="YYYYMMDD"` |
| 关键字段 | 代码、名称、涨跌幅、最新价、成交额、连续跌停(`dtc`)、所属行业 |
| 使用场景 | 盘后情绪向量宽度计算（涨停数 - 跌停数）、退潮期确认 |


## 三、龙虎榜

### 6. `ak.stock_lhb_detail_em(start_date, end_date)`

**龙虎榜个股明细（净买额/买入额/卖出额）**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:231` → `DataFetcher.get_lhb_detail()` |
| 参数 | `start_date="YYYYMMDD"`, `end_date="YYYYMMDD"` |
| 关键字段 | 代码、名称、解读、收盘价、涨跌幅、龙虎榜净买额、龙虎榜买入额、龙虎榜卖出额、龙虎榜成交额、换手率、流通市值、上榜原因 |
| 使用场景 | 盘后龙虎榜分析，个股级资金态度判断 |
| 备注 | 新版 akshare 返回个股级聚合数据，不含营业部名称 |

### 7. `ak.stock_lhb_hyyyb_em(start_date, end_date)`

**龙虎榜活跃营业部明细（营业部级买卖数据）**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:261` → `DataFetcher.get_lhb_seats()` |
| 参数 | `start_date="YYYYMMDD"`, `end_date="YYYYMMDD"` |
| 关键字段 | 营业部名称(`seat_name`)、上榜日、买入个股数、卖出个股数、买入总金额、卖出总金额、总买卖净额、买入股票列表 |
| 使用场景 | 盘后席位画像分析（格局派/砸盘派/量化识别），优先使用营业部级数据 |


## 四、板块与资金

### 8. `ak.stock_board_industry_cons_em(symbol)`

**同花顺/东财概念板块成分股**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:288` → `DataFetcher.get_board_cons()` |
| 参数 | `symbol="板块名称"`（如 "人工智能"、"新能源汽车"） |
| 关键字段 | 代码、名称、最新价、涨跌幅、成交额、换手率、量比、市盈率-动态 |
| 使用场景 | 动态中军池筛选（板块成交额 Top3 + 市值 Top5） |
| 备注 | 新版 akshare 不再返回「总市值」字段，中军池改用成交额单维度筛选，市值列兜底为 0 |

### 9. `ak.stock_individual_fund_flow(stock, market)`

**个股历史主力资金流向**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:315` → `DataFetcher.get_individual_fund_flow()` |
| 参数 | `stock="600519"` (个股代码), `market="sh"` 或 `"sz"` |
| 关键字段 | 日期、主力净流入-净额(`main_net_inflow`)、主力净流入-净占比、超大单净流入-净额、收盘价、涨跌幅 |
| 使用场景 | 盘中点火异动个股的主力资金验证（主力净流入 > 5000 万判定合力扫货） |
| 备注 | 新版 akshare 该接口改为按个股查询历史资金流向，不再是全市场排名 |


## 五、分时与 K 线

### 10. `ak.stock_zh_a_hist_min_em(symbol, period="5", adjust="")`

**个股 5 分钟 K 线**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:352` → `DataFetcher.get_intraday_pattern()`; `llm/sell_advisor.py:22` → `DynamicSellAdvisor._fetch_intraday()`; `llm/post_market.py:235` → 盘后复盘分时数据拉取; `api_server.py:307` → API 端点 |
| 参数 | `symbol="000001"`, `period="5"`, `adjust=""` |
| 关键字段 | 时间、开盘、收盘、最高、最低、成交量、成交额、涨跌幅 |
| 使用场景 | **核心接口**：盘中 AI 卖出建议的分时分析、盘后连板股封板质量判断（核心标的全量 OHLCV，杂毛仅摘要）、API 分时数据查询 |

### 11. `ak.stock_intraday_em(symbol)`

**个股分时逐笔成交数据**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:334` → `DataFetcher.get_intraday_vwap()` |
| 参数 | `symbol="000001"` |
| 关键字段 | 时间、价格、成交量 |
| 使用场景 | 计算个股真实分时 VWAP（均价），用于持仓破位止损判断（跌破 VWAP + 跌破 MA5 触发卖出） |

### 12. `ak.stock_zh_a_hist(symbol, period="daily", start_date="", end_date="", adjust="qfq")`

**个股历史日 K 线**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/fetcher.py:79` → `DataFetcher.get_stock_ma_prices()` |
| 参数 | `symbol="000001"`, `period="daily"`, `start_date=""` (空=全部), `end_date=""` (空=最新), `adjust="qfq"` (前复权) |
| 返回字段 | 日期、开盘、收盘、最高、最低、成交量、成交额、振幅、涨跌幅、涨跌额、换手率 |
| 使用场景 | 计算个股关键均线 MA5 / MA10 / MA20，用于破位止损判断（盘中缓存，每日仅算一次） |


## 六、新闻与舆情

### 13. `ak.stock_info_global_cls()`

**财联社电报/快讯**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/news_fetcher.py:24` → `NewsFetcher.get_cls_news()` |
| 参数 | 无 |
| 关键字段 | `title` (标题)、`content` (内容)、`pub_time` (发布时间) |
| 使用场景 | 盘前简报新闻源（取最近 15 条）、假日消息汇总（取最近 30 条），喂给 LLM 做政策/行业归因 |

### 14. `ak.stock_hot_rank_em()`

**同花顺/东财热搜个股排名**

| 项目 | 说明 |
|---|---|
| 调用位置 | `data/news_fetcher.py:48` → `NewsFetcher.get_hot_search_words()` |
| 参数 | 无 |
| 关键字段 | 代码、股票名称 |
| 使用场景 | 盘前简报热搜词源（取 Top 15），辅助 LLM 识别市场关注焦点 |


## 七、交易日历

### 15. `ak.tool_trade_date_hist_sina()`

**新浪交易日历（历史+未来）**

| 项目 | 说明 |
|---|---|
| 调用位置 | `database/services.py:513` → `TradeCalendarManager.sync_calendar()` |
| 参数 | 无 |
| 返回字段 | `trade_date` (交易日列表) |
| 使用场景 | 定时同步交易日历（±30 天窗口），非交易日自动跳过定时任务；每周强制刷新应对调休变动 |


---

## 调用关系总览

```
akshare API                              → 封装层                          → 消费方
──────────────────────────────────────────────────────────────────────────────────
stock_zh_a_spot_em()                     → DataFetcher.get_realtime_spot()  → MarketMonitor(15s轮询)
                                                                             → PostMarketAnalyzer(盘后)
                                                                             → CallAuctionAnalyzer(竞价)
stock_zt_pool_em()                       → DataFetcher.get_zt_pool()        → EmotionVector(情绪计算)
                                                                             → PostMarketAnalyzer(连板梯队)
                                                                             → MarketMonitor(炸板检测)
stock_zt_pool_zbgc_em()                  → DataFetcher.get_zhaban_pool()   → EmotionVector(炸板率)
stock_zt_pool_dtgc_em()                  → DataFetcher.get_dt_pool()       → EmotionVector(宽度)
stock_lhb_detail_em()                    → DataFetcher.get_lhb_detail()    → PostMarketAnalyzer(龙虎榜)
stock_lhb_hyyyb_em()                     → DataFetcher.get_lhb_seats()     → SeatAnalyzer(席位画像)
stock_board_industry_cons_em()           → DataFetcher.get_board_cons()    → ActiveCorePool(中军池)
stock_individual_fund_flow()             → DataFetcher.get_individual_..() → MarketMonitor(主力验证)
stock_zh_a_hist()                        → DataFetcher.get_stock_ma_..()   → HoldingMonitor(MA均线)
stock_zh_a_hist_min_em()                 → DataFetcher.get_intraday_..()   → DynamicSellAdvisor(AI卖出)
                                         → PostMarketAnalyzer(分时数据)     → api_server(API端点)
stock_intraday_em()                      → DataFetcher.get_intraday_vwap() → HoldingMonitor(VWAP)
stock_zh_index_daily()                   → PostMarketAnalyzer._get_..()    → RegulatoryYidong(偏离度)
stock_info_global_cls()                  → NewsFetcher.get_cls_news()      → PreMarketAnalyzer(盘前)
stock_hot_rank_em()                      → NewsFetcher.get_hot_search_..() → PreMarketAnalyzer(盘前)
tool_trade_date_hist_sina()              → TradeCalendarManager.sync_..()  → is_trading_day(调度)
```
