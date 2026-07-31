import logging
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Query, HTTPException, Header
import uvicorn

from database.services import HoldingManager, PushLogManager, LLMLogManager, ErrorLogManager
from data.fetcher import DataFetcher
from config.settings import settings
from scheduler.daily_runner import job_pre_market, job_call_auction, job_post_market

logger = logging.getLogger(__name__)

# 创建 FastAPI Web HTTP 接口应用
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="dragon_pulse_plus 持仓管理 API",
    description="REST API 用于添加、查看、更新与平仓股票持仓",
    version="1.0.0"
)

# 简单的 API Key 鉴权：通过设置 API_KEY 环境变量或 .env 文件控制访问
API_KEY = getattr(settings, "API_KEY", None) or ""


def _check_auth(x_api_key: Optional[str] = Header(None)):
    """基本 API Key 鉴权（未配置 API_KEY 时跳过校验）"""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="无效的 API Key")


@app.get("/", summary="系统状态接口")
def root():
    return {"status": "ok", "message": "dragon_pulse_plus 持仓管理 API 正常运行中"}


@app.get("/holdings", summary="查看当前活跃持仓列表")
def get_holdings(
    type: Optional[str] = Query(None, description="持仓类型筛选: MANUAL(手动持仓) 或 AI_AUTO(AI自动持仓)，不填返回全部")
):
    """
    HTTP GET 示例: http://127.0.0.1:8000/holdings?type=AI_AUTO
    """
    holdings = HoldingManager.get_active_holdings(holding_type=type)
    return {
        "code": 200,
        "msg": "获取成功",
        "count": len(holdings),
        "data": holdings
    }


@app.post("/holdings/add", summary="添加新持仓股票")
def add_holding(
    x_api_key: Optional[str] = Header(None),
    code: str = Query(..., description="股票代码，如 000001"),
    price: float = Query(..., description="买入成本价，如 10.5"),
    quantity: int = Query(100, description="持仓数量，默认 100"),
    strategy: str = Query("低吸战法", description="买入战法标签 (低吸战法/打板战法/二波战法/抱团战法/共振战法)"),
    buy_date: str = Query("", description="买入日期 YYYY-MM-DD，默认为今天")
):
    """
    POST 请求添加新持仓（支持 GET 兼容）。系统根据股票代码自动匹配股票名称。
    示例: POST /holdings/add?code=000001&price=10.5&strategy=低吸战法
    """
    _check_auth(x_api_key)
    matched_name = DataFetcher.get_stock_name(code=code)

    success = HoldingManager.add_holding(
        code=code,
        name=matched_name,
        cost_price=price,
        quantity=quantity,
        buy_date=buy_date,
        strategy=strategy
    )
    if success:
        return {
            "code": 200,
            "msg": f"成功添加持仓 {matched_name}({code})",
            "data": {"code": code, "name": matched_name, "price": price, "strategy": strategy}
        }
    else:
        raise HTTPException(status_code=500, detail="添加持仓失败，请检查数据库")


# 兼容旧版 GET 请求（保留灵活性，但标记为 deprecated）
@app.get("/holdings/add", summary="[已弃用] 添加新持仓股票 - 请使用 POST")
def add_holding_get(
    code: str = Query(..., description="股票代码"),
    price: float = Query(..., description="买入成本价"),
    quantity: int = Query(100, description="持仓数量"),
    strategy: str = Query("低吸战法", description="买入战法标签"),
    buy_date: str = Query("", description="买入日期 YYYY-MM-DD")
):
    """向后兼容的 GET 接口，建议迁移至 POST"""
    return add_holding(
        x_api_key=None,  # GET 兼容模式下不强制鉴权
        code=code,
        price=price,
        quantity=quantity,
        strategy=strategy,
        buy_date=buy_date
    )


@app.post("/holdings/close", summary="平仓/卖出指定股票")
def close_holding(
    x_api_key: Optional[str] = Header(None),
    code: str = Query(..., description="股票代码，如 000001")
):
    """
    POST 请求平仓（支持 GET 兼容）
    示例: POST /holdings/close?code=000001
    """
    _check_auth(x_api_key)
    success = HoldingManager.close_holding(code=code)
    if success:
        return {
            "code": 200,
            "msg": f"成功平仓 {code}",
            "data": {"code": code, "status": "CLOSED"}
        }
    else:
        raise HTTPException(status_code=404, detail=f"未找到代码为 {code} 的活跃持仓")


# 兼容旧版 GET 请求
@app.get("/holdings/close", summary="[已弃用] 平仓/卖出指定股票 - 请使用 POST")
def close_holding_get(code: str = Query(..., description="股票代码")):
    """向后兼容的 GET 接口，建议迁移至 POST"""
    return close_holding(x_api_key=None, code=code)


@app.get("/push-logs", summary="查询推送历史记录")
def get_push_logs(
    date: Optional[str] = Query(None, description="查询日期，格式 YYYY-MM-DD，不填返回最近50条"),
    group: Optional[str] = Query(None, description="按推送分组筛选，如 盘后复盘/盘中异动/AI自动持仓"),
    limit: int = Query(50, description="返回条数上限，默认50")
):
    """
    查询推送通知历史记录，支持按日期和分组筛选
    示例: GET /push-logs?date=2026-07-29&limit=20
    """
    logs = PushLogManager.get_logs(date_str=date, push_group=group, limit=limit)
    return {
        "code": 200,
        "msg": "获取成功",
        "count": len(logs),
        "data": logs
    }


@app.get("/llm-logs", summary="查询 LLM 调用历史记录")
def get_llm_logs(
    module: Optional[str] = Query(None, description="模块筛选: pre_market/call_auction/post_market/sell_advisor"),
    date: Optional[str] = Query(None, description="查询日期 YYYY-MM-DD"),
    success: Optional[bool] = Query(None, description="仅查成功/失败"),
    limit: int = Query(50, description="返回条数上限")
):
    """查询大模型调用历史，支持按模块、日期、成功/失败筛选"""
    logs = LLMLogManager.get_logs(module=module, date_str=date, success_only=success, limit=limit)
    return {"code": 200, "msg": "获取成功", "count": len(logs), "data": logs}


@app.get("/error-logs", summary="查询系统错误日志")
def get_error_logs(
    level: Optional[str] = Query(None, description="ERROR 或 WARNING"),
    date: Optional[str] = Query(None, description="查询日期 YYYY-MM-DD"),
    module: Optional[str] = Query(None, description="模块名模糊匹配"),
    limit: int = Query(100, description="返回条数上限")
):
    """查询系统错误/警告日志，覆盖 akshare 异常、推送异常、代码异常等"""
    logs = ErrorLogManager.get_logs(level=level, date_str=date, module=module, limit=limit)
    return {"code": 200, "msg": "获取成功", "count": len(logs), "data": logs}


@app.post("/jobs/pre-market", summary="手动触发盘前简报")
def trigger_pre_market(x_api_key: Optional[str] = Header(None)):
    """立即执行 08:30 盘前简报任务（新闻抓取 + LLM 分析 + Bark 推送）"""
    _check_auth(x_api_key)
    try:
        job_pre_market()
        return {"code": 200, "msg": "盘前简报已执行完成"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"盘前简报执行失败: {e}")


@app.post("/jobs/call-auction", summary="手动触发竞价观察")
def trigger_call_auction(x_api_key: Optional[str] = Header(None)):
    """立即执行 09:26 竞价观察任务（拉涨停池 + LLM 竞价分析 + Bark 推送）"""
    _check_auth(x_api_key)
    try:
        job_call_auction()
        return {"code": 200, "msg": "竞价观察已执行完成"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"竞价观察执行失败: {e}")


@app.post("/jobs/post-market", summary="手动触发盘后复盘")
def trigger_post_market(x_api_key: Optional[str] = Header(None)):
    """立即执行 15:30 盘后复盘任务（情绪计算 + LLM 深度复盘 + 推荐入库 + 龙头表更新 + Bark 推送）"""
    _check_auth(x_api_key)
    try:
        job_post_market()
        return {"code": 200, "msg": "盘后复盘已执行完成"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"盘后复盘执行失败: {e}")


# ==================== AkShare 数据接口（可在 /docs 页面直接调用测试） ====================

AKSHARE_TAG = "AkShare 数据"

@app.get("/data/spot", summary="全市场实时行情快照",
         description="对应 ak.stock_zh_a_spot_em()，返回全市场 A 股实时价格/涨跌幅/量比/成交额等",
         tags=[AKSHARE_TAG])
def data_spot():
    df = DataFetcher.get_realtime_spot()
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.head(100).to_dict(orient="records")}


@app.get("/data/zt-pool", summary="每日涨停池",
         description="对应 ak.stock_zt_pool_em(date)，返回涨停股列表含连板数/封板资金/炸板次数等",
         tags=[AKSHARE_TAG])
def data_zt_pool(date: str = Query(..., description="日期 YYYYMMDD，如 20260729")):
    df = DataFetcher.get_zt_pool(date_str=date)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.to_dict(orient="records")}


@app.get("/data/zhaban-pool", summary="每日炸板观察池",
         description="对应 ak.stock_zt_pool_zbgc_em(date)，返回炸板观察池",
         tags=[AKSHARE_TAG])
def data_zhaban_pool(date: str = Query(..., description="日期 YYYYMMDD")):
    df = DataFetcher.get_zhaban_pool(date_str=date)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.to_dict(orient="records")}


@app.get("/data/dt-pool", summary="每日跌停池",
         description="对应 ak.stock_zt_pool_dtgc_em(date)",
         tags=[AKSHARE_TAG])
def data_dt_pool(date: str = Query(..., description="日期 YYYYMMDD")):
    df = DataFetcher.get_dt_pool(date_str=date)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.to_dict(orient="records")}


@app.get("/data/lhb-detail", summary="龙虎榜个股明细",
         description="对应 ak.stock_lhb_detail_em(start_date, end_date)",
         tags=[AKSHARE_TAG])
def data_lhb_detail(date: str = Query(..., description="日期 YYYYMMDD")):
    df = DataFetcher.get_lhb_detail(date_str=date)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.to_dict(orient="records")}


@app.get("/data/lhb-seats", summary="龙虎榜活跃营业部",
         description="对应 ak.stock_lhb_hyyyb_em(start_date, end_date)，返回营业部级买卖数据",
         tags=[AKSHARE_TAG])
def data_lhb_seats(date: str = Query(..., description="日期 YYYYMMDD")):
    df = DataFetcher.get_lhb_seats(date_str=date)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.head(50).to_dict(orient="records")}


@app.get("/data/board-cons", summary="板块成分股",
         description="对应 ak.stock_board_industry_cons_em(symbol)",
         tags=[AKSHARE_TAG])
def data_board_cons(board: str = Query(..., description="板块名称，如 半导体/人工智能/低空经济")):
    df = DataFetcher.get_board_cons(board_name=board)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.head(50).to_dict(orient="records")}


@app.get("/data/news", summary="财联社电报快讯",
         description="对应 ak.stock_info_global_cls()",
         tags=[AKSHARE_TAG])
def data_news(limit: int = Query(20, description="返回条数")):
    from data.news_fetcher import NewsFetcher
    items = NewsFetcher.get_cls_news(limit=limit)
    return {"code": 200, "count": len(items), "data": items}


@app.get("/data/hot-rank", summary="同花顺热搜榜",
         description="对应 ak.stock_hot_rank_em()",
         tags=[AKSHARE_TAG])
def data_hot_rank():
    from data.news_fetcher import NewsFetcher
    items = NewsFetcher.get_hot_search_words(limit=20)
    return {"code": 200, "count": len(items), "data": items}


@app.get("/data/intraday", summary="个股分时 OHLCV（多源降级）",
         description="新浪→腾讯→东财，返回 5 分钟 K 线标准列 [time, open, high, low, close, volume, change_pct]",
         tags=[AKSHARE_TAG])
def data_intraday(code: str = Query(..., description="股票代码，如 300359")):
    df = DataFetcher._fetch_intraday_5min(code)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.tail(20).to_dict(orient="records")}


@app.get("/data/kline-daily", summary="个股历史日K线（含均线）",
         description="对应 ak.stock_zh_a_hist(symbol, period='daily', adjust='qfq')，返回日K线及 MA5/MA10/MA20 均线",
         tags=[AKSHARE_TAG])
def data_kline_daily(code: str = Query(..., description="股票代码，如 000001"),
                     lookback: int = Query(30, description="回溯天数，默认 30")):
    ma_data = DataFetcher.get_stock_ma_prices(code=code, lookback=lookback)
    return {
        "code": 200,
        "data": {
            "code": code,
            "ma5": ma_data.get("ma5"),
            "ma10": ma_data.get("ma10"),
            "ma20": ma_data.get("ma20")
        }
    }


@app.get("/data/intraday-tick", summary="个股分时逐笔成交（VWAP）",
         description="对应 ak.stock_intraday_em(symbol)，返回分时成交量加权均价 VWAP，用于破位止损判断",
         tags=[AKSHARE_TAG])
def data_intraday_tick(code: str = Query(..., description="股票代码，如 000001")):
    vwap = DataFetcher.get_intraday_vwap(code=code)
    pattern = DataFetcher.get_intraday_pattern(code=code)
    return {
        "code": 200,
        "data": {
            "code": code,
            "vwap": vwap,
            "pattern": pattern
        }
    }


@app.get("/data/fund-flow", summary="个股主力资金流向",
         description="对应 ak.stock_individual_fund_flow(stock, market)，返回个股历史主力净流入/超大单净额等",
         tags=[AKSHARE_TAG])
def data_fund_flow(code: str = Query(..., description="股票代码，如 600519"),
                   market: str = Query("sh", description="市场: sh(沪市) 或 sz(深市)")):
    df = DataFetcher.get_individual_fund_flow(stock_code=code, market=market)
    if df.empty:
        return {"code": 200, "count": 0, "data": []}
    return {"code": 200, "count": len(df), "data": df.tail(10).to_dict(orient="records")}


@app.get("/data/index-daily", summary="上证指数日K线",
         description="对应 ak.stock_zh_index_daily(symbol='sh000001')，用于计算大盘近 3 日/10 日涨跌幅",
         tags=[AKSHARE_TAG])
def data_index_daily():
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        if df is not None and not df.empty:
            recent = df.tail(30)
            return {"code": 200, "count": len(recent), "data": recent.to_dict(orient="records")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"code": 200, "count": 0, "data": []}


@app.post("/data/trade-calendar/refresh", summary="强制刷新交易日历",
          description="重新从 akshare 拉取交易日历（ak.tool_trade_date_hist_sina），覆盖 ±30 天数据。应对调休/假期安排变动。",
          tags=[AKSHARE_TAG])
def refresh_trade_calendar(x_api_key: Optional[str] = Header(None)):
    _check_auth(x_api_key)
    from database.services import TradeCalendarManager
    TradeCalendarManager.sync_calendar(force=True)
    return {"code": 200, "msg": "交易日历已强制刷新"}


@app.get("/backtest", summary="模拟回测",
         description="用历史涨停池数据回测 AI 买卖策略。示例：/backtest?strategy=打板接力&hold_days=3",
         tags=["AkShare 数据"])
def run_backtest(
    start: str = Query("20260701", description="起始日期 YYYYMMDD"),
    end: str = Query("20260730", description="结束日期 YYYYMMDD"),
    strategy: str = Query("打板接力", description="策略：打板接力/低吸/全部"),
    hold_days: int = Query(3, description="持仓天数"),
):
    from core.backtest import BacktestEngine
    result = BacktestEngine.run(
        start_date=start, end_date=end,
        strategy=strategy, hold_days=hold_days
    )
    return {"code": 200, "data": result}


@app.get("/monitor", response_class=HTMLResponse, summary="实时风控看板",
         description="HTML 页面，每10秒自动刷新，展示持仓盈亏+市场风格+点火信号")
def monitor_page():
    """实时风控看板 HTML 页面"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>DragonPulse 实时风控看板</title>
<style>
body{font-family:'Microsoft YaHei',sans-serif;background:#0a0e17;color:#e0e6ed;padding:10px;margin:0}
h1{color:#00d4aa;font-size:18px;text-align:center;margin:5px 0}
.card{background:#141b25;border-radius:8px;padding:12px;margin:8px 0;border-left:3px solid #00d4aa}
.red{border-left-color:#ff4757}.yellow{border-left-color:#ffa502}.green{border-left-color:#2ed573}
.row{display:flex;justify-content:space-between;align-items:center;margin:4px 0}
.label{color:#8899aa;font-size:12px}.value{font-size:16px;font-weight:bold}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;background:#1a2235;color:#00d4aa}
.badge-red{background:#3d1520;color:#ff4757}
.badge-green{background:#153d20;color:#2ed573}
.pnl-up{color:#ff4757}.pnl-down{color:#2ed573}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}
.live{animation:pulse 2s infinite;display:inline-block;width:8px;height:8px;background:#00d4aa;border-radius:50%;margin-right:4px}
</style></head>
<body>
<h1><span class="live"></span>DragonPulse 实时风控看板</h1>
<div id="app">加载中...</div>
<script>
async function load(){const r=await fetch('/dashboard');const d=(await r.json()).data;
document.getElementById('app').innerHTML=`
<div class="card ${d.market.style==='抱团'||d.market.style==='高潮'?'red':d.market.style==='共振'?'green':'yellow'}">
  <div class="row"><span class="label">市场风格</span><span class="value">${d.market.style} → ${d.market.strategy}</span></div>
  <div class="row"><span class="label">${d.market.reason}</span></div>
  <div class="row"><span class="label">容量因子</span><span class="value">K=${d.liquidity.capacity_factor}</span></div>
  <div class="row"><span class="label">此刻/昨日</span><span class="value">${d.liquidity.now_amount} / ${d.liquidity.baseline_yesterday}</span></div>
</div>
<div class="card">
  <div class="row"><span class="label">情绪分</span><span class="value">${d.emotion.sentiment_index}<span style="font-size:10px;color:#8899aa"> 溢价${d.emotion.score_premium} 宽度${d.emotion.score_breadth} 高度${d.emotion.score_height} 承接${d.emotion.score_support}</span></span></div>
  <div class="row"><span class="label">开盘溢价</span><span class="value">${d.emotion.premium_opening}</span></div>
  <div class="row"><span class="label">即时溢价</span><span class="value">${d.emotion.premium_intraday}</span></div>
  <div class="row"><span class="label">高开率 / 红盘率</span><span class="value">${d.emotion.high_open_rate} / ${d.emotion.red_rate}</span></div>
</div>
<div class="card">
  <div class="row"><span class="label">涨跌分布</span><span class="value"><span style="color:#ff4757">${d.breadth.up_count}</span> / <span style="color:#2ed573">${d.breadth.down_count}</span> / ${d.breadth.flat_count}</span></div>
  <div class="row"><span class="label">涨停 / 跌停</span><span class="value">${d.breadth.zt_count}<span class="badge">${d.breadth.zt_source}</span> / <span class="badge badge-red">${d.breadth.dt_count}</span></span></div>
  <div class="row"><span class="label">炸板</span><span class="value">${d.breadth.zhaban_rate} (${d.breadth.zhaban_count}只)</span></div>
  <div class="row"><span class="label">最高连板</span><span class="value">${d.breadth.height}板</span></div>
</div>
<div class="card green">
  <div class="row"><span class="label">活跃持仓</span><span class="value">${d.holdings.active}只</span></div>
  <div class="row"><span class="label">AI自动</span><span class="badge badge-green">${d.holdings.ai_auto}只</span></div>
  <div class="row"><span class="label">手动</span><span class="badge">${d.holdings.manual}只</span></div>
</div>
<div class="card">
  <div class="row"><span class="label" style="color:#00d4aa">定时任务</span></div>
  <div class="row"><span class="label">04:00 日志清理</span><span class="badge">15天/30天</span></div>
  <div class="row"><span class="label">04:05 龙头过期</span><span class="badge">>30天</span></div>
  <div class="row"><span class="label">08:30 盘前简报</span><span class="badge">新闻→LLM</span></div>
  <div class="row"><span class="label">09:26 竞价观察</span><span class="badge">竞价→LLM</span></div>
  <div class="row"><span class="label">09:30-15:00 盘中监控</span><span class="badge">15s轮询</span></div>
  <div class="row"><span class="label">15:30 盘后复盘+回测</span><span class="badge">LLM+推送</span></div>
  <div class="row"><span class="label">20:00 假日汇总</span><span class="badge">假期最后一天</span></div>
</div>
<div style="text-align:center;color:#445566;font-size:11px;margin-top:10px">
${d.updated?'每10秒自动刷新':'等待监控数据...'} | <a href="/docs" style="color:#445566">API文档</a> | <a href="/dashboard" style="color:#445566">JSON</a>
</div>`}
load();setInterval(load,10000);
</script></body></html>"""


@app.get("/dashboard", summary="情绪看板（手机端一站式概览）",
         description="返回市场风格/溢价/涨跌停/情绪分/持仓等关键数据",
         tags=["AkShare 数据"])
def dashboard():
    from scheduler.market_monitor import _current_market_style_global
    from database.services import HoldingManager
    style = dict(_current_market_style_global)
    holdings = HoldingManager.get_active_holdings()
    ai_count = sum(1 for h in holdings if h.get("holding_type") == "AI_AUTO")

    return {
        "code": 200,
        "data": {
            "market": {
                "style": style.get("style", "未知"),
                "strategy": style.get("priority_strategy", ""),
                "reason": style.get("reason", ""),
            },
            "liquidity": {
                "capacity_factor": style.get("capacity_factor", 0),
                "now_amount": f"{style.get('now_amount_billion', 0):.0f}亿",
                "baseline_yesterday": f"{style.get('baseline_ma20_billion', 0):.0f}亿",
            },
            "emotion": {
                "sentiment_index": style.get("sentiment_index", 0),
                "score_premium": style.get("score_premium", 0),
                "score_breadth": style.get("score_breadth", 0),
                "score_height": style.get("score_height", 0),
                "score_support": style.get("score_support", 0),
                "premium_opening": f"{style.get('premium_opening', 0)}%",
                "premium_intraday": f"{style.get('premium_intraday', 0)}%",
                "red_rate": f"{style.get('positive_ratio', 0)}%",
                "high_open_rate": f"{style.get('high_open_ratio', 0)}%",
            },
            "breadth": {
                "up_count": style.get("up_count", 0),
                "down_count": style.get("down_count", 0),
                "flat_count": style.get("flat_count", 0),
                "limit_up_est": style.get("limit_up_est", 0),
                "limit_down_est": style.get("limit_down_est", 0),
                "zt_count": style.get("zt_count", 0),
                "dt_count": style.get("dt_count", 0),
                "zhaban_count": style.get("zhaban_count", 0),
                "zhaban_rate": f"{style.get('zhaban_rate', 0)}%",
                "height": style.get("height", 0),
                "zt_source": style.get("zt_source", ""),
            },
            "holdings": {
                "active": len(holdings),
                "ai_auto": ai_count,
                "manual": len(holdings) - ai_count,
            },
            "updated": style.get("style", "") != "",
        }
    }


@app.get("/data/market-style", summary="盘中实时市场风格",
         description="返回当前市场风格判定及推荐战法，每15秒自动更新",
         tags=[AKSHARE_TAG])
def data_market_style():
    from scheduler.market_monitor import _current_market_style_global
    style = dict(_current_market_style_global)
    return {"code": 200, "data": style if style else {"style": "未知", "reason": "监控尚未启动或数据不足"}}


@app.get("/data/emotion", summary="情绪多维向量",
         description="输入日期，返回当日情绪向量（高度/宽度/反馈/力度/承接/综合分）",
         tags=[AKSHARE_TAG])
def data_emotion(date: str = Query(..., description="日期 YYYYMMDD")):
    from core.emotion_index import EmotionVector
    zt_df = DataFetcher.get_zt_pool(date_str=date)
    zhaban_df = DataFetcher.get_zhaban_pool(date_str=date)
    dt_df = DataFetcher.get_dt_pool(date_str=date)
    result = EmotionVector.calculate(zt_df=zt_df, zhaban_df=zhaban_df, dt_df=dt_df)
    return {"code": 200, "data": result}


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """启动持仓管理 HTTP API 服务"""
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
