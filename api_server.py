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


@app.get("/data/zt-pool/live", summary="每日涨停池（实时抓取）",
         description="对应 ak.stock_zt_pool_em(date)，直接从 AkShare 实时抓取涨停池（含连板数/封板资金/炸板次数等）。"
                     "注意：/data/zt-pool（不带 /live）查询的是已落库数据，支持不传 date 默认取最新。",
         tags=[AKSHARE_TAG])
def data_zt_pool_live(date: str = Query(..., description="日期 YYYYMMDD，如 20260729")):
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


@app.get("/backtest", summary="模拟回测（历史模拟）",
         description="用历史涨停池数据模拟 AI 买卖策略的盈亏。这不是真实成交统计，是'如果当时买了会怎样'的模拟。"
                     "示例：/backtest?start=20260701&end=20260720",
         tags=["AkShare 数据"])
def run_backtest(
    start: str = Query("20260701", description="起始日期 YYYYMMDD"),
    end: str = Query("20260730", description="结束日期 YYYYMMDD"),
    max_positions: int = Query(None, description="最大持仓数，默认取 settings.MAX_AI_POSITIONS"),
    max_daily_buys: int = Query(None, description="每日最大买入，默认取 settings.MAX_DAILY_BUYS"),
):
    from core.backtest import AIBacktestEngine
    result = AIBacktestEngine.run(
        start_date=start, end_date=end,
        max_positions=max_positions, max_daily_buys=max_daily_buys,
    )
    return {"code": 200, "data": result}


@app.get("/trades/stats", summary="真实成交统计",
         description="查看数据库里 AI 真实买卖的盈亏统计。这不是模拟，是实盘成交记录。"
                     "示例：/trades/stats?holding_type=AI_AUTO",
         tags=["持仓管理"])
def trade_statistics(
    holding_type: str = Query(None, description="持仓类型：AI_AUTO / MANUAL，不传查全部"),
):
    from database.services import HoldingManager
    result = HoldingManager.get_trade_statistics(holding_type=holding_type or None)
    return {"code": 200, "data": result}


@app.get("/trades/equity-curve", summary="净值曲线",
         description="获取每日净值快照，用于画资金曲线。示例：/trades/equity-curve?days=30",
         tags=["持仓管理"])
def equity_curve(days: int = Query(60, description="回溯天数")):
    from database.services import DailySnapshotManager
    result = DailySnapshotManager.get_equity_curve(days=days)
    return {"code": 200, "data": result}


@app.get("/data/zt-pool", summary="涨停池明细",
         description="查询某日涨停池。示例：/data/zt-pool?date=20260801",
         tags=["AkShare 数据"])
def zt_pool_query(date: str = Query(None, description="日期 YYYYMMDD，默认取最新")):
    from database.services import ZtPoolManager, db_manager
    from database.models import DailyZtPool
    session = db_manager.get_session()
    try:
        if date is None:
            latest = session.query(DailyZtPool.trade_date).order_by(
                DailyZtPool.trade_date.desc()
            ).first()
            date = latest[0] if latest else ""
        records = session.query(DailyZtPool).filter(
            DailyZtPool.trade_date == date
        ).order_by(DailyZtPool.lbc.desc()).all()
        return {"code": 200, "data": [{
            "code": r.code, "name": r.name, "lbc": r.lbc,
            "price": r.price, "change_pct": r.change_pct,
            "industry": r.industry, "seal_amount": r.seal_amount,
            "first_seal_time": r.first_seal_time,
        } for r in records]}
    finally:
        session.close()


@app.get("/data/hot-sectors", summary="热门板块",
         description="查询某日板块强度排名。示例：/data/hot-sectors?date=20260801&top=10",
         tags=["AkShare 数据"])
def hot_sectors(date: str = Query(None, description="日期 YYYYMMDD，默认最新"),
                top: int = Query(10, description="返回前N个")):
    from database.services import SectorStrengthManager
    result = SectorStrengthManager.get_hot_sectors(date_str=date, top_n=top)
    return {"code": 200, "data": result}


@app.get("/data/seats", summary="龙虎榜席位画像",
         description="查询席位画像（累计买卖/自动分类/人工标签），按累计净买入降序。"
                     "示例：/data/seats?top=20",
         tags=["龙虎榜"])
def data_seats(
    seat_type: Optional[str] = Query(None, description="按类型筛选: 格局派/砸盘派/散户派/对倒派/外资北向/未知"),
    top: int = Query(20, description="返回前N个(按累计净买入降序)"),
    active_only: bool = Query(True, description="仅活跃席位"),
):
    from database import SeatProfileManager
    result = SeatProfileManager.get_profiles(seat_type=seat_type, top=top, active_only=active_only)
    return {"code": 200, "count": len(result), "data": result}


@app.get("/data/seats/stats", summary="龙虎榜席位画像统计",
         description="席位总数 + 按类型计数",
         tags=["龙虎榜"])
def data_seats_stats():
    from database import SeatProfileManager
    return {"code": 200, "data": SeatProfileManager.get_stats()}


@app.get("/data/sectors/cycle", summary="板块情绪周期",
         description="查看板块阶段（冰点/启动/发酵/高潮/退潮）与主线分。示例：/data/sectors/cycle?top=10",
         tags=["板块"])
def data_sectors_cycle(
    date: Optional[str] = Query(None, description="日期 YYYYMMDD，默认最新"),
    top: int = Query(20, description="返回前N个(按主线分降序)"),
):
    from database import SectorCycleManager
    result = SectorCycleManager.get_sector_cycle(trade_date=date, top=top)
    return {"code": 200, "count": len(result), "data": result}


@app.get("/data/concepts/cycle", summary="概念情绪周期",
         description="查看题材概念阶段（冰点/启动/发酵/高潮/退潮）与主线分（非题材标签已过滤）。示例：/data/concepts/cycle?top=10",
         tags=["概念"])
def data_concepts_cycle(
    date: Optional[str] = Query(None, description="日期 YYYYMMDD，默认最新"),
    top: int = Query(20, description="返回前N个(按主线分降序)"),
):
    from database import ConceptCycleManager
    result = ConceptCycleManager.get_concept_cycle(trade_date=date, top=top)
    return {"code": 200, "count": len(result), "data": result}


@app.get("/data/mainlines", summary="概念/行业双维度主线对照",
         description="概念主线(题材) vs 行业主线(东财) 并排对照，均含阶段/涨停/最高连板/主线分。",
         tags=["概念"])
def data_mainlines():
    from dashboard.data import _build_mainlines_section
    return {"code": 200, "data": _build_mainlines_section()}


@app.get("/data/source-status", summary="数据源熔断状态",
         description="查看各数据源当日异常次数与熔断状态。某源当日异常达 SOURCE_FAIL_CIRCUIT_LIMIT 次后，当天不再调用该源（次日自动重置）",
         tags=["AkShare 数据"])
def data_source_status():
    from data.core import source_circuit_status
    return {"code": 200, "data": source_circuit_status()}


@app.get("/monitor", response_class=HTMLResponse, summary="系统综合看板",
         description="HTML 页面，展示大盘/情绪/持仓/板块/龙头/系统状态")
def monitor_page():
    from dashboard import render_html
    return render_html()


@app.get("/dashboard", summary="系统综合看板 JSON",
         description="返回看板所需全部数据", tags=["AkShare 数据"])
def dashboard():
    from dashboard import build_dashboard_data
    return {"code": 200, "data": build_dashboard_data()}


@app.get("/data/market-style", summary="盘中实时市场风格",
         description="返回当前市场风格判定及推荐战法，每15秒自动更新",
         tags=[AKSHARE_TAG])
def data_market_style():
    from scheduler.monitor_core import _current_market_style_global
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


def run_server(host: str = None, port: int = 8000):
    """启动持仓管理 HTTP API 服务（默认绑定 settings.API_HOST，安全起见为 127.0.0.1）"""
    uvicorn.run(app, host=host or settings.API_HOST, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
