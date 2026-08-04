import datetime
import json
import logging
import re
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from notifier.bark import bark_notifier
from core.trade_calendar import is_trading_day, is_last_non_trading_day, get_previous_trading_day
from database.services import RecommendationManager, db_manager
from database.models import SystemLog, HistoricDragon, Recommendation
from database import SystemLogManager

logger = logging.getLogger(__name__)

_cached_pre_market_report: str = ""
def _record_job_run(job_id: str, job_name: str):
    """记录定时任务执行时间（仅数据库，不占内存）"""
    now = datetime.datetime.now()
    try:
        # 监控循环每轮更新同一条记录（避免重复写入）
        if job_id == "job_monitor_loop":
            session = db_manager.get_session()
            try:
                existing = session.query(SystemLog).filter(
                    SystemLog.log_date == now.strftime("%Y-%m-%d"),
                    SystemLog.category == "job_run",
                    SystemLog.title == job_name,
                ).first()
                if existing:
                    existing.detail = f"{now.strftime('%H:%M:%S')}|{job_id}"
                    session.commit()
                    return
            finally:
                session.close()

        SystemLogManager.add_log(
            log_date=now.strftime("%Y-%m-%d"),
            category="job_run",
            title=job_name,
            detail=f"{now.strftime('%H:%M:%S')}|{job_id}"
        )
    except Exception as e:
        # 任务状态落库失败也要可追溯（否则看板"今日是否执行"会误显示未执行）
        logger.warning(f"任务执行记录失败 ({job_name}/{job_id}): {e}")


def _get_job_status() -> List[Dict[str, Any]]:
    """获取所有定时任务的状态列表（查库）"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    all_jobs = [
        {"id": "job_log_cleanup",    "name": "日志清理",         "time": "04:00", "desc": "系统/LLM/错误日志保留15天，推送30天"},
        {"id": "job_dragon_expire",  "name": "龙头过期标记",     "time": "04:05", "desc": "超过30天无人气龙头自动失效"},
        {"id": "job_pre_market",     "name": "盘前简报",         "time": "08:30", "desc": "新闻抓取→LLM分析→推送题材预测"},
        {"id": "job_call_auction",   "name": "竞价观察",         "time": "09:26", "desc": "竞价快照→LLM判断超预期标的"},
        {"id": "job_monitor_loop",   "name": "盘中实时监控",     "time": "09:30-15:00", "desc": "15秒轮询，点火异动+板块联动+AI自动交易"},
        {"id": "job_post_market",    "name": "盘后深度复盘",     "time": "18:01", "desc": "LLM复盘+推荐标的+指数落库+盈亏推送"},
        {"id": "job_holiday_summary","name": "假日消息汇总",     "time": "20:00", "desc": "假期最后一天汇总近期消息"},
    ]

    db_records = {}
    try:
        logs = SystemLogManager.get_logs(log_date=today, category="job_run", limit=50)
        for log in logs:
            detail = log.get("detail", "")
            if "|" in detail:
                parts = detail.split("|", 1)
                db_records[parts[1]] = {"last_run": parts[0], "last_date": today}
    except Exception:
        pass

    for j in all_jobs:
        record = db_records.get(j["id"])
        j["last_run"] = record["last_run"] if record else "-"
        j["ran_today"] = record is not None
    return all_jobs


def extract_json_block(text: str) -> dict:
    """
    从 LLM 输出中提取结构化 JSON：
    1) 优先 ```json ... ``` 代码块；2) 无围栏时取最外层 {...} 对象。
    解析失败返回 {}。用于"结构化结论落库 + 推送去 JSON"的通用模式。
    """
    import json
    import re
    if not text:
        return {}
    json_text = None
    block = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if block:
        json_text = block.group(1)
        start = json_text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(json_text)):
                if json_text[i] == "{":
                    depth += 1
                elif json_text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        json_text = json_text[start:i + 1]
                        break
    else:
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        json_text = text[start:i + 1]
                        break
    if not json_text:
        return {}
    try:
        return json.loads(json_text)
    except Exception:
        return {}


def _parse_and_save_recommendations(trade_date: str, report_text: str):
    """
    从 LLM 复盘报告中提取推荐标的并落库到 recommendations 表。
    优先级：1) 结构化 JSON 块（Prompt 要求 LLM 输出）2) 正则兜底匹配
    """
    items = []

    json_parsed = False  # 标记是否成功解析了 JSON（包括空数组）

    # 优先取 ```json ... ``` 代码块
    json_block = re.search(r'```json\s*(.*?)\s*```', report_text, re.DOTALL)
    json_text = None
    if json_block:
        json_text = json_block.group(1)
        # 手动提取最外层 {} 避免非贪婪匹配被嵌套对象截断
        start = json_text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(json_text)):
                if json_text[i] == "{":
                    depth += 1
                elif json_text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        json_text = json_text[start:i + 1]
                        break
    if not json_text:
        json_text = ""  # 无法提取

    if json_text:
        try:
            data = json.loads(json_text)
            recs = data.get("recommendations")
            if isinstance(recs, list):
                json_parsed = True
                for r in recs:
                    code = str(r.get("code", "")).strip()
                    if not code or len(code) != 6 or not code.isdigit():
                        continue
                    items.append({
                        "code": code,
                        "name": str(r.get("name", "")),
                        "strategy_type": str(r.get("strategy_type", "AI复盘推荐")),
                        "open_requirement": str(r.get("open_requirement", "")),
                        "auction_vol_ratio": str(r.get("auction_vol_ratio", "")),
                        "buy_condition": str(r.get("buy_condition", "")),
                        "sell_condition": str(r.get("sell_condition", ""))
                    })
            else:
                # JSON 解析成功但结构不符（缺 recommendations 列表），视为格式偏离，走正则兜底
                logger.warning("推荐 JSON 结构不符合预期（缺少 recommendations 列表），回退到正则匹配")
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 推荐解析失败，回退到正则匹配: {e}")

    # 正则兜底仅当 JSON 解析完全失败时启用，空数组说明 AI 明确判定不应买入
    if not items and not json_parsed:
        stock_pattern = re.compile(r'[一-鿿]{2,8}\s*[（(]\s*(\d{6})\s*[）)]')
        found = stock_pattern.findall(report_text)
        seen = set()
        for code in found:
            if code in seen:
                continue
            seen.add(code)
            name = DataFetcher.get_stock_name(code)
            items.append({
                "code": code,
                "name": name,
                "strategy_type": "AI复盘推荐",
                "open_requirement": "",
                "auction_vol_ratio": "",
                "buy_condition": "",
                "sell_condition": ""
            })
            if len(items) >= 5:
                break

    if items:
        RecommendationManager.add_recommendations(trade_date, items)
        logger.info(f"从复盘报告中解析并落库 {len(items)} 个推荐标的: "
                    f"{', '.join(i['name'] + '(' + i['code'] + ')' for i in items)}")
    else:
        reason = "AI判定不推荐" if json_parsed else "未检测到推荐标的代码"
        logger.info(f"复盘报告推荐标的: {reason}，跳过入库")




def _evaluate_yesterday_recommendations(trade_date: str, spot_df: pd.DataFrame = None,
                                        zt_df: pd.DataFrame = None):
    """
    LLM 复盘打分（断链8修复）：让 LLM 评估昨日推荐标的今日的实际表现，形成闭环反馈。
    - 逐标的评估：单次 LLM 调用要求输出结构化 JSON {"evaluations":[{code,score,comment}]}，
      按 code 写入各记录的 eval_note/eval_score（不再整段文本写所有记录）。
    - spot 为空不跳过：用今日涨停池兜底，保证每只推荐都有评估记录，避免"未经评估就过期"。
    - 无结构化 JSON 时退化为整段文本写 eval_note（兜底不丢数据）。
    """
    yesterday = get_previous_trading_day(
        datetime.datetime.strptime(trade_date, "%Y%m%d").date()
    )
    # 含已买入(TRIGGERED)/已过期(EXPIRED)——胜率复盘必须把实际买入的也算进去
    pending = RecommendationManager.get_recommendations_by_date(yesterday)
    if not pending:
        return

    # ---- 构建 每只推荐 今日表现（spot 优先，涨停池兜底）----
    perf: Dict[str, str] = {}
    spot_ok = spot_df is not None and not spot_df.empty
    if spot_ok:
        for r in pending:
            m = spot_df[spot_df["code"].astype(str) == str(r["code"])]
            if not m.empty:
                row = m.iloc[0]
                perf[r["code"]] = (f"今日涨幅 {row.get('change_pct', 0)}%, "
                                   f"现价 {row.get('price', 0)}元")
            else:
                perf[r["code"]] = "未获取到当日行情"
    else:
        # spot 为空 → 用今日涨停池兜底（至少知道哪些涨停），不跳过整体评估
        try:
            today_zt = zt_df if (zt_df is not None and not zt_df.empty) \
                else DataFetcher.get_zt_pool(date_str=trade_date)
            zt_codes = set(today_zt["code"].astype(str)) if today_zt is not None and not today_zt.empty else set()
        except Exception:
            zt_codes = set()
        for r in pending:
            perf[r["code"]] = "今日涨停" if str(r["code"]) in zt_codes else "未获取到完整行情(spot为空)"

    # ---- 单次 LLM 结构化逐票评估 ----
    lines = ["逐只评估昨日推荐标的今日表现，并给出胜率评分与一句话点评。"]
    for r in pending:
        lines.append(f"- {r['name']}({r['code']}) [{r['strategy_type']}]: {perf.get(r['code'], '')}")
    eval_text = "\n".join(lines)

    score_prompt = (
        "你是短线交易复盘专家。对昨日推荐的每只标的，基于今日表现给出胜率评分(0-100，越高越准)与一句话点评(≤30字)。\n"
        "必须只输出 JSON，不要其他文字，格式：\n"
        '{"evaluations": [{"code": "001208", "score": 80, "comment": "点评"}]}\n'
        "逐只覆盖全部推荐标的，不得遗漏。"
    )
    try:
        evaluation = llm_client.generate(
            system_prompt=score_prompt,
            user_prompt=eval_text,
            module="recommendation_eval"
        )
        logger.info(f"昨日推荐复盘评估: {str(evaluation)[:200]}")
    except Exception as e:
        logger.warning(f"LLM 复盘打分失败: {e}")
        evaluation = ""

    # ---- 落库：逐标的写 eval_note/eval_score；无结构化则整段兜底 ----
    session = db_manager.get_session()
    try:
        evals: Dict[str, dict] = {}
        data = extract_json_block(evaluation) if evaluation else {}
        if data and data.get("evaluations"):
            for e in data["evaluations"]:
                code = str(e.get("code", "")).strip()
                if code:
                    evals[code] = e
        for rec in pending:
            row = session.query(Recommendation).filter(
                Recommendation.id == rec["id"]).first()
            if not row:
                continue
            e = evals.get(str(rec["code"]))
            if e:
                row.eval_note = str(e.get("comment", ""))[:500]
                try:
                    row.eval_score = int(e.get("score"))
                except (TypeError, ValueError):
                    pass
            else:
                row.eval_note = (evaluation or "评估失败")[:500]  # 兜底
        session.commit()
        logger.info(f"推荐评估落库完成: {len(pending)} 条")
    except Exception as e2:
        session.rollback()
        logger.warning(f"推荐评估落库失败: {e2}")
    finally:
        session.close()




def _compute_yidong_bravery(today_str: str, today_zt_df) -> float:
    """
    计算"破规胆量"维度：昨日高位连板(>=3板)的股票中，今日仍在涨停池的比例。
    这反映资金在监管压力下继续做多的勇气。
    无数据时返回默认50%（中性）。
    """
    try:
        yesterday = get_previous_trading_day(
            datetime.datetime.strptime(today_str, "%Y%m%d").date()
        )
        yesterday_zt = DataFetcher.get_zt_pool(date_str=yesterday)
        if yesterday_zt is None or yesterday_zt.empty or "lbc" not in yesterday_zt.columns:
            return 50.0
        if today_zt_df is None or today_zt_df.empty:
            return 50.0

        # 昨日连板>=3的高位股
        high_lbc = yesterday_zt[yesterday_zt["lbc"].astype(int) >= 3]
        if high_lbc.empty:
            return 50.0

        high_codes = set(high_lbc["code"].astype(str))
        today_zt_codes = set(today_zt_df["code"].astype(str))

        # 今日仍在涨停池 = 晋级成功
        promoted = high_codes & today_zt_codes
        rate = len(promoted) / len(high_codes) * 100
        logger.info(f"破规胆量: 昨日高位{len(high_codes)}只, 今日晋级{len(promoted)}只, 胆量{rate:.1f}%")
        return round(rate, 1)
    except Exception as e:
        logger.warning(f"计算破规胆量失败: {e}")
        return 50.0




def _auto_populate_dragons(trade_date: str, zt_df):
    """
    将当日连板 >= 3 的高标自动写入 HistoricDragon 表，
    供二波战法在过去 30 天内溯源人气总龙头。

    去重规则：同一 code 只保留一条活跃记录，新数据的连板数更高时更新峰值。
    """
    if zt_df is None or zt_df.empty or "lbc" not in zt_df.columns:
        return
    try:
        session = db_manager.get_session()
        try:
            # 30 天前的龙头标记为失效
            cutoff = (datetime.datetime.strptime(trade_date, "%Y%m%d") -
                      datetime.timedelta(days=30)).strftime("%Y%m%d")
            session.query(HistoricDragon).filter(
                HistoricDragon.peak_date < cutoff
            ).update({"is_active": False}, synchronize_session="fetch")

            highs = zt_df[zt_df["lbc"].astype(int) >= 3]
            updated_count = 0
            for _, row in highs.iterrows():
                code = str(row.get("code", ""))
                lbc = int(row.get("lbc", 3))
                name = str(row.get("name", ""))
                peak_price = float(row.get("price", 0))
                industry = str(row.get("industry", "")) if "industry" in row.index else ""

                existing = session.query(HistoricDragon).filter(
                    HistoricDragon.code == code,
                    HistoricDragon.is_active == True
                ).first()

                if existing:
                    # 同一只股票已在表中：连板数更高时更新峰值
                    if lbc > existing.max_lbc:
                        existing.max_lbc = lbc
                        existing.peak_date = trade_date
                        existing.peak_price = peak_price
                        existing.name = name
                        existing.board_name = industry or existing.board_name
                        existing.is_active = True
                        updated_count += 1
                    else:
                        # 连板数持平或更低，只刷新 active 状态
                        existing.is_active = True
                else:
                    # 新龙头
                    session.add(HistoricDragon(
                        code=code, name=name, max_lbc=lbc,
                        peak_date=trade_date, peak_price=peak_price,
                        board_name=industry, is_active=True
                    ))
                    updated_count += 1
            session.commit()
            logger.info(f"历史龙头表已更新，新增/更新 {updated_count} 只高标，失效旧记录")
        except Exception as e:
            session.rollback()
            logger.warning(f"历史龙头表自动填充失败: {e}")
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"历史龙头表自动填充异常: {e}")
