# dragon_pulse_plus AkShare 接口速查文档

本文档详细记录了 `dragon_pulse_plus` AI智能情绪周期策略系统中调用的所有 **AkShare** 行情、短线数据及新闻接口，包含接口函数名、触发时间、系统封装方法及核心字段映射。

---

## 目录
1. [接口汇总一览表](#一接口汇总一览表)
2. [接口详细说明](#二接口详细说明)
   - [1. 全市场 A 股实时行情快照](#1-全市场-a-股实时行情快照)
   - [2. 每日涨停池](#2-每日涨停池)
   - [3. 每日炸板池](#3-每日炸板池)
   - [4. 每日跌停池](#4-每日跌停池)
   - [5. 龙虎榜营业部明细](#5-龙虎榜营业部明细)
   - [6. 概念/行业板块成分股](#6-概念行业板块成分股)
   - [7. 主力资金流向排名](#7-主力资金流向排名)
   - [8. 财联社全球电报/快讯](#8-财联社全球电报快讯)
   - [9. 同花顺/东财热搜榜](#9-同花顺东财热搜榜)
3. [接口防封与重试异常处理机制](#三接口防封与重试异常处理机制)

---

## 一、接口汇总一览表

| 序号 | 分类 | AkShare 原生接口函数 | 系统封装方法 | 触发频率/时间 |
|---|---|---|---|---|
| **1** | **全市场行情** | `stock_zh_a_spot_em()` | `DataFetcher.get_realtime_spot()` | 盘中每15秒 / 09:26 竞价 / 15:30 复盘 |
| **2** | **涨停池** | `stock_zt_pool_em(date)` | `DataFetcher.get_zt_pool(date_str)` | 15:30 盘后深度复盘 |
| **3** | **炸板观察池** | `stock_zt_pool_zbgc_em(date)` | `DataFetcher.get_zhaban_pool(date_str)` | 15:30 盘后深度复盘 |
| **4** | **跌停池** | `stock_zt_pool_dtgc_em(date)` | `DataFetcher.get_dt_pool(date_str)` | 15:30 盘后深度复盘 |
| **5** | **龙虎榜** | `stock_lhb_detail_em(start_date, end_date)` + `stock_lhb_hyyyb_em(start_date, end_date)` | `DataFetcher.get_lhb_detail()` + `get_lhb_seats()` | 15:30 盘后深度复盘 |
| **5b** | **龙虎榜营业部** | `stock_lhb_hyyyb_em(start_date, end_date)` | `DataFetcher.get_lhb_seats(date_str)` | 15:30 营业部席位画像 |
| **6** | **板块成分股** | `stock_board_industry_cons_em(symbol)` | `DataFetcher.get_board_cons(board_name)` | 盘前中军池更新 & 板块归因 |
| **7** | **资金流向** | `stock_individual_fund_flow(stock_code, market)` | `DataFetcher.get_individual_fund_flow(code, market)` | 盘中点火标的资金校验 |
| **8** | **新闻快讯** | `stock_info_global_cls()` | `NewsFetcher.get_cls_news(limit)` | 08:30 盘前简报 |
| **9** | **热搜榜单** | `stock_hot_rank_em()` | `NewsFetcher.get_hot_search_words(limit)` | 08:30 盘前简报 |

---

## 二、接口详细说明

### 1. 全市场 A 股实时行情快照
- **AkShare 接口**: `ak.stock_zh_a_spot_em()`
- **系统封装方法**: `DataFetcher.get_realtime_spot()`
- **应用场景**:
  - 09:26 集合竞价撮合数据提取
  - 盘中每 15 秒轮询，扫描全市场量比爆发（`volume_ratio >= 3.0`）且涨幅（`change_pct >= 3.0`）的点火股票
  - 15:30 盘后提取全市场成交额 Top 20 标的
- **字段映射表**:

| AkShare 原始字段 | 系统映射字段 | 数据类型 | 说明 |
|---|---|---|---|
| 代码 | `code` | str | 股票代码（如 `000001`） |
| 名称 | `name` | str | 股票名称 |
| 最新价 | `price` | float | 实时最新成交价 |
| 涨跌幅 | `change_pct` | float | 今日涨跌幅 (%) |
| 成交量 | `volume` | float | 成交量（手） |
| 成交额 | `amount` | float | 成交额（元） |
| 量比 | `volume_ratio` | float | 用于点火异动预警 |
| 换手率 | `turnover_rate` | float | 换手率 (%) |
| 总市值 | `total_market_cap` | float | 总市值（元） |

---

### 2. 每日涨停池
- **AkShare 接口**: `ak.stock_zt_pool_em(date="YYYYMMDD")`
- **系统封装方法**: `DataFetcher.get_zt_pool(date_str)`
- **应用场景**: 盘后复盘计算 5D 情绪多维向量中的**高度 Height（最高连板数）**、**宽度 Breadth（涨停家数）** 及**封单力度 Force**。
- **关键字段**:
  - `lbc`: 连板天数（系统用于按连板层级构建连板梯队）
  - `seal_amount`: 封单资金（系统用于计算全市场封单资金 / 总成交额比例）
  - `open_count`: 炸板次数

---

### 3. 每日炸板观察池
- **AkShare 接口**: `ak.stock_zt_pool_zbgc_em(date="YYYYMMDD")`（旧版 `stock_zt_pool_zhaban_em` 已废弃）
- **系统封装方法**: `DataFetcher.get_zhaban_pool(date_str)`
- **应用场景**: 计算情绪向量中的**炸板率 (Support 承接分值)**：`炸板率 = 炸板家数 / (涨停家数 + 炸板家数)`。
  注：`zbgc_em` 返回涨停观察池（含炸板次数），通过 `炸板次数 > 0` 过滤得出当日炸板股。

---

### 4. 每日跌停池
- **AkShare 接口**: `ak.stock_zt_pool_dtgc_em(date="YYYYMMDD")`
- **系统封装方法**: `DataFetcher.get_dt_pool(date_str)`
- **应用场景**: 计算情绪向量中的**宽度 Breadth (涨停家数 - 跌停家数)**，评估市场恐慌杀跌程度。

---

### 5. 龙虎榜数据（个股聚合 + 营业部明细）
- **AkShare 接口**:
  - 个股聚合：`ak.stock_lhb_detail_em(start_date="YYYYMMDD", end_date="YYYYMMDD")`（新版返回个股级数据，含龙虎榜净买额/买入额/卖出额，不含营业部名称）
  - 营业部明细：`ak.stock_lhb_hyyyb_em(start_date="YYYYMMDD", end_date="YYYYMMDD")`（活跃营业部排行，含营业部名称/买入总金额/卖出总金额/总买卖净额）
- **系统封装方法**: `DataFetcher.get_lhb_detail(date_str)` + `DataFetcher.get_lhb_seats(date_str)`
- **应用场景**: 盘后提取龙虎榜个股数据 + 营业部席位数据，传给 `SeatAnalyzer` 识别游资派系（**格局派** 如六一中路/上海超短；**砸盘派** 如拉萨天团/量化派）。
- **关键字段**: 个股级 `net_amount/buy_amount/sell_amount`，营业部级 `seat_name/buy_amount/sell_amount/net_amount/buy_stocks`。

---

### 6. 概念/行业板块成分股
- **AkShare 接口**: `ak.stock_board_industry_cons_em(symbol="板块名称")`
- **系统封装方法**: `DataFetcher.get_board_cons(board_name)`
- **应用场景**: 盘前维护 `Active_Core_Pool`（动态中军池），筛选板块内成交额 Top 3 & 总市值 Top 5 且成交额 > 20 亿的个股。

---

### 7. 主力资金流向
- **AkShare 接口**: `ak.stock_individual_fund_flow(stock_code="600519", market="sh")`
- **系统封装方法**: `DataFetcher.get_individual_fund_flow(code, market)`
- **应用场景**: 对盘中点火标的逐只查询主力净流入，识别游资合力扫货迹象。新版接口需指定个股代码，不再支持全市场排名。

---

### 8. 财联社全球电报/快讯
- **AkShare 接口**: `ak.stock_info_global_cls()`
- **系统封装方法**: `NewsFetcher.get_cls_news(limit=15)`
- **应用场景**: 08:30 盘前简报抓取最新的 15 条新闻快讯，由 LLM 提取政策关键词、评估政策级别（国家级 vs 地方级）。

---

### 9. 同花顺/东财热搜榜
- **AkShare 接口**: `ak.stock_hot_rank_em()`
- **系统封装方法**: `NewsFetcher.get_hot_search_words(limit=15)`
- **应用场景**: 08:30 盘前简报获取全网热搜 Top 15 个股与词汇，辅助判断市场关注度焦点。

---

## 三、接口防封与重试异常处理机制

为防止 AkShare 遇到网络波动、接口超时或频率限制导致策略中断，系统在 `data/fetcher.py` 中实现了 `@retry_on_exception` 装饰器机制：

```python
def retry_on_exception(retries: int = 3, delay: float = 2.0, backoff: float = 1.5):
    """
    网络请求重试装饰器，支持指数退避
    - retries: 最大重试次数 (默认 3 次，可在 settings.py 配置)
    - delay: 重试延迟时间 (默认 2 秒)
    - backoff: 延迟倍数，实现指数退避
    """
```

所有数据抓取函数均进行了空值（`None` 或 `Empty DataFrame`）防御性校验与字段重命名转换，保证下游策略计算不会因字段变动引发崩溃。
