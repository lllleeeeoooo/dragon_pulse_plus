import logging
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd

from llm.client import llm_client
from config.prompt_templates import POST_MARKET_SYSTEM_PROMPT, POST_MARKET_USER_TEMPLATE
from core.emotion_index import EmotionVector
from core.seat_analyzer import SeatAnalyzer
from core.regulatory_yidong import RegulatoryYidongCalculator
from core.core_pool import ActiveCorePool
from database.services import RecommendationManager, SentimentManager

logger = logging.getLogger(__name__)


class PostMarketAnalyzer:
    """
    盘后深度复盘分析器
    封装盘后数据的清洗、格式化与 LLM 分析生成
    """

    @classmethod
    def _get_recent_index_pct(cls) -> Tuple[float, float]:
        """
        获取上证指数近 3 日和近 10 日涨跌幅，用于偏离度计算
        :return: (index_3d_pct, index_10d_pct)
        """
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol="sh000001")
            if df is not None and not df.empty:
                closes = pd.to_numeric(df["close"], errors="coerce").dropna()
                if len(closes) >= 11:
                    latest = closes.iloc[-1]
                    idx_3d = round((latest - closes.iloc[-4]) / closes.iloc[-4] * 100, 2)
                    idx_10d = round((latest - closes.iloc[-11]) / closes.iloc[-11] * 100, 2)
                    return idx_3d, idx_10d
        except Exception as e:
            logger.warning(f"获取大盘指数涨跌数据失败: {e}")
        return 0.0, 0.0

    @classmethod
    def _get_stock_recent_pct(cls, code: str) -> Tuple[float, float]:
        """
        拉取个股近 10 天日 K 线，计算实际 3 日和 10 日累计涨跌幅。
        失败时返回 0, 0（兜底，不影响主流程）。
        """
        try:
            from data.fetcher import DataFetcher
            import akshare as ak
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="", end_date="", adjust="qfq")
            if df is None or df.empty:
                return 0.0, 0.0
            closes = pd.to_numeric(df["收盘"], errors="coerce").dropna()
            if len(closes) < 3:
                return 0.0, 0.0
            latest = closes.iloc[-1]
            # 3 日累计
            if len(closes) >= 4:
                real_3d = round((latest - closes.iloc[-4]) / closes.iloc[-4] * 100, 2)
            else:
                real_3d = 0.0
            # 10 日累计
            if len(closes) >= 11:
                real_10d = round((latest - closes.iloc[-11]) / closes.iloc[-11] * 100, 2)
            else:
                real_10d = round((latest - closes.iloc[0]) / closes.iloc[0] * 100, 2)
            return real_3d, real_10d
        except Exception:
            return 0.0, 0.0

    @classmethod
    def _detect_leading_sectors(cls, zt_df: pd.DataFrame) -> List[str]:
        """从涨停池聚类主线板块：涨停数量 Top 5 行业"""
        if zt_df is None or zt_df.empty or "industry" not in zt_df.columns:
            return []
        sectors = zt_df["industry"].dropna().astype(str)
        # 过滤空值和无意义标签
        sectors = sectors[sectors.str.len() > 0]
        counts = sectors.value_counts().head(5)
        lines = []
        for industry, count in counts.items():
            stocks = zt_df[zt_df["industry"].astype(str) == industry]
            names = stocks["name"].astype(str).head(5).tolist()
            lines.append(f"- **{industry}**：{count}只涨停，代表：{'/'.join(names)}")
        return lines

    @classmethod
    def _score_stocks(cls, zt_df: pd.DataFrame) -> dict:
        """
        多维度加权打分，区分核心标的与杂毛股。
        返回: {code: score} 字典，score 范围 0~100
        """
        import numpy as np
        scores = {}
        if zt_df is None or zt_df.empty:
            return scores

        # 提取各维度原始值
        codes = zt_df["code"].astype(str).tolist()
        lbcs = pd.to_numeric(zt_df.get("lbc", 1), errors="coerce").fillna(1)
        amounts = pd.to_numeric(zt_df.get("amount", 0), errors="coerce").fillna(0) / 1e8
        seals = pd.to_numeric(zt_df.get("seal_amount", 0), errors="coerce").fillna(0) / 1e8
        turnovers = pd.to_numeric(zt_df.get("turnover_rate", 0), errors="coerce").fillna(0)
        open_cnts = pd.to_numeric(zt_df.get("open_count", 0), errors="coerce").fillna(0)

        # 归一化辅助：线性映射到 0~100
        def _norm(series, reverse=False):
            mn, mx = series.min(), series.max()
            if mx == mn:
                return pd.Series([50.0] * len(series))
            result = (series - mn) / (mx - mn) * 100
            return 100 - result if reverse else result

        score_lbc = _norm(lbcs) * 0.30
        score_amount = _norm(amounts) * 0.25
        score_seal = _norm(seals) * 0.20

        # 换手率：8%~20% 最优，<3% 或 >30% 扣分
        score_turnover = []
        for t in turnovers:
            if 8 <= t <= 20:
                score_turnover.append(100)
            elif 3 <= t < 8 or 20 < t <= 30:
                score_turnover.append(60)
            else:
                score_turnover.append(20)
        score_turnover = pd.Series(score_turnover) * 0.15

        # 炸板次数：0 次满分，逐次扣分
        score_open = (100 - open_cnts.clip(upper=4) * 25) * 0.10

        total = score_lbc + score_amount + score_seal + score_turnover + score_open
        for i, code in enumerate(codes):
            scores[code] = round(float(total.iloc[i]), 0)

        return scores

    @classmethod
    def run_review(
        cls,
        trade_date: str,
        zt_df: pd.DataFrame,
        zhaban_df: pd.DataFrame,
        dt_df: pd.DataFrame,
        top_amount_df: pd.DataFrame,
        lhb_df: pd.DataFrame,
        spot_df: pd.DataFrame = None,
        lhb_seats_df: pd.DataFrame = None,
        yesterday_zt_yield: float = 1.5,
        market_style_info: str = "",
        precomputed_emotion: dict = None
    ) -> str:
        """
        运行盘后深度复盘
        """
        logger.info(f"开始执行 {trade_date} 盘后深度复盘...")

        # 1. 计算情绪多维向量分值（若有预计算数据则复用，避免重复调用）
        if precomputed_emotion is not None:
            emotion_res = precomputed_emotion
        else:
            emotion_res = EmotionVector.calculate(
                zt_df=zt_df,
                zhaban_df=zhaban_df,
                dt_df=dt_df,
                yesterday_zt_avg_premium=yesterday_zt_yield
            )

        # 2. 从数据库检索昨日推荐标的进行胜率复盘（修复 #4：按日期过滤+全市场匹配）
        import datetime
        yesterday = (datetime.datetime.strptime(trade_date, "%Y%m%d") -
                      datetime.timedelta(days=1)).strftime("%Y%m%d")
        history_recs = RecommendationManager.get_pending_recommendations(trade_date=yesterday)
        history_review_lines = []
        # 优先用全市场快照，其次用 Top 20
        match_df = spot_df if (spot_df is not None and not spot_df.empty) else top_amount_df
        if history_recs:
            for rec in history_recs:
                code = rec["code"]
                name = rec["name"]
                perf = "平盘/未找到"
                if match_df is not None and not match_df.empty and "code" in match_df.columns:
                    match_rows = match_df[match_df["code"].astype(str) == str(code)]
                    if not match_rows.empty:
                        target_row = match_rows.iloc[0]
                        perf = (f"今日收盘价 {target_row.get('price', 'N/A')}元, "
                                f"涨幅 {target_row.get('change_pct', 'N/A')}%")
                history_review_lines.append(f"- 标的 {name}({code}) [{rec.get('strategy_type')}]: {perf}")
        history_review_text = "\n".join(history_review_lines) if history_review_lines else "前一交易日无推荐记录"

        # 3. 计算提取今日“动态中军池”
        core_leaders = ActiveCorePool.filter_core_leaders(top_amount_df) if top_amount_df is not None else []
        core_pool_lines = [f"- {c['name']}({c['code']}): 涨幅 {c['change_pct']}%, 日成交额 {c['amount_billion']}亿, 总市值 {c['market_cap_billion']}亿" for c in core_leaders]
        active_core_pool_text = "\n".join(core_pool_lines) if core_pool_lines else "暂无符合条件的大成交额核心中军"

        # 4. 主线板块检测（涨停池行业聚类）
        sector_lines = cls._detect_leading_sectors(zt_df)
        leading_sector_text = "\n".join(sector_lines) if sector_lines else "无明显主线板块"

        # 5. 评估高位龙头异动风控（拉取真实 K 线计算累计涨幅）
        yidong_warning_lines = []
        if zt_df is not None and not zt_df.empty:
            index_3d_pct, index_10d_pct = cls._get_recent_index_pct()
            for _, row in zt_df.head(10).iterrows():
                code = str(row.get("code", ""))
                name = str(row.get("name", ""))
                lbc = row.get("lbc", 1)
                try:
                    lbc = int(lbc)
                except (ValueError, TypeError):
                    lbc = 1
                if lbc >= 2:
                    # 拉取真实日 K 线计算累计涨幅（替代估算）
                    real_3d, real_10d = cls._get_stock_recent_pct(code)
                    yidong_info = RegulatoryYidongCalculator.evaluate_stock_yidong(
                        code=code, name=name,
                        recent_3d_pct=real_3d,
                        index_3d_pct=index_3d_pct,
                        recent_10d_pct=real_10d,
                        index_10d_pct=index_10d_pct,
                        yidong_count_10d=0
                    )
                    if yidong_info["level"] != "NORMAL":
                        yidong_warning_lines.append(f"- 🚨【{name}({code})】{yidong_info['warning_msg']}")

        yidong_warning_text = "\n".join(yidong_warning_lines) if yidong_warning_lines else "高位连板标的无严重监管异动风险"

        # 3. 连板梯队 + 分层分时数据（核心标的给全量 OHLCV，杂毛给一行摘要）
        ladder_text = "无连板数据"
        intraday_text = "无分时数据"
        if zt_df is not None and not zt_df.empty and "lbc" in zt_df.columns:
            try:
                import time as _time
                import numpy as _np

                # ---- 3.1 多维度打分，区分核心标的与杂毛 ----
                scored = cls._score_stocks(zt_df)
                # 核心标的：前 15 名，其余为杂毛
                core_threshold = sorted(scored.values(), reverse=True)[:15][-1] if len(scored) >= 15 else 0

                # ---- 3.2 连板梯队：全量输出，标记得分 ----
                zt_sorted = zt_df.copy()
                zt_sorted["_score"] = zt_sorted["code"].astype(str).map(scored).fillna(0)
                zt_sorted = zt_sorted.sort_values(by=["lbc", "_score"], ascending=[False, False])

                ladder_lines = []
                for lbc_val, group in zt_sorted.groupby("lbc", sort=False):
                    stock_details = []
                    for _, zrow in group.iterrows():
                        code = str(zrow.get("code", ""))
                        name = str(zrow.get("name", ""))
                        s = scored.get(code, 0)
                        star = "⭐" if s >= core_threshold else "  "
                        amt = float(zrow.get("amount", 0)) / 1e8
                        turnover = float(zrow.get("turnover_rate", 0))
                        seal = float(zrow.get("seal_amount", 0)) / 1e8
                        open_cnt = int(zrow.get("open_count", 0))
                        first_seal = str(zrow.get("first_seal_time", ""))
                        stock_details.append(
                            f"{star} {name}({code}) 得分{s:.0f} 成交{amt:.2f}亿 "
                            f"换手{turnover:.2f}% 封单{seal:.2f}亿 炸板{open_cnt}次 首封{first_seal}"
                        )
                    lvl_tag = "首板" if lbc_val == 1 else f"{lbc_val}连板"
                    ladder_lines.append(
                        f"- 【{lvl_tag}】({len(stock_details)}家):\n    " + "\n    ".join(stock_details)
                    )
                ladder_text = "\n".join(ladder_lines)

                # ---- 3.3 分时数据：核心标的给全量 OHLCV，杂毛只给摘要 ----
                core_intra_lines = []
                brief_lines = []
                for idx, (_, zrow) in enumerate(zt_sorted.iterrows()):
                    code = str(zrow.get("code", ""))
                    name = str(zrow.get("name", ""))
                    lbc = int(zrow.get("lbc", 1))
                    s = scored.get(code, 0)
                    is_core = s >= core_threshold

                    if is_core:
                        try:
                            from data.fetcher import DataFetcher
                            df = DataFetcher._fetch_intraday_5min(code)
                            if df is not None and not df.empty:
                                rows = []
                                for _, r in df.iterrows():
                                    t = str(r.get("time", r.iloc[0]))
                                    if any(m in str(t) for m in ["09:", "10:", "11:", "13:", "14:", "15:"]):
                                        o_val = float(r.get("open", 0))
                                        c_val = float(r.get("close", 0))
                                        h_val = float(r.get("high", 0))
                                        l_val = float(r.get("low", 0))
                                        v_val = float(r.get("volume", 0)) / 1e4
                                        rows.append(f"{str(t)[-8:-3]} O{o_val:.2f} H{h_val:.2f} L{l_val:.2f} C{c_val:.2f} V{v_val:.0f}万")
                                if rows:
                                    core_intra_lines.append(
                                        f"--- {name}({code}) {lbc}板 得分{s:.0f} ---\n" + "\n".join(rows)
                                    )
                        except Exception:
                            pass
                    else:
                        # 杂毛：一行摘要
                        amt = float(zrow.get("amount", 0)) / 1e8
                        turnover = float(zrow.get("turnover_rate", 0))
                        seal = float(zrow.get("seal_amount", 0)) / 1e8
                        open_cnt = int(zrow.get("open_count", 0))
                        brief_lines.append(
                            f"  {name}({code}) {lbc}板 得分{s:.0f} 成交{amt:.2f}亿 换手{turnover:.2f}% 封单{seal:.2f}亿 炸板{open_cnt}次"
                        )

                    if idx % 3 == 2:
                        _time.sleep(0.6)

                if core_intra_lines:
                    core_part = "\n\n".join(core_intra_lines)
                    brief_part = "\n".join(brief_lines) if brief_lines else ""
                    intraday_text = (
                        f"【核心标的全量分时 OHLCV】\n{core_part}\n\n"
                        f"【其余标的摘要】\n{brief_part}"
                    )
            except Exception as e:
                logger.warning(f"格式化连板梯队/分时数据失败: {e}")

        # 4. 获取历史龙头数据（供 LLM 判断二波止跌标的）
        from database.services import DragonManager
        recent_dragons = DragonManager.get_recent_dragons(days_lookback=30)
        dragon_text = "暂无历史龙头数据"
        if recent_dragons:
            dragon_lines = []
            for d in recent_dragons:
                dragon_lines.append(
                    f"- {d['name']}({d['code']}): {d['max_lbc']}连板龙头, "
                    f"见顶日{d['peak_date']}, 最高价{d['peak_price']}元, 题材:{d.get('board_name', '未知')}"
                )
            dragon_text = "\n".join(dragon_lines)

        # 3. 格式化成交额 Top 20 文本
        top_amount_text = "无成交额数据"
        if top_amount_df is not None and not top_amount_df.empty:
            try:
                records = top_amount_df.head(20).to_dict(orient="records")
                lines = [
                    f"{idx+1}. {r.get('name')}({r.get('code')}): 最新价 {r.get('price')}元, 涨幅 {r.get('change_pct')}%, 成交额 {round(float(r.get('amount',0))/1e8, 2)}亿"
                    for idx, r in enumerate(records)
                ]
                top_amount_text = "\n".join(lines)
            except Exception as e:
                logger.warning(f"格式化 Top 20 失败: {e}")

        # 4. 龙虎榜席位分析（优先使用营业部级数据）
        seat_res = SeatAnalyzer.analyze_lhb(lhb_seats_df if lhb_seats_df is not None and not lhb_seats_df.empty else lhb_df)
        lhb_text = f"龙虎榜整体评价: {seat_res['summary']}\n风控级别: {seat_res['risk_warning']}"
        if seat_res.get("detected_seats"):
            seat_lines = [f"- {s['stock_name']} | {s['seat_name']} ({s['seat_type']}): 净买入 {s['net_amt_wan']}万" for s in seat_res["detected_seats"][:10]]
            lhb_text += "\n核心游资动向:\n" + "\n".join(seat_lines)

        # 5. 填入 User Prompt 模板
        user_prompt = POST_MARKET_USER_TEMPLATE.format(
            trade_date=trade_date,
            height=emotion_res["height"],
            up_limit_count=emotion_res["zt_count"],
            down_limit_count=emotion_res["dt_count"],
            zhaban_count=emotion_res["zhaban_count"],
            yesterday_up_limit_yield=emotion_res["yield_rate"],
            seal_force_ratio=emotion_res["seal_force_ratio"],
            zhaban_rate=emotion_res["zhaban_rate"],
            yidong_bravery=emotion_res["yidong_bravery"],
            history_recommend_review_text=history_review_text,
            active_core_pool_text=active_core_pool_text,
            yidong_text=yidong_warning_text,
            ladder_text=ladder_text,
            top_amount_text=top_amount_text,
            lhb_text=lhb_text,
            leading_sector_text=leading_sector_text,
            dragon_text=dragon_text,
            intraday_text=intraday_text,
            market_style_info=market_style_info or ""
        )

        # 6. 调用 LLM 生成复盘分析
        analysis_result = llm_client.generate(
            system_prompt=POST_MARKET_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            module="post_market"
        )

        return analysis_result
