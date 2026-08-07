import datetime
import logging
import threading
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from notifier.bark import bark_notifier
from core.trade_calendar import is_trading_day, is_last_non_trading_day

logger = logging.getLogger(__name__)
from scheduler.helpers import _record_job_run, _parse_and_save_recommendations, _evaluate_yesterday_recommendations, _compute_yidong_bravery, _auto_populate_dragons
from scheduler.reporting import _push_daily_pnl_report
from database import SentimentManager, MarketIndexManager, ZtPoolManager, SectorStrengthManager, HoldingManager
from llm.post_market import PostMarketAnalyzer
from core.strategies import MarketStyle
from core.emotion_index import EmotionVector
from core.cycle_machine import EmotionCycleMachine


def _alert_kline_fail(what: str, detail: str) -> None:
    """日线增量同步失败 Bark 告警（复用看门狗「系统告警」风格；bark 自身异常不打断复盘）"""
    try:
        bark_notifier.send(
            title="⚠️ [日线缓存同步失败] " + what,
            body=detail,
            group="系统告警",
            level="timeSensitive",
        )
    except Exception:
        pass


def _sync_kline_incremental() -> None:
    """盘后日线增量同步线程体：惰性 import 按需加载 kline_etl（长驻进程免重启热更新），
    串行摊速模式（零并发）——从启动(约15:30)逐只摊速到 KLINE_ETL_PACED_DEADLINE(默认23:30) 前完成，
    源 QPS 最低防限流。进度每 100 只通过 _record_job_run 落库供看板实时显示；
    异常或空 universe 时记 error 并发 Bark 告警，线程内异常不抛回主线程。"""
    def _on_progress(pulled=None, total=None, done=False):
        _record_job_run("job_kline_sync", "日线同步",
                        state="完成" if done else "运行中",
                        progress=f"{pulled or 0}/{total or 0}" if total else "")
    try:
        from data.kline_etl import KlineEtl
        result = KlineEtl.run_incremental_paced(on_progress=_on_progress)
        if isinstance(result, dict) and result.get("error"):
            logger.error(f"日线增量同步未执行: {result['error']}")
            _record_job_run("job_kline_sync", "日线同步", state="失败", progress=result["error"])
            _alert_kline_fail("增量同步未执行", result["error"])
        else:
            _record_job_run("job_kline_sync", "日线同步", state="完成",
                            progress=f"{result.get('pulled', 0)}/{result.get('universe', 0)}")
            logger.info(f"日线增量同步完成: {result}")
    except Exception as e:
        logger.error(f"日线增量同步执行异常: {e}")
        _record_job_run("job_kline_sync", "日线同步", state="失败", progress=str(e)[:120])
        _alert_kline_fail("增量同步执行异常", str(e))


def job_kline_sync():
    """
    15:30 日线同步独立定时任务（不再挂在 18:01 复盘里）：串行摊速拉取全市场日线，
    到 KLINE_ETL_PACED_DEADLINE(默认23:30) 前完成。非交易日自动跳过。
    任务状态（运行中/完成/失败 + 进度）通过 _record_job_run 落库，看板"定时任务"实时显示。
    """
    _record_job_run("job_kline_sync", "日线同步", state="运行中")
    if not is_trading_day():
        logger.info("今日非交易日，跳过日线同步")
        _record_job_run("job_kline_sync", "日线同步", state="跳过")
        return
    logger.info(">>> 触发 15:30 日线同步定时任务...")
    # 后台线程串行摊速（长任务，不阻塞调度线程；供 mode=signals 回测用最新数据）
    try:
        threading.Thread(target=_sync_kline_incremental, daemon=True,
                         name="kline-sync").start()
    except Exception as e:
        logger.error(f"日线同步启动失败: {e}")
        _record_job_run("job_kline_sync", "日线同步", state="失败", progress=str(e)[:100])
        _alert_kline_fail("同步启动失败", str(e))


def job_post_market():
    """
    18:01 盘后复盘定时任务。非交易日自动跳过。
    （日线同步已拆分为独立 15:30 任务 job_kline_sync，不再挂在本任务内）
    """
    _record_job_run("job_post_market", "盘后深度复盘")
    if not is_trading_day():
        logger.info("今日非交易日，跳过盘后复盘")
        return

    logger.info(">>> 触发 18:01 盘后深度复盘定时任务...")
    try:
        today_str = datetime.datetime.now().strftime("%Y%m%d")

        zt_df = DataFetcher.get_zt_pool(date_str=today_str)
        zhaban_df = DataFetcher.get_zhaban_pool(date_str=today_str)
        dt_df = DataFetcher.get_dt_pool(date_str=today_str)
        spot_df = DataFetcher.get_realtime_spot()
        lhb_df = DataFetcher.get_lhb_detail(date_str=today_str)
        lhb_seats_df = DataFetcher.get_lhb_seats(date_str=today_str)  # 营业部级席位数据

        # 龙虎榜席位画像同步（行为分类自动更新，供席位"神韵"分析）
        try:
            from database import SeatProfileManager
            SeatProfileManager.sync_from_lhb(lhb_seats_df, today_str)
        except Exception as e:
            logger.warning(f"龙虎榜席位画像同步失败: {e}")

        # 排序成交额 Top 20
        top_amount_df = spot_df.sort_values(by="amount", ascending=False).head(20) if not spot_df.empty else None

        # ---- 盘后市场风格判定（在复盘前执行，传给 LLM 作为上下文）----
        # 获取真实溢价和成交额，替换默认值
        premium_data = DataFetcher.get_yesterday_zt_premium()
        # 使用未过滤全市场成交额（对齐券商软件口径），get_realtime_spot() 已在 L37 写入缓存
        total_amount = DataFetcher.get_market_total_amount()
        if not total_amount or total_amount <= 0:
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
        recent_sentiments = SentimentManager.get_recent_sentiments(days_lookback=3)
        yesterday_phase = recent_sentiments[0]["cycle_stage"] if recent_sentiments else "冰点"
        cycle_result = EmotionCycleMachine.determine_phase(emotion_res, yesterday_phase)
        cycle_stage = cycle_result["phase"]
        cycle_reason = cycle_result["reason"]

        style_reason = market_style.get("reason", "")
        k_factor = market_style.get("capacity_factor", 1.0)
        logger.info(
            f"盘后周期判定: [{cycle_stage}] {cycle_reason} | "
            f"风格:{market_style.get('style')} K={k_factor:.2f}(今日{total_amount_billion:.0f}亿/均{baseline:.0f}亿) | "
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
            f"风格={market_style.get('style')} K={k_factor:.2f}(今日{total_amount_billion:.0f}亿/均{baseline:.0f}亿) "
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
            logger.warning(f"LLM 复盘生成异常: {e}")

        if not review_report:
            # llm_client.generate 在全部重试与备用模型失败后返回空串而非抛异常，需显式兜底
            logger.warning("LLM 复盘内容为空，使用定量数据兜底")
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

        # ---- LLM 复盘打分：评估昨日推荐胜率（spot 空时用 zt_df 兜底，不跳过）----
        _evaluate_yesterday_recommendations(today_str, spot_df, zt_df)

        # ---- 自动填充历史龙头表 ----
        _auto_populate_dragons(today_str, zt_df)

        # 发送 Bark 推送（bark 层统一去掉 LLM 结构化 JSON 块，只推用户可读报告）
        bark_notifier.send(
            title="🌙 盘后深度复盘与次日交易指南",
            body=review_report,
            group="盘后复盘"
        )

        # ---- 大盘指数落库 ----
        MarketIndexManager.save_daily_index(today_str, spot_df, total_amount_yuan=total_amount)

        # ---- 涨停池明细落库 ----
        ZtPoolManager.save_daily_zt_pool(today_str, zt_df)

        # ---- 板块强度落库 ----
        SectorStrengthManager.save_daily_sectors(today_str, zt_df)

        # ---- 板块情绪周期计算（主线板块识别 + 板块阶段）----
        try:
            from database import SectorCycleManager
            SectorCycleManager.sync_from_zt_pool(today_str, zt_df)
        except Exception as e:
            logger.warning(f"板块周期同步失败: {e}")

        # ---- 概念情绪周期计算（切片3：概念主线识别，非题材标签已过滤）----
        try:
            from database import ConceptCycleManager
            ConceptCycleManager.sync_from_zt_pool(today_str, zt_df)
        except Exception as e:
            logger.warning(f"概念周期同步失败: {e}")

        # ---- 每日盈亏报告（同步收盘价 + 净值快照 + 推送） ----
        _push_daily_pnl_report(today_str, spot_df)

    except Exception as e:
        logger.error(f"18:01 盘后复盘执行异常: {e}")


