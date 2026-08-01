import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from llm.pre_market import PreMarketAnalyzer
from llm.call_auction import CallAuctionAnalyzer
from llm.post_market import PostMarketAnalyzer
from core.strategies import MarketStyle
from notifier.bark import bark_notifier
from core.trade_calendar import is_trading_day, is_last_non_trading_day
from database.services import RecommendationManager, SentimentManager, HoldingManager

logger = logging.getLogger(__name__)

# 模块级缓存：盘前简报预测结果，供竞价观察读取
_cached_pre_market_report: str = ""


def job_pre_market():
    """
    08:30 盘前简报定时任务。非交易日自动跳过。
    """
    if not is_trading_day():
        logger.info("今日非交易日，跳过盘前简报")
        return

    global _cached_pre_market_report
    logger.info(">>> 触发 08:30 盘前简报定时任务...")
    try:
        report = PreMarketAnalyzer.run_report()
        _cached_pre_market_report = report  # 缓存供 09:26 竞价使用（修复 #2）
        # 发送 Bark 推送
        bark_notifier.send(
            title="☀️ 盘前简报与爆发题材预测",
            body=report,
            group="盘前简报"
        )
    except Exception as e:
        logger.error(f"08:30 盘前简报执行异常: {e}")


def job_call_auction():
    """
    09:26 竞价观察与指令定时任务。非交易日自动跳过。
    """
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
        pending_recs = RecommendationManager.get_pending_recommendations(trade_date=today_str)
        lines = []
        if pending_recs:
            for r in pending_recs:
                lines.append(f"- [复盘推荐] {r['name']}({r['code']}): {r['strategy_type']}"
                             f" | 要求: {r['open_requirement']}")

        # ---- 2. 昨日涨停池中连板/首板标的（修复 #1）----
        try:
            yesterday_zt = DataFetcher.get_zt_pool(date_str=yesterday_str)
            if yesterday_zt is not None and not yesterday_zt.empty and "lbc" in yesterday_zt.columns:
                # 取连板>=2的高标和首板，按连板数降序
                zt_obs = yesterday_zt[yesterday_zt["lbc"].astype(int) >= 1].head(15)
                for _, zrow in zt_obs.iterrows():
                    zcode = str(zrow.get("code", ""))
                    zname = str(zrow.get("name", ""))
                    zlbc = int(zrow.get("lbc", 1))
                    tag = f"{zlbc}连板" if zlbc >= 2 else "首板"
                    lines.append(f"- [昨涨停-{tag}] {zname}({zcode})")
        except Exception as e:
            logger.warning(f"获取昨日涨停池连板/首板标的失败: {e}")

        recs_summary = "\n".join(lines) if lines else "暂无待观察标的"

        # ---- 3. 盘前简报预测板块（修复 #2）----
        global _cached_pre_market_report
        predicted_summary = _cached_pre_market_report[:800] if _cached_pre_market_report else ""

        # ---- 4. 抓取实时竞价快照 ----
        spot_df = DataFetcher.get_realtime_spot()

        # ---- 5. LLM 竞价观察指令 ----
        # 获取昨日涨停溢价，用于竞价风控
        premium_info = DataFetcher.get_yesterday_zt_premium()
        result = CallAuctionAnalyzer.run_auction_analysis(
            trade_date=today_str,
            auction_df=spot_df,
            yesterday_zt_auction_yield=premium_info.get("intraday_premium", 1.5),
            recommended_targets_summary=recs_summary,
            predicted_sectors_summary=predicted_summary
        )

        bark_notifier.send(
            title="🎯 09:26 竞价超预期指令",
            body=result,
            group="竞价指令",
            level="timeSensitive"
        )
    except Exception as e:
        logger.error(f"09:26 竞价观察执行异常: {e}")


def job_post_market():
    """
    15:30 盘后复盘定时任务。非交易日自动跳过。
    """
    if not is_trading_day():
        logger.info("今日非交易日，跳过盘后复盘")
        return

    logger.info(">>> 触发 15:30 盘后深度复盘定时任务...")
    try:
        today_str = datetime.datetime.now().strftime("%Y%m%d")

        zt_df = DataFetcher.get_zt_pool(date_str=today_str)
        zhaban_df = DataFetcher.get_zhaban_pool(date_str=today_str)
        dt_df = DataFetcher.get_dt_pool(date_str=today_str)
        spot_df = DataFetcher.get_realtime_spot()
        lhb_df = DataFetcher.get_lhb_detail(date_str=today_str)
        lhb_seats_df = DataFetcher.get_lhb_seats(date_str=today_str)  # 营业部级席位数据

        # 排序成交额 Top 20
        top_amount_df = spot_df.sort_values(by="amount", ascending=False).head(20) if not spot_df.empty else None

        # ---- 盘后市场风格判定（在复盘前执行，传给 LLM 作为上下文）----
        from core.emotion_index import EmotionVector
        # 获取真实溢价和成交额，替换默认值
        premium_data = DataFetcher.get_yesterday_zt_premium()
        total_amount = float(spot_df["amount"].sum()) if not spot_df.empty else 8e11

        # 计算"破规胆量"：昨日连板>=3且3日偏离度接近红线的股票中，今日仍涨停的比例
        yidong_bravery = _compute_yidong_bravery(today_str, zt_df)

        emotion_res = EmotionVector.calculate(
            zt_df=zt_df, zhaban_df=zhaban_df, dt_df=dt_df,
            total_market_amount=total_amount,
            yesterday_zt_avg_premium=premium_data.get("intraday_premium", 1.5),
            yidong_stocks_next_day_promoted_rate=yidong_bravery
        )
        total_amount_billion = total_amount / 1e8
        bl = DataFetcher.get_adaptive_baseline()
        baseline = bl["ma_amount"]
        market_style = MarketStyle.classify(emotion_res,
                                            market_amount=total_amount_billion,
                                            baseline=baseline)

        # 情绪周期状态机：基于前一交易日的周期状态和今日数据判定当前所处阶段
        from core.cycle_machine import EmotionCycleMachine
        recent_sentiments = SentimentManager.get_recent_sentiments(days_lookback=3)
        yesterday_phase = recent_sentiments[0]["cycle_stage"] if recent_sentiments else "冰点"
        cycle_result = EmotionCycleMachine.determine_phase(emotion_res, yesterday_phase)
        cycle_stage = cycle_result["phase"]
        cycle_reason = cycle_result["reason"]

        style_reason = market_style.get("reason", "")
        k_factor = market_style.get("capacity_factor", 1.0)
        logger.info(
            f"盘后周期判定: [{cycle_stage}] {cycle_reason} | "
            f"风格:{market_style.get('style')} K={k_factor:.2f}(今日{total_amount:.0f}亿/均{baseline:.0f}亿) | "
            f"涨停{emotion_res['zt_count']}/跌停{emotion_res['dt_count']}/"
            f"溢价{emotion_res['yield_rate']}%/情绪{emotion_res['sentiment_index']}"
        )

        # 周期转换时发送 Bark 通知
        if cycle_result["transition"]:
            stance = EmotionCycleMachine.get_trading_stance(cycle_stage)
            bark_notifier.send(
                title=f"🔄 [周期转换] {cycle_result['yesterday']} → {cycle_stage}",
                body=f"{cycle_reason}\n操作建议: {stance['stance']} - {stance['desc']}",
                group="情绪周期",
                level="timeSensitive"
            )

        # 生成深度复盘分析（传入风格判定供 LLM 参考）
        style_info = (
            f"周期={cycle_stage}({cycle_reason}) "
            f"风格={market_style.get('style')} K={k_factor:.2f}(今日{total_amount:.0f}亿/均{baseline:.0f}亿) "
            f"推荐战法={market_style.get('priority_strategy', '')} {style_reason}"
        )
        review_report = ""
        try:
            review_report = PostMarketAnalyzer.run_review(
                trade_date=today_str,
                zt_df=zt_df,
                zhaban_df=zhaban_df,
                dt_df=dt_df,
                top_amount_df=top_amount_df,
                lhb_df=lhb_df,
                spot_df=spot_df,
                lhb_seats_df=lhb_seats_df,
                yesterday_zt_yield=premium_data.get("intraday_premium", 1.5),
                market_style_info=style_info,
                precomputed_emotion=emotion_res
            )
        except Exception as e:
            logger.warning(f"LLM 复盘生成失败: {e}，使用定量数据兜底")
            review_report = (
                f"⚠️ LLM 服务不可用，以下为定量数据摘要：\n\n"
                f"风格判定: {cycle_stage} | {style_reason}\n"
                f"情绪分: {emotion_res['sentiment_index']} | "
                f"涨停{emotion_res['zt_count']}/跌停{emotion_res['dt_count']}/"
                f"炸板{emotion_res['zhaban_count']}({emotion_res['zhaban_rate']}%)\n"
                f"最高连板: {emotion_res['height']}板 | "
                f"昨日涨停溢价: {emotion_res['yield_rate']}%\n"
                f"容量因子: K={k_factor:.2f} | "
                f"今日成交: {total_amount_billion:.0f}亿 | 基准: {baseline:.0f}亿\n"
                f"推荐战法: {market_style.get('priority_strategy', '')}"
            )

        # ---- 落库：每日情绪向量 ----
        SentimentManager.save_daily_sentiment(
            trade_date=today_str,
            sentiment_data=emotion_res,
            cycle_stage=cycle_stage,
            summary=style_reason + "\n" + review_report[:400],
            total_amount=total_amount_billion
        )
        logger.info(f"情绪向量已落库 ({today_str})，风格: {cycle_stage} K={k_factor:.2f}")


        # ---- 落库：推荐标的 ----
        _parse_and_save_recommendations(today_str, review_report)

        # ---- LLM 复盘打分：评估昨日推荐胜率 ----
        _evaluate_yesterday_recommendations(today_str, spot_df)

        # ---- 自动填充历史龙头表 ----
        _auto_populate_dragons(today_str, zt_df)

        # 发送 Bark 推送
        bark_notifier.send(
            title="🌙 盘后深度复盘与次日交易指南",
            body=review_report,
            group="盘后复盘"
        )

        # ---- 策略回测报告 ----
        _generate_backtest_report(today_str)

    except Exception as e:
        logger.error(f"15:30 盘后复盘执行异常: {e}")


def _generate_backtest_report(trade_date: str):
    """盘后实盘回测报告：从 AI 自动交易的平仓记录中统计真实盈亏"""
    from database.services import db_manager
    from database.models import Holding
    import datetime

    session = db_manager.get_session()
    try:
        closed = session.query(Holding).filter(
            Holding.status == "CLOSED",
            Holding.holding_type == "AI_AUTO",
            Holding.sell_price > 0
        ).order_by(Holding.updated_at.desc()).limit(200).all()

        if not closed:
            session.close()
            return

        lines = ["📊 AI实盘回测报告", f"共 {len(closed)} 笔已平仓交易", ""]

        total_trades = 0
        wins = 0
        returns = []
        stock_returns: Dict[str, List[float]] = {}
        stock_names: Dict[str, str] = {}

        for h in closed:
            if h.cost_price <= 0 or h.sell_price <= 0:
                continue
            ret = round((h.sell_price - h.cost_price) / h.cost_price * 100, 2)
            total_trades += 1
            if ret > 0:
                wins += 1
            returns.append(ret)
            stock_returns.setdefault(h.code, []).append(ret)
            stock_names[h.code] = h.name

        if total_trades == 0:
            session.close()
            return

        avg_ret = round(sum(returns) / len(returns), 2)
        win_rate = round(wins / total_trades * 100, 2)

        cumulative = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in returns:
            cumulative *= (1 + r / 100)
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / peak * 100
            max_dd = max(max_dd, dd)

        lines.append(f"胜率: {win_rate}% | 均收益: {avg_ret}% | 最大回撤: {round(max_dd, 2)}%")
        lines.append(f"总收益: {round((cumulative-1)*100, 2)}%")

        # 个股排行
        stock_avg = [(c, round(sum(v) / len(v), 2), len(v), stock_names.get(c, c))
                     for c, v in stock_returns.items() if len(v) >= 1]
        stock_avg.sort(key=lambda x: x[1], reverse=True)
        if stock_avg:
            lines.append("")
            lines.append(f"━━━ 个股收益排行（{len(stock_avg)}只）━━━")
            top5 = stock_avg[:5]
            bottom5 = stock_avg[-5:] if len(stock_avg) >= 5 else []
            lines.append("🏆 Top5:")
            for c, ret, cnt, n in top5:
                lines.append(f"  {n}({c}): +{ret}% ({cnt}次)")
            if bottom5:
                lines.append("💀 Bottom5:")
                for c, ret, cnt, n in bottom5:
                    lines.append(f"  {n}({c}): {ret}% ({cnt}次)")

        session.close()
        body = "\n".join(lines)
        logger.info(f"AI实盘回测报告:\n{body}")
        bark_notifier.send(
            title="📊 AI实盘回测报告",
            body=body,
            group="回测报告",
            level="passive"
        )
    except Exception as e:
        session.close()
        logger.warning(f"实盘回测报告生成失败: {e}")


def _parse_and_save_recommendations(trade_date: str, report_text: str):
    """
    从 LLM 复盘报告中提取推荐标的并落库到 recommendations 表。
    优先级：1) 结构化 JSON 块（Prompt 要求 LLM 输出）2) 正则兜底匹配
    """
    import re
    import json
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
            json_parsed = True
            recs = data.get("recommendations", [])
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


def _evaluate_yesterday_recommendations(trade_date: str, spot_df: pd.DataFrame = None):
    """
    LLM 复盘打分：让 LLM 评估昨日推荐标的今日的实际表现，形成闭环反馈。
    评分存入 recommendations 表的 sell_condition 字段（复用为评分备注）。
    """
    from core.trade_calendar import get_previous_trading_day
    yesterday = get_previous_trading_day(
        datetime.datetime.strptime(trade_date, "%Y%m%d").date()
    )
    pending = RecommendationManager.get_pending_recommendations(trade_date=yesterday)
    if not pending or spot_df is None or spot_df.empty:
        return

    # 拼昨日推荐 + 今日表现的评估 Prompt
    lines = ["评估昨日推荐标的今日表现："]
    for r in pending:
        code = r["code"]
        match = spot_df[spot_df["code"].astype(str) == str(code)]
        if not match.empty:
            row = match.iloc[0]
            lines.append(
                f"- {r['name']}({code}) [{r['strategy_type']}]: "
                f"今日涨幅 {row.get('change_pct', 0)}%, 现价 {row.get('price', 0)}元"
            )
        else:
            lines.append(f"- {r['name']}({code}) [{r['strategy_type']}]: 未找到今日数据")
    eval_text = "\n".join(lines)

    try:
        score_prompt = "你是短线交易复盘专家。请用一句话评估昨日推荐标的今天的表现（不超过50字），并给出胜率评分（0-100）。"
        evaluation = llm_client.generate(
            system_prompt=score_prompt,
            user_prompt=eval_text,
            module="recommendation_eval"
        )
        logger.info(f"昨日推荐复盘评估: {evaluation}")
    except Exception as e:
        logger.warning(f"LLM 复盘打分失败: {e}")


def _compute_yidong_bravery(today_str: str, today_zt_df) -> float:
    """
    计算"破规胆量"维度：昨日高位连板(>=3板)的股票中，今日仍在涨停池的比例。
    这反映资金在监管压力下继续做多的勇气。
    无数据时返回默认50%（中性）。
    """
    try:
        from core.trade_calendar import get_previous_trading_day
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
        from database.services import db_manager
        from database.models import HistoricDragon
        import datetime
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


def job_holiday_news_summary():
    """
    节假日/周末最后一天 20:00 执行。
    汇总休市期间的重要新闻，让开盘前有充足的信息准备。
    """
    if not is_last_non_trading_day():
        logger.info("今日非假期最后一天，跳过假日消息汇总")
        return

    logger.info(">>> 触发假日消息汇总（假期最后一天 20:00）...")
    try:
        # 拉取近期新闻
        news_items = NewsFetcher.get_cls_news(limit=30)
        hot_words = NewsFetcher.get_hot_search_words(limit=20)

        news_text = "\n".join(
            [f"- [{item.get('time', '')}] {item.get('title', '')}: {item.get('content', '')[:120]}..."
             for item in news_items]
        ) if news_items else "暂无新闻快讯"
        hot_text = ", ".join(hot_words) if hot_words else "暂无热搜"

        system_prompt = """你是A股策略分析师。下方是休市期间的重要新闻和热搜，请汇总为一份简洁的开盘前简报。
格式要求：
1. 最重要的 3-5 条政策/行业新闻（标题+一句话解读）
2. 可能影响的开盘板块和方向判断
3. 总体情绪评估（偏多/偏空/中性）
字数不超过300字。
"""
        user_prompt = f"""休市期间新闻快讯：
{news_text}

热搜关键词：
{hot_text}

请汇总为开盘前简报。"""

        summary = llm_client.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            module="holiday_summary"
        )

        bark_notifier.send(
            title="📰 假日消息汇总 | 开盘前简报",
            body=summary,
            group="假日汇总",
            level="timeSensitive"
        )
        logger.info("假日消息汇总完成，已推送")
    except Exception as e:
        logger.error(f"假日消息汇总执行异常: {e}")
