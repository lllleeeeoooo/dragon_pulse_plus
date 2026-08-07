# dragon_pulse_plus HTTP API 接口文档

本文档记录了 `dragon_pulse_plus` AI智能策略系统中基于 FastAPI/Uvicorn 构建的 **HTTP API 服务**，涵盖持仓管理、推送日志、LLM 调用记录及定时任务手动触发。

---

## 目录
1. [服务启动与说明](#一服务启动与说明)
2. [接口速查一览表](#二接口速查一览表)
3. [持仓管理接口](#三持仓管理接口)
4. [日志查询接口](#四日志查询接口)
5. [定时任务手动触发接口](#五定时任务手动触发接口)
6. [数据缓存与回测接口](#六数据缓存与回测接口)
7. [iOS 快捷指令配置指引](#七ios-快捷指令-shortcuts-配置指引)

---

## 一、服务启动与说明

- **默认监听地址**: `0.0.0.0:8000` (本地访问路径: `http://127.0.0.1:8000`)
- **自动后台启动**: 随系统主程序 `python main.py` 启动时，后台线程会自动拉起 API 服务。
- **单独启动方法**: 执行 `python api_server.py` 可单独启动 Web API 服务。
- **自动 Swagger 文档**: 浏览器访问 `http://127.0.0.1:8000/docs` 可查看交互式 API 测试界面。
- **认证**: 在 `.env` 中设置 `API_KEY` 后，写操作接口需在请求头传入 `X-API-Key`。

---

## 二、接口速查一览表

| 序号 | 分类 | 接口功能 | HTTP 方法 | 请求路径 |
|---|---|---|---|---|
| **1** | 系统 | 健康检查 | `GET` | `/` |
| **2** | 持仓 | 查看活跃持仓列表 | `GET` | `/holdings` |
| **3** | 持仓 | 添加新持仓 | `POST` | `/holdings/add` |
| **4** | 持仓 | 平仓/卖出 | `POST` | `/holdings/close` |
| **5** | 日志 | 查询推送历史 | `GET` | `/push-logs` |
| **6** | 日志 | 查询 LLM 调用记录 | `GET` | `/llm-logs` |
| **7** | 任务 | 手动触发盘前简报 | `POST` | `/jobs/pre-market` |
| **8** | 任务 | 手动触发竞价观察 | `POST` | `/jobs/call-auction` |
| **9** | 任务 | 手动触发盘后复盘 | `POST` | `/jobs/post-market` |
| **10** | 数据 | 拉取/补拉全市场日线缓存 | `POST` | `/backtest/etl-kline` |

---

## 三、持仓管理接口

### 1. 系统健康检查 `/`

- **HTTP 方法**: `GET`
- **请求 URL**: `http://127.0.0.1:8000/`
- **响应示例**:
```json
{"status": "ok", "message": "dragon_pulse_plus 持仓管理 API 正常运行中"}
```

---

### 2. 查看当前活跃持仓列表 `/holdings`

- **HTTP 方法**: `GET`
- **请求 URL**: `http://127.0.0.1:8000/holdings?type=AI_AUTO`
- **参数**:

| 参数名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 否 | `MANUAL`(手动) / `AI_AUTO`(AI自动)，不填返回全部 |

- **响应示例**:
```json
{
  "code": 200, "msg": "获取成功", "count": 2,
  "data": [
    {"id": 1, "code": "600519", "name": "贵州茅台", "cost_price": 1750.0,
     "quantity": 100, "buy_date": "2026-07-29", "buy_strategy": "低吸战法",
     "profit_rate": 2.5, "was_limit_up_today": false}
  ]
}
```

---

### 3. 添加新持仓 `/holdings/add`

- **HTTP 方法**: `POST`（兼容 `GET`）
- **请求 URL**: `http://127.0.0.1:8000/holdings/add?code=600519&price=1750&strategy=中军低吸`
- **参数**:

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `code` | string | 是 | - | 股票代码 |
| `price` | float | 是 | - | 买入成本价 |
| `strategy` | string | 否 | `低吸战法` | 低吸/打板/二波/抱团/共振 |
| `quantity` | int | 否 | `100` | 持仓股数 |
| `buy_date` | string | 否 | 当天 | `YYYY-MM-DD` |

- **响应示例**:
```json
{"code": 200, "msg": "成功添加持仓 贵州茅台(600519)",
 "data": {"code": "600519", "name": "贵州茅台", "price": 1750.0, "strategy": "中军低吸"}}
```

---

### 4. 平仓/卖出 `/holdings/close`

- **HTTP 方法**: `POST`（兼容 `GET`）
- **请求 URL**: `http://127.0.0.1:8000/holdings/close?code=600519`
- **参数**: `code` (string, 必填) — 股票代码
- **响应示例**:
```json
{"code": 200, "msg": "成功平仓 贵州茅台(600519)", "data": {"code": "600519", "status": "CLOSED"}}
```

---

## 四、日志查询接口

### 5. 查询推送历史 `/push-logs`

- **HTTP 方法**: `GET`
- **请求 URL**: `http://127.0.0.1:8000/push-logs?date=2026-07-29&group=盘中异动&limit=20`
- **参数**:

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `date` | string | 否 | - | 查询日期 `YYYY-MM-DD` |
| `group` | string | 否 | - | 分组: 盘前简报/竞价指令/盘后复盘/盘中异动/AI自动持仓/卖出提醒/炸板提醒 |
| `limit` | int | 否 | `50` | 返回条数上限 |

- **响应示例**:
```json
{"code": 200, "msg": "获取成功", "count": 3,
 "data": [{"id": 1, "title": "⚡ [点火预警] 全通教育(300359) +20.05%",
           "push_group": "盘中异动", "level": "timeSensitive",
           "send_success": true, "created_at": "2026-07-29 14:12:25"}]}
```

---

### 6. 查询 LLM 调用记录 `/llm-logs`

- **HTTP 方法**: `GET`
- **请求 URL**: `http://127.0.0.1:8000/llm-logs?module=post_market&date=2026-07-29&success=true&limit=10`
- **参数**:

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `module` | string | 否 | - | pre_market / call_auction / post_market / sell_advisor |
| `date` | string | 否 | - | 查询日期 `YYYY-MM-DD` |
| `success` | bool | 否 | - | 仅查成功/失败 |
| `limit` | int | 否 | `50` | 返回条数上限 |

- **响应示例**:
```json
{"code": 200, "msg": "获取成功", "count": 1,
 "data": [{"id": 1, "module": "post_market", "model": "deepseek-chat",
           "tokens_used": 8542, "success": true,
           "response": "## 今日情绪周期定性...", "created_at": "2026-07-29 15:32:10"}]}
```

---

## 五、定时任务手动触发接口

> ⚠️ 均为同步执行，任务完成才返回响应。盘后复盘耗时较长（需拉取分时数据），可能需要 30~60 秒。

### 7. 手动触发盘前简报 `/jobs/pre-market`

- **HTTP 方法**: `POST`
- **请求 URL**: `http://127.0.0.1:8000/jobs/pre-market`
- **Header**: `X-API-Key: <your_key>`（若配置了 API_KEY）
- **功能**: 立即执行 08:30 盘前简报——抓取新闻快讯 + 热搜榜，调用 LLM 预测今日爆发板块，Bark 推送结果。
- **响应示例**:
```json
{"code": 200, "msg": "盘前简报已执行完成"}
```

---

### 8. 手动触发竞价观察 `/jobs/call-auction`

- **HTTP 方法**: `POST`
- **请求 URL**: `http://127.0.0.1:8000/jobs/call-auction`
- **Header**: `X-API-Key: <your_key>`
- **功能**: 立即执行 09:26 竞价观察——拉取昨日涨停池连板/首板标的 + 盘前简报预测 + 实时竞价快照，LLM 生成竞价交易指令，Bark 推送。
- **响应示例**:
```json
{"code": 200, "msg": "竞价观察已执行完成"}
```

---

### 9. 手动触发盘后复盘 `/jobs/post-market`

- **HTTP 方法**: `POST`
- **请求 URL**: `http://127.0.0.1:8000/jobs/post-market`
- **Header**: `X-API-Key: <your_key>`
- **功能**: 立即执行 15:30 盘后复盘——拉取涨停池/炸板池/跌停池/龙虎榜/全市场快照/分时数据，计算情绪向量，LLM 深度复盘，推荐标的入库，龙头表自动更新，Bark 推送。
- **耗时**: 约 30~60 秒（取决于涨停股数量和分时数据拉取速度）
- **响应示例**:
```json
{"code": 200, "msg": "盘后复盘已执行完成"}
```

### 手动触发示例 (curl)

```bash
# 盘后复盘（最常用）
curl -X POST http://127.0.0.1:8000/jobs/post-market -H "X-API-Key: your_key"

# 竞价观察
curl -X POST http://127.0.0.1:8000/jobs/call-auction -H "X-API-Key: your_key"

# 盘前简报
curl -X POST http://127.0.0.1:8000/jobs/pre-market -H "X-API-Key: your_key"
```

---

## 六、数据缓存与回测接口

### 10. 拉取/补拉全市场日线缓存 `/backtest/etl-kline`

- **HTTP 方法**: `POST`
- **请求 URL**: `http://127.0.0.1:8000/backtest/etl-kline?start=20260701&end=20260806`
- **Header**: `X-API-Key: <your_key>`（仅当 API_KEY 已配置且该接口启用鉴权时）
- **功能**: 拉取回测区间内全市场历史日线到 `daily_kline` 表（断点续传，已完整覆盖的 code 自动跳过）。
  - 默认**并行**拉取（`workers` 控制线程数）；
  - `serial=true` 走**串行低QPS补拉**——逐只限速 + 多轮重试，每轮只重试未完整覆盖的 code，源限流/静默空返回时用，替代原 `_backfill_kline.py` 脚本。
- **参数**:

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `start` | string | 是 | - | 起始日期 `YYYYMMDD` |
| `end` | string | 是 | - | 结束日期 `YYYYMMDD` |
| `workers` | int | 否 | `settings.KLINE_ETL_WORKERS` | 并行线程数（仅并行模式生效，建议保持低位防源限流） |
| `serial` | bool | 否 | `false` | `true` 时走串行低QPS补拉（源限流时用） |
| `max_rounds` | int | 否 | `59` | 串行补拉最大轮数，每轮只重试未完整覆盖的 code |

- **注意**: 同步执行，全市场串行约 0.15s/只，首轮可能耗时数分钟；已有日线同步在跑时新调用直接返回跳过（进程内互斥，防跨天重叠，返回 `message` 而非 `error`）。
- **响应示例** (serial 模式):
```json
{"code": 200, "data": {"universe": 2345, "pulled": 612, "remaining": 0, "rounds": 2, "rows": 62594}}
```

---

## 七、iOS 快捷指令 (Shortcuts) 配置指引

1. **新建快捷指令**: 打开"快捷指令" App → 点击"+"新建
2. **添加"询问输入"**: 提示词设为"请输入股票代码"
3. **添加第二个"询问输入"**: 提示词设为"请输入买入价格"
4. **添加"获取 URL 内容"**: URL 格式 `http://<您的电脑局域网IP>:8000/holdings/add?code=<代码输入>&price=<价格输入>`
5. **保存到主屏幕**: 盘中买入一键录入
