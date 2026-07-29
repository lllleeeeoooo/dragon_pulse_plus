import datetime
import logging
from typing import Dict, Any, List

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from llm.pre_market import PreMarketAnalyzer
from llm.call_auction import CallAuctionAnalyzer
from llm.post_market import PostMarketAnalyzer
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
        yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y%m%d")

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
        result = CallAuctionAnalyzer.run_auction_analysis(
            trade_date=today_str,
            auction_df=spot_df,
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

        # 生成深度复盘分析
        review_report = PostMarketAnalyzer.run_review(
            trade_date=today_str,
            zt_df=zt_df,
            zhaban_df=zhaban_df,
            dt_df=dt_df,
            top_amount_df=top_amount_df,
            lhb_df=lhb_df,
            spot_df=spot_df,  # 修复 #4：传入全市场快照用于胜率复盘
            lhb_seats_df=lhb_seats_df  # 传入营业部级席位数据
        )

        # ---- 落库：每日情绪向量 ----
        from core.emotion_index import EmotionVector
        emotion_res = EmotionVector.calculate(
            zt_df=zt_df,
            zhaban_df=zhaban_df,
            dt_df=dt_df
        )
        cycle_stage = "未判定"
        for keyword in ["高潮期", "退潮期", "冰点期", "启动期", "发酵期"]:
            if keyword in review_report:
                cycle_stage = keyword
                break
        SentimentManager.save_daily_sentiment(
            trade_date=today_str,
            sentiment_data=emotion_res,
            cycle_stage=cycle_stage,
            summary=review_report[:500]
        )
        logger.info(f"情绪向量数据已落库 ({today_str})，周期: {cycle_stage}")

        # ---- 落库：推荐标的 ----
        _parse_and_save_recommendations(today_str, review_report)

        # ---- 自动填充历史龙头表（修复 #5）----
        _auto_populate_dragons(today_str, zt_df)

        # 发送 Bark 推送
        bark_notifier.send(
            title="🌙 盘后深度复盘与次日交易指南",
            body=review_report,
            group="盘后复盘"
        )
    except Exception as e:
        logger.error(f"15:30 盘后复盘执行异常: {e}")


def _parse_and_save_recommendations(trade_date: str, report_text: str):
    """
    从 LLM 复盘报告中提取推荐标的并落库到 recommendations 表。
    优先级：1) 结构化 JSON 块（Prompt 要求 LLM 输出）2) 正则兜底匹配
    """
    import re
    import json
    items = []

    json_match = re.search(r'```json\s*(\{.*?\})\s*```', report_text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'\{[^{]*"recommendations"\s*:\s*\[.*?\][^{]*\}', report_text, re.DOTALL)

    if json_match:
        try:
            data = json.loads(json_match.group(1) if json_match.lastindex else json_match.group(0))
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

    if not items:
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
        logger.info("复盘报告中未检测到明确的推荐标的代码")


def _auto_populate_dragons(trade_date: str, zt_df):
    """
    修复 #5：将当日连板 >= 3 的高标自动写入 HistoricDragon 表，
    供二波战法在过去 30 天内溯源人气总龙头。
    """
    if zt_df is None or zt_df.empty or "lbc" not in zt_df.columns:
        return
    try:
        from database.services import db_manager
        from database.models import HistoricDragon
        session = db_manager.get_session()
        try:
            highs = zt_df[zt_df["lbc"].astype(int) >= 3]
            for _, row in highs.iterrows():
                code = str(row.get("code", ""))
                lbc = int(row.get("lbc", 3))
                name = str(row.get("name", ""))
                peak_price = float(row.get("price", 0))
                industry = str(row.get("industry", "")) if "industry" in row.index else ""

                # 避免重复：同日同代码已存在则跳过
                exists = session.query(HistoricDragon).filter(
                    HistoricDragon.code == code,
                    HistoricDragon.peak_date == trade_date
                ).first()
                if not exists:
                    session.add(HistoricDragon(
                        code=code,
                        name=name,
                        max_lbc=lbc,
                        peak_date=trade_date,
                        peak_price=peak_price,
                        board_name=industry,
                        is_active=True
                    ))
            session.commit()
            logger.info(f"历史龙头表已更新，录入 {len(highs)} 只高标")
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
