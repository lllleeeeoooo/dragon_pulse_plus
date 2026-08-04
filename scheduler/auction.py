import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from notifier.bark import bark_notifier
from core.trade_calendar import is_trading_day, is_last_non_trading_day

logger = logging.getLogger(__name__)
import scheduler.helpers as _helpers
from database import RecommendationManager
from llm.call_auction import CallAuctionAnalyzer


def _classify_auction_verdicts(result_text: str, targets: list) -> dict:
    """
    从 LLM 竞价结论中为每个推荐标的归类，返回 {code: {"verdict": 买入/观察/放弃, "premise": 满足/不满足/""}}。
    优先解析结构化字段「判断=买入/观察/放弃」「前提=满足/不满足」（Prompt 已强制逐票下判），
    解析不到时回退到关键词近似匹配。
    供盘中"判断=买入 且 前提=满足"才执行竞价买入指令。
    """
    import re
    verdicts = {}
    if not result_text:
        return verdicts
    for t in targets or []:
        code = str(t.get("code", "")).strip()
        name = str(t.get("name", code))
        if not code:
            continue
        lines = [ln for ln in result_text.splitlines() if code in ln or name in ln]
        text = "\n".join(lines)
        verdict = "观察"
        premise = ""
        if text:
            # 结构化判定优先：判断=买入/观察/放弃（支持 = : ： 变体）
            m = re.search(r"判断\s*[=:：]\s*(买入|观察|放弃)", text)
            if m:
                verdict = m.group(1)
            elif any(k in text for k in ("放弃", "不介入", "回避", "不追", "不买", "不参与", "不建议")):
                verdict = "放弃"
            elif any(k in text for k in ("直接挂单买入", "挂单买入", "竞价买入", "直接买入", "抢筹", "买进", "建议买入")):
                verdict = "买入"
            p = re.search(r"前提\s*[=:：]\s*(满足|不满足)", text)
            if p:
                premise = p.group(1)
        verdicts[code] = {"verdict": verdict, "premise": premise}
    return verdicts


def _extract_verdicts_json(result_text: str) -> dict:
    """
    从 LLM 竞价结论解析结构化 JSON（Prompt 已要求输出）:
    {"verdicts": [{"code": "001208", "verdict": "买入", "premise": "满足", "reason": "..."}]}
    → {code: {"verdict", "premise", "reason"}}。解析失败返回 {}。
    """
    data = _helpers.extract_json_block(result_text)
    if not data:
        return {}
    verdicts = {}
    for item in data.get("verdicts") or []:
        code = str(item.get("code", "")).strip()
        if not code or len(code) != 6 or not code.isdigit():
            continue
        verdicts[code] = {
            "verdict": item.get("verdict", "观察"),
            "premise": item.get("premise", ""),
            "reason": item.get("reason", ""),
        }
    return verdicts


def job_call_auction():
    """
    09:26 竞价观察与指令定时任务。非交易日自动跳过。
    """
    _helpers._record_job_run("job_call_auction", "竞价观察")
    if not is_trading_day():
        logger.info("今日非交易日，跳过竞价观察")
        return

    logger.info(">>> 触发 09:26 竞价观察定时任务...")
    try:
        today = datetime.datetime.now()
        today_str = today.strftime("%Y%m%d")
        from core.trade_calendar import get_previous_trading_day
        yesterday_str = get_previous_trading_day(today.date())

        # ---- 1. 昨日 LLM 复盘推荐标的 ----
        # 复盘在 T 日 18:01 以 trade_date=T 落库，T+1 日需按"上一交易日"查询
        pending_recs = RecommendationManager.get_pending_recommendations(trade_date=yesterday_str)
        lines = []
        if pending_recs:
            for r in pending_recs:
                lines.append(f"- [复盘推荐] {r['name']}({r['code']}): {r['strategy_type']}"
                             f" | 要求: {r['open_requirement']}")

        # ---- 2. 昨日涨停池中连板/首板标的（修复 #1）----
        yesterday_zt_targets = []  # 结构化 {code,name,lbc}，供 LLM 逐只匹配真实竞价数据
        try:
            yesterday_zt = DataFetcher.get_zt_pool(date_str=yesterday_str)
            if yesterday_zt is not None and not yesterday_zt.empty and "lbc" in yesterday_zt.columns:
                # 取连板>=2的高标和首板，按连板数降序
                zt_obs = yesterday_zt[yesterday_zt["lbc"].astype(int) >= 1].head(15)
                for _, zrow in zt_obs.iterrows():
                    zcode = str(zrow.get("code", ""))
                    zname = str(zrow.get("name", ""))
                    zlbc = int(zrow.get("lbc", 1))
                    yesterday_zt_targets.append({"code": zcode, "name": zname, "lbc": zlbc})
                    tag = f"{zlbc}连板" if zlbc >= 2 else "首板"
                    lines.append(f"- [昨涨停-{tag}] {zname}({zcode})")
        except Exception as e:
            logger.warning(f"获取昨日涨停池连板/首板标的失败: {e}")

        recs_summary = "\n".join(lines) if lines else "暂无待观察标的"

        # ---- 3. 盘前简报预测板块（优先 DB——进程重启不丢；内存缓存次之；都无则空）----
        try:
            from database import PreMarketReportManager
            predicted_summary = (PreMarketReportManager.get(today_str)
                                 or _helpers._cached_pre_market_report or "")[:800]
        except Exception as e:
            logger.warning(f"读取盘前简报失败: {e}")
            predicted_summary = (_helpers._cached_pre_market_report or "")[:800]

        # ---- 4. 抓取实时竞价快照 ----
        spot_df = DataFetcher.get_realtime_spot()

        # ---- 4.5 记录每只推荐标的的 09:26 竞价成交额（断链3：供盘中竞价量能校验）----
        try:
            auction_amounts = {}
            for r in pending_recs:
                code = str(r.get("code", ""))
                m = spot_df[spot_df["code"].astype(str) == code] \
                    if spot_df is not None and not spot_df.empty else None
                if m is not None and not m.empty:
                    auction_amounts[code] = float(m.iloc[0].get("amount", 0))
            if auction_amounts:
                RecommendationManager.save_auction_amounts(auction_amounts, yesterday_str)
        except Exception as e:
            logger.warning(f"保存竞价金额失败: {e}")

        # ---- 5. LLM 竞价观察指令 ----
        # 读取 09:25 规则引擎竞价预判作为 LLM 上下文
        from scheduler.monitor_auction import _auction_prediction_cache
        auction_prediction = _auction_prediction_cache or ""

        # 获取昨日涨停溢价，用于竞价风控
        premium_info = DataFetcher.get_yesterday_zt_premium()
        result = CallAuctionAnalyzer.run_auction_analysis(
            trade_date=today_str,
            auction_df=spot_df,
            yesterday_zt_auction_yield=premium_info.get("intraday_premium", 1.5),
            recommended_targets_summary=recs_summary,
            recommended_targets=pending_recs,
            yesterday_zt_targets=yesterday_zt_targets,
            predicted_sectors_summary=predicted_summary,
            auction_prediction=auction_prediction
        )

        # 竞价结论落库：优先解析 LLM 结构化 JSON（verdicts 列表），解析不到再回退关键词分类
        if result and pending_recs:
            try:
                verdicts = _extract_verdicts_json(result)
                if not verdicts:
                    verdicts = _classify_auction_verdicts(result, pending_recs)
                # trade_date 限定昨日推荐，避免多日 PENDING 累积写错（断链7）
                RecommendationManager.update_auction_verdicts(verdicts, yesterday_str)
                logger.info(f"竞价结论落库: {verdicts}")
            except Exception as e:
                logger.warning(f"竞价结论落库失败: {e}")

        # 推送（bark 层统一去掉结构化 JSON 块，用户只看可读 Markdown 结论）
        bark_notifier.send(
            title="🎯 09:26 竞价超预期指令",
            body=result,
            group="竞价指令",
            level="timeSensitive"
        )
    except Exception as e:
        logger.error(f"09:26 竞价观察执行异常: {e}")


