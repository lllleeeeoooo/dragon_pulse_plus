"""
P0/P1 修复回归测试
覆盖：回测滑点单次、炸板口径统一、席位买卖方向、推荐 JSON 解析/幂等/TRIGGERED/过期语义、
净值快照与曲线排序、指数兜底、交易日兜底、核心标的评分索引对齐、core_pool Beta 实际生效。
"""
import os
os.environ.setdefault("DB_PATH", "dragon_pulse_test.db")

import math
import unittest
from unittest.mock import patch
import pandas as pd


class TestPureRegressions(unittest.TestCase):
    """纯逻辑回归（无需数据库）"""

    def test_backtest_calc_return_single_slippage(self):
        """回测收益率：买卖滑点各计一次，不再重复扣（修复前约 8.69%，修复后约 9.34%）"""
        from core.backtest import AIBacktestEngine
        buy_raw, sell_raw, s = 10.0, 11.0, 0.3
        cost = buy_raw * (1 + s / 100)
        sell_px = sell_raw * (1 - s / 100)
        ret = AIBacktestEngine._calc_return(cost, sell_px)
        expected = round((sell_px - cost) / cost * 100, 2)
        self.assertAlmostEqual(ret, expected, places=2)
        self.assertGreater(ret, 9.0)

    def test_zhaban_count_and_rate_unified(self):
        """炸板口径统一：真炸板 = 炸板池 − 当前涨停池"""
        from scheduler.monitor_style import _MonitorStyleMixin
        zt = pd.DataFrame({"code": ["600001", "600002", "600003"]})
        zb = pd.DataFrame({"code": ["600002", "600004"]})
        self.assertEqual(_MonitorStyleMixin._compute_true_zhaban_count(zt, zb), 1)
        m = _MonitorStyleMixin()
        m._zt_pool_cache = zt
        m._zhaban_pool_cache = zb
        self.assertAlmostEqual(m._get_market_zhaban_rate(), 25.0, places=2)

    def test_seat_analyzer_respects_direction(self):
        """席位派系看买卖方向：格局派净卖出判"出货"，净买入才判"强共识" """
        from core.seat_analyzer import SeatAnalyzer
        sell = pd.DataFrame([{"name": "T", "seat_name": "招商证券福州六一中路",
                              "buy_amount": 0, "sell_amount": 8e7, "net_amount": -8e7}])
        r = SeatAnalyzer.analyze_lhb(sell)
        self.assertEqual(r["risk_warning"], "中")
        self.assertIn("出货", r["summary"])
        buy = pd.DataFrame([{"name": "T", "seat_name": "招商证券福州六一中路",
                             "buy_amount": 8e7, "sell_amount": 0, "net_amount": 8e7}])
        r2 = SeatAnalyzer.analyze_lhb(buy)
        self.assertEqual(r2["risk_warning"], "低")
        self.assertIn("强共识", r2["summary"])

    def test_score_stocks_index_alignment(self):
        """核心标的评分：非连续索引 + 常量列时不得产生 NaN（修复前索引错位）"""
        from llm.post_market import PostMarketAnalyzer
        zt_df = pd.DataFrame({
            "code": ["600001", "600002", "600003"],
            "lbc": [5, 3, 1],
            "amount": [30e8, 20e8, 10e8],
            "seal_amount": [5e8, 5e8, 5e8],  # 常量 → _norm 走常量分支(RangeIndex)
            "turnover_rate": [10.0, 12.0, 15.0],
            "open_count": [0, 1, 2],
        }, index=[5, 9, 12])
        scores = PostMarketAnalyzer._score_stocks(zt_df)
        self.assertEqual(len(scores), 3)
        self.assertTrue(all(isinstance(v, (int, float)) and not math.isnan(v) for v in scores.values()))

    def test_get_previous_trading_day_weekend_fallback(self):
        """交易日兜底：日历不可用时返回最近工作日而非自然日"""
        import datetime
        import core.trade_calendar as tc
        with patch.object(tc, "is_trading_day", return_value=False):
            # 2026-08-03 是周一，日历全不可用 → 兜底返回周五 2026-07-31
            self.assertEqual(tc.get_previous_trading_day(datetime.date(2026, 8, 3)), "20260731")

    def test_parse_recommendations_json_paths(self):
        """推荐 JSON 解析三态：有效 / 空数组(AI不推荐) / 缺键回退正则"""
        from scheduler.helpers import _parse_and_save_recommendations
        from database.recommendations import RecommendationManager

        # 有效 JSON
        text = '```json\n{"recommendations":[{"code":"600519","name":"贵州茅台","strategy_type":"打板"}]}\n```'
        with patch.object(RecommendationManager, "add_recommendations") as add:
            _parse_and_save_recommendations("20260803", text)
            add.assert_called_once()
            self.assertEqual(add.call_args[0][1][0]["code"], "600519")

        # 空数组 → AI 判定不推荐，不落库
        text2 = '```json\n{"recommendations":[]}\n```'
        with patch.object(RecommendationManager, "add_recommendations") as add:
            _parse_and_save_recommendations("20260803", text2)
            add.assert_not_called()

        # JSON 缺 recommendations 键 → 走正则兜底，不应静默当作"AI 不推荐"
        text3 = '```json\n{"summary":"今日关注"}\n```\n关注标的：贵州茅台(600519) 与 平安银行(000001)'
        with patch.object(RecommendationManager, "add_recommendations") as add, \
             patch("data.fetcher.DataFetcher.get_stock_name", return_value="贵州茅台"):
            _parse_and_save_recommendations("20260803", text3)
            add.assert_called_once()
            codes = [i["code"] for i in add.call_args[0][1]]
            self.assertIn("600519", codes)


    def test_classify_auction_verdicts(self):
        """竞价结论关键词归类：买入/观察/放弃"""
        from scheduler.auction import _classify_auction_verdicts
        targets = [
            {"code": "001208", "name": "华菱线缆"},
            {"code": "002879", "name": "长缆科技"},
            {"code": "600396", "name": "华电辽能"},
        ]
        result = ("华菱线缆(001208) 竞价抢筹，建议直接挂单买入\n"
                  "长缆科技(002879) 低开走弱，放弃介入\n"
                  "华电辽能(600396) 平开震荡，观察")
        v = _classify_auction_verdicts(result, targets)
        self.assertEqual(v.get("001208"), "买入")
        self.assertEqual(v.get("002879"), "放弃")
        self.assertEqual(v.get("600396"), "观察")

    def test_daily_loss_breaker_uses_today_change(self):
        """熔断用当日盈亏(相对昨收)而非持仓总浮亏（修复名不副实）"""
        from scheduler.monitor_signals import _MonitorSignalsMixin
        from unittest.mock import patch
        obj = object.__new__(_MonitorSignalsMixin)
        obj._circuit_breaker_alerted = False
        # 今日 +5% / -10% → 平均 -2.5%，未破 -5%，即便 profit_rate 平均为 0 也不触发
        holdings = [
            {"code": "600001", "current_price": 10.5, "prev_close_price": 10.0, "profit_rate": 20.0},
            {"code": "600002", "current_price": 9.0, "prev_close_price": 10.0, "profit_rate": -20.0},
        ]
        with patch("scheduler.monitor_signals.bark_notifier.send"):
            self.assertFalse(_MonitorSignalsMixin._is_daily_loss_breaker_triggered(obj, holdings))
        # 单只今日 -10% → 触发
        with patch("scheduler.monitor_signals.bark_notifier.send"):
            self.assertTrue(_MonitorSignalsMixin._is_daily_loss_breaker_triggered(
                obj, [{"code": "600001", "current_price": 9.0, "prev_close_price": 10.0, "profit_rate": 50.0}]))

    def test_pattern_cache_reuses_result(self):
        """分时形态检测缓存：同一 code 只联网一次（修复轮询内重复联网）"""
        from scheduler.monitor_signals import _MonitorSignalsMixin
        obj = object.__new__(_MonitorSignalsMixin)
        obj._pattern_cache = {}
        calls = []
        class _DF:
            @staticmethod
            def detect_intraday_patterns(code):
                calls.append(code)
                return ["平稳走势"]
        obj._DF = _DF
        self.assertFalse(obj._is_bad_intraday_pattern("600001"))
        self.assertFalse(obj._is_bad_intraday_pattern("600001"))
        self.assertEqual(calls, ["600001"])  # 只联网一次
        self.assertIn("600001", obj._pattern_cache)

    def test_pattern_cache_expires_and_refetches(self):
        """分时形态缓存 TTL 过期后重新联网（盘中走势会变，不能冻结全天）"""
        from scheduler.monitor_signals import _MonitorSignalsMixin
        import time as _time
        obj = object.__new__(_MonitorSignalsMixin)
        calls = []
        class _DF:
            @staticmethod
            def detect_intraday_patterns(code):
                calls.append(code)
                return ["平稳走势"]
        obj._DF = _DF
        # 预置一条已过期缓存（时间戳在 9999 秒前）
        obj._pattern_cache = {"600001": (False, _time.time() - 9999)}
        self.assertFalse(obj._is_bad_intraday_pattern("600001"))
        self.assertEqual(calls, ["600001"])  # 过期 → 重新联网
        # 新结果写入后，TTL 内不再联网
        self.assertFalse(obj._is_bad_intraday_pattern("600001"))
        self.assertEqual(calls, ["600001"])

    def test_sentiment_score_anti_saturation(self):
        """情绪分反饱和：溢价/宽度不再顶格，能区分热度；中性锚点保持"""
        from core.emotion_index import EmotionVector
        # 溢价 4% 不再满分（原公式 +4% 就 100），8% 明显更高
        self.assertLess(EmotionVector._score_premium(4.0), 100)
        self.assertGreater(EmotionVector._score_premium(8.0), EmotionVector._score_premium(4.0))
        # 宽度 60 家不再满分（原公式 60 家就 100），127 家更高
        self.assertLess(EmotionVector._score_breadth(60), 100)
        self.assertGreater(EmotionVector._score_breadth(127), EmotionVector._score_breadth(60))
        # 中性锚点保持
        self.assertEqual(EmotionVector._score_premium(0), 50)
        self.assertEqual(EmotionVector._score_breadth(0), 40)

    def test_style_extreme_hot_is_gaochao(self):
        """涨停超打板上限+高标+情绪热 → 高潮，优先于共振（修复共振遮住高潮）"""
        from core.strategies import MarketStyle
        # 127 涨停 + 7 板 + 94 情绪 → 高潮(顶部风险)，不再是"共振/全力进攻"
        r = MarketStyle.classify({"height": 7, "zt_count": 127, "dt_count": 0,
                                  "zhaban_rate": 3.79, "sentiment_index": 94, "yield_rate": 4.16})
        self.assertEqual(r["style"], "高潮")
        # 正常 50 涨停 5 板 60 情绪 → 打板或共振，不误判高潮（评分模型下打板分更高）
        r2 = MarketStyle.classify({"height": 5, "zt_count": 50, "dt_count": 0,
                                   "zhaban_rate": 10, "sentiment_index": 60, "yield_rate": 2})
        self.assertIn(r2["style"], ("打板", "共振"))

    def test_sector_cycle_phase(self):
        """板块阶段状态机：冰点/启动/发酵/高潮/退潮 判定 + 主线分"""
        from core.sector_cycle import SectorCycleMachine as M
        self.assertEqual(M.derive_phase(0, 0, []), "冰点")
        self.assertEqual(M.derive_phase(2, 2, [0, 1]), "启动")
        self.assertEqual(M.derive_phase(4, 3, [0, 1, 3]), "发酵")
        self.assertEqual(M.derive_phase(8, 4, [3, 5]), "高潮")
        self.assertEqual(M.derive_phase(2, 2, [5, 7]), "退潮")
        # 从发酵/高潮掉到 1 家 → 退潮
        self.assertEqual(M.derive_phase(1, 1, [4, 5], "高潮"), "退潮")
        # 主线分
        self.assertGreater(M.mainline_score(6, 4, 2, 4), 0.5)
        self.assertLess(M.mainline_score(1, 1, 0, 1), 0.5)

    def test_market_style_classify(self):
        """市场风格分类：抱团/共振/打板 三档命中"""
        from core.strategies import MarketStyle
        r = MarketStyle.classify({"height": 2, "zt_count": 20, "dt_count": 12,
                                  "zhaban_rate": 30, "sentiment_index": 30, "yield_rate": -3})
        self.assertEqual(r["style"], "抱团")
        self.assertEqual(r["priority_strategy"], "避险抱团")
        r2 = MarketStyle.classify({"height": 4, "zt_count": 50, "dt_count": 0,
                                   "zhaban_rate": 10, "sentiment_index": 60, "yield_rate": 2})
        self.assertEqual(r2["style"], "共振")
        r3 = MarketStyle.classify({"height": 6, "zt_count": 40, "dt_count": 0,
                                   "zhaban_rate": 15, "sentiment_index": 50, "yield_rate": 1})
        self.assertEqual(r3["style"], "打板")

    def test_capacity_factor(self):
        """容量因子 K：缩量 <1，放量 >1"""
        from core.strategies import MarketStyle
        self.assertLess(MarketStyle._capacity_factor(6000, 8000), 1.0)
        self.assertGreater(MarketStyle._capacity_factor(10000, 8000), 1.0)

    def test_backtest_trade_date_list(self):
        """回测交易日历：akshare 日历生效，周末不计入；失败回退工作日"""
        from core.backtest import AIBacktestEngine
        cal = pd.DataFrame({"trade_date": pd.to_datetime(
            ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"])})
        with patch("akshare.tool_trade_date_hist_sina", return_value=cal):
            dates = AIBacktestEngine._build_trade_date_list("20260727", "20260802")  # 区间含周末
            self.assertEqual(dates, ["20260727", "20260728", "20260729", "20260730", "20260731"])
        with patch("akshare.tool_trade_date_hist_sina", side_effect=Exception("net down")):
            dates2 = AIBacktestEngine._build_trade_date_list("20260803", "20260804")  # 周一/周二
            self.assertEqual(dates2, ["20260803", "20260804"])

    def test_pre_market_analyzer(self):
        """盘前简报：content 为 None 不崩溃（修复 #P1-Q）+ prompt 组装正常"""
        from llm.pre_market import PreMarketAnalyzer
        news = [{"time": "10:00", "title": "标题", "content": None}]  # 旧代码此处崩溃
        with patch("llm.pre_market.NewsFetcher.get_cls_news", return_value=news), \
             patch("llm.pre_market.NewsFetcher.get_hot_search_words", return_value=["热搜词"]), \
             patch("llm.pre_market.llm_client.generate", return_value="AI简报内容"):
            self.assertEqual(PreMarketAnalyzer.run_report(), "AI简报内容")

    def test_call_auction_analyzer(self):
        """竞价观察：排序 Top15 + prompt 组装不崩（修复 #P1-L）"""
        from llm.call_auction import CallAuctionAnalyzer
        df = pd.DataFrame([
            {"code": "600001", "name": "A", "price": 10.0, "change_pct": 2.0, "amount": 5e7},
            {"code": "600002", "name": "B", "price": 20.0, "change_pct": 8.0, "amount": 8e7},
        ])
        with patch("llm.call_auction.llm_client.generate", return_value="竞价指令"):
            result = CallAuctionAnalyzer.run_auction_analysis(
                trade_date="20260804", auction_df=df,
                yesterday_zt_auction_yield=2.0, recommended_targets_summary="",
                predicted_sectors_summary="", auction_prediction="")
            self.assertEqual(result, "竞价指令")

    def test_fill_ohlc_from_sina(self):
        """腾讯源缺 OHLC 时用新浪补齐（修复低开猛拉在 0 值 OHLC 上误触发）"""
        import data.fetcher_spot as fs
        from data.fetcher_spot import _SpotMixin
        fs._sina_ohlc_cache = None
        fs._sina_ohlc_cache_time = 0.0
        tencent = pd.DataFrame({
            "code": ["002827", "600001"], "price": [41.84, 10.0],
            "volume_ratio": [12.2, 1.0],
            "open": [0.0, 0.0], "high": [0.0, 0.0], "low": [0.0, 0.0],
        })
        sina = pd.DataFrame({
            "code": ["002827", "600001"],
            "open": [41.87, 9.90], "high": [41.87, 10.2], "low": [38.82, 9.5],
        })
        with patch.object(_SpotMixin, "_fetch_spot_sina", return_value=sina):
            out = _SpotMixin._fill_ohlc_from_sina(tencent)
        r = out[out["code"] == "002827"].iloc[0]
        self.assertGreater(float(r["open"]), 0)
        self.assertEqual(float(r["open"]), 41.87)
        self.assertEqual(float(r["low"]), 38.82)
        # OHLC 已有真实值时不补（不触发新浪拉取）
        tencent2 = tencent.copy()
        tencent2["open"] = 41.87; tencent2["high"] = 41.9; tencent2["low"] = 38.8
        with patch.object(_SpotMixin, "_fetch_spot_sina", return_value=sina) as m:
            _SpotMixin._fill_ohlc_from_sina(tencent2)
        m.assert_not_called()
        fs._sina_ohlc_cache = None  # 清理缓存，避免影响其他测试

    def test_tencent_market_cap_unit(self):
        """腾讯市值 亿元→元（修复 流通市值:0亿、市值阈值算错）"""
        from data.fetcher_spot import _SpotMixin
        raw = pd.DataFrame({
            "code": ["sh600519"], "name": ["贵州茅台"], "zxj": ["1332.98"], "zdf": ["-1.91"],
            "zd": ["-26.00"], "zf": ["1.46"], "hsl": ["0.18"], "lb": ["0.79"],
            "volume": ["22125.00"], "turnover": ["296389"], "ltsz": ["16663.34"],
            "zsz": ["16663.34"], "pe_ttm": ["20.15"],
        })
        with patch("akshare.stock_zh_a_spot_tx", return_value=raw):
            df = _SpotMixin._fetch_spot_tencent()
        self.assertAlmostEqual(float(df.iloc[0]["circ_market_cap"]), 16663.34e8, delta=1e7)
        self.assertAlmostEqual(float(df.iloc[0]["total_market_cap"]), 16663.34e8, delta=1e7)

    def test_individual_fund_flow_name_mapping(self):
        """资金流接口：股票简称→name（修复名称显示成代码）"""
        from data.fetcher_pool import _PoolMixin
        raw = pd.DataFrame({
            "日期": ["2026-08-04"], "股票代码": ["301082"], "股票简称": ["奥雅股份"],
            "收盘价": [10.0], "涨跌幅": [5.0], "主力净流入-净额": [5e7],
        })
        with patch("akshare.stock_individual_fund_flow", return_value=raw):
            df = _PoolMixin.get_individual_fund_flow("301082", "sz")
        self.assertEqual(df.iloc[-1]["name"], "奥雅股份")
        self.assertEqual(df.iloc[-1]["main_net_inflow"], 5e7)

    def test_source_circuit_breaker(self):
        """数据源当日异常达阈值后熔断，后续不再调用该源；次日重置"""
        import data.core as dc
        from config.settings import settings
        dc._source_fail_counts.clear()
        dc._source_circuit_open.clear()
        dc._source_fail_date = dc._today_str()
        calls = []
        def bad():
            calls.append("bad"); raise Exception("conn reset")
        def good():
            calls.append("good"); return pd.DataFrame({"a": [1]})
        limit = settings.SOURCE_FAIL_CIRCUIT_LIMIT
        for _ in range(limit):
            dc.multi_source_fetch([("坏源", bad), ("好源", good)])
        self.assertEqual(calls.count("bad"), limit)
        self.assertTrue(dc._source_circuit_open.get("坏源", False))
        # 第 limit+1 次：坏源被熔断，直接走好源
        calls.clear()
        dc.multi_source_fetch([("坏源", bad), ("好源", good)])
        self.assertEqual(calls.count("bad"), 0)
        self.assertEqual(calls.count("good"), 1)
        # 状态接口覆盖真实源
        st = dc.source_circuit_status()
        self.assertIn("东财", st)
        # 次日重置
        dc._source_fail_date = "19990101"
        self.assertFalse(dc.source_blocked("坏源"))

    def test_spot_source_priority(self):
        """信号依赖量比/振幅：主源必须是东财或腾讯，不能是新浪（新浪缺量比振幅会让信号全灭，历史 bug）"""
        from data.core import SOURCE_PRIORITY
        self.assertEqual(SOURCE_PRIORITY[0], "东财")
        self.assertIn("腾讯", SOURCE_PRIORITY[:2])  # 腾讯必须在主源位置（东财挂时顶上）

    def test_sell_advisor_format_alert(self):
        """盘中异动润色：LLM 成功返回润色文本；失败降级为规则化文案"""
        from llm.sell_advisor import DynamicSellAdvisor
        with patch.object(DynamicSellAdvisor, "_fetch_intraday", return_value="分时数据"), \
             patch("llm.sell_advisor.llm_client.generate", return_value="润色后的提醒"):
            msg = DynamicSellAdvisor.format_alert_message(
                trigger_type="[点火异动]", stock_code="600001", stock_name="测试",
                current_price=10.0, change_pct=5.0, volume_ratio=3.0,
                strategy_tag="打板", detail_info="量比3倍")
            self.assertEqual(msg, "润色后的提醒")
        # LLM 失败 → 规则化文案兜底（包含代码与触发类型）
        with patch.object(DynamicSellAdvisor, "_fetch_intraday", return_value="分时数据"), \
             patch("llm.sell_advisor.llm_client.generate", side_effect=Exception("llm down")):
            msg2 = DynamicSellAdvisor.format_alert_message(
                trigger_type="[点火异动]", stock_code="600001", stock_name="测试",
                current_price=10.0, change_pct=5.0, volume_ratio=3.0,
                strategy_tag="打板", detail_info="量比3倍")
            self.assertIn("600001", msg2)
            self.assertIn("点火异动", msg2)

    def test_call_auction_includes_target_auction_data(self):
        """竞价分析：推荐标的 + 昨涨停的真实竞价数据必须进 prompt（修复"未提供具体竞价数据"）"""
        from llm.call_auction import CallAuctionAnalyzer
        df = pd.DataFrame([
            {"code": "001208", "name": "华菱线缆", "price": 10.0, "change_pct": 2.5, "amount": 8e7, "volume_ratio": 3.2},
            {"code": "002879", "name": "长缆科技", "price": 20.0, "change_pct": -1.0, "amount": 5e7, "volume_ratio": 1.1},
            {"code": "600396", "name": "华电辽能", "price": 5.0, "change_pct": 3.0, "amount": 2e7, "volume_ratio": 2.0},
            {"code": "000001", "name": "平安银行", "price": 12.0, "change_pct": 8.0, "amount": 3e8, "volume_ratio": 5.0},
        ])
        recs = [
            {"code": "001208", "name": "华菱线缆", "strategy_type": "打板", "open_requirement": "+1%~+4%"},
            {"code": "002879", "name": "长缆科技", "strategy_type": "低吸", "open_requirement": "平开或低开"},
            {"code": "600396", "name": "华电辽能", "strategy_type": "打板", "open_requirement": "+1%~+4%"},
        ]
        captured = {}
        def fake_generate(**kwargs):
            captured["prompt"] = kwargs.get("user_prompt", "")
            return "竞价指令"
        with patch("llm.call_auction.llm_client.generate", side_effect=fake_generate):
            CallAuctionAnalyzer.run_auction_analysis(
                trade_date="20260804", auction_df=df,
                recommended_targets=recs, recommended_targets_summary="",
                yesterday_zt_targets=[{"code": "000001", "name": "平安银行", "lbc": 1}])
        p = captured["prompt"]
        # 推荐标的具体竞价数据必须在 prompt 中（不再是"未提供具体竞价数据"）
        self.assertIn("华菱线缆", p)
        self.assertIn("2.5", p)   # 华菱线缆实际竞价涨幅
        self.assertIn("长缆科技", p)
        self.assertIn("-1.0", p)  # 长缆科技实际竞价涨幅
        self.assertIn("华电辽能", p)
        # 昨涨停标的竞价数据也在
        self.assertIn("平安银行", p)
        self.assertIn("8.0", p)
        # 三个推荐都在快照里，不应出现"未找到竞价数据"
        self.assertNotIn("未找到竞价数据", p)


class TestDbRegressions(unittest.TestCase):
    """数据库相关回归（使用独立测试库，防清空生产库）"""

    @classmethod
    def setUpClass(cls):
        from database.services import db_manager, switch_to_test_db
        from database.models import Base
        switch_to_test_db()
        Base.metadata.drop_all(bind=db_manager.engine)
        Base.metadata.create_all(bind=db_manager.engine)

    def setUp(self):
        from database.services import db_manager
        from database.models import Base
        session = db_manager.get_session()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
        finally:
            session.close()

    def test_recommendation_idempotent_and_triggered(self):
        """推荐幂等去重 + TRIGGERED 状态流转"""
        from database.services import RecommendationManager, db_manager
        from database.models import Recommendation
        RecommendationManager.add_recommendations("20260803", [{"code": "600519", "name": "茅台", "strategy_type": "打板"}])
        RecommendationManager.add_recommendations("20260803", [{"code": "600519", "name": "茅台", "strategy_type": "打板"}])
        pending = RecommendationManager.get_pending_recommendations("20260803")
        self.assertEqual(len(pending), 1)
        RecommendationManager.mark_triggered(pending[0]["id"])
        session = db_manager.get_session()
        rec = session.query(Recommendation).filter_by(code="600519").first()
        session.close()
        self.assertEqual(rec.status, "TRIGGERED")

    def test_recommendation_expire_semantics(self):
        """推荐过期语义：上一交易日创建的推荐当日存活，次日才过期"""
        from database.services import RecommendationManager, db_manager
        from database.models import Recommendation
        RecommendationManager.add_recommendations("20260803", [{"code": "600519", "name": "茅台", "strategy_type": "打板"}])
        RecommendationManager.add_recommendations("20260801", [{"code": "000001", "name": "平安", "strategy_type": "低吸"}])
        # 今日开盘：只过期"上一交易日之前"的（before_date = 上一交易日）
        RecommendationManager.expire_old_recommendations(before_date="20260803")
        pending = RecommendationManager.get_pending_recommendations("20260803")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["code"], "600519")
        # 次日再过期 → T 的也失效
        RecommendationManager.expire_old_recommendations(before_date="20260804")
        session = db_manager.get_session()
        n = session.query(Recommendation).filter_by(status="PENDING").count()
        session.close()
        self.assertEqual(n, 0)

    def test_snapshot_and_equity_curve_order(self):
        """净值快照落库正确（持仓市值=Σ现价×数量）+ 曲线取最近 N 天"""
        from database.market_data import DailySnapshotManager
        from database.models import DailyEquitySnapshot
        from database.services import db_manager
        pnl = {
            "cumulative_total_pnl": 12345.0, "total_unrealized_pnl": 5000.0,
            "today_realized_pnl": 100.0, "total_realized_pnl": 7000.0,
            "active_positions": 2, "today_total_pnl_pct": 1.2, "cumulative_total_pnl_pct": 3.4,
            "holdings": [
                {"code": "600519", "quantity": 100, "current_price": 1700.0, "cost_price": 1600.0},
                {"code": "000001", "quantity": 200, "current_price": 12.0, "cost_price": 11.0},
            ],
        }
        DailySnapshotManager.save_snapshot("20260803", pnl, sh_change_pct=0.5)
        DailySnapshotManager.save_snapshot("20260804", pnl, sh_change_pct=1.0)
        session = db_manager.get_session()
        row = session.query(DailyEquitySnapshot).filter_by(trade_date="20260804").first()
        session.close()
        self.assertAlmostEqual(row.position_value, 1700 * 100 + 12 * 200, places=2)
        self.assertAlmostEqual(row.available_cash, row.total_equity - row.position_value, places=2)
        # 取最近 1 天 → 应为最新日期
        curve = DailySnapshotManager.get_equity_curve(days=1)
        self.assertEqual(len(curve), 1)
        self.assertEqual(curve[0]["date"], "20260804")

    def test_batch_update_profit_rates(self):
        """批量更新持仓收益率（盘中一次 session 写库）"""
        from database.services import HoldingManager
        HoldingManager.add_holding(code="600519", cost_price=100.0, name="茅台", holding_type="AI_AUTO")
        HoldingManager.batch_update_profit_rates([("600519", 110.0, "AI_AUTO")])
        h = HoldingManager.get_active_holdings(holding_type="AI_AUTO")[0]
        self.assertAlmostEqual(h["profit_rate"], 10.0, places=2)

    def test_sector_concentration_gate(self):
        """板块集中度：同板块 AI 持仓达上限则跳过买入"""
        from scheduler.monitor_core import _MonitorCoreMixin
        obj = object.__new__(_MonitorCoreMixin)
        obj._industry_cache = {}
        obj._zt_pool_cache = pd.DataFrame([
            {"code": "600001", "industry": "电力设备"},
            {"code": "600002", "industry": "电力设备"},
            {"code": "600003", "industry": "电力设备"},
            {"code": "000001", "industry": "银行"},
        ])
        # 候选 600002 电力设备，已持仓 1 只 < 上限2 → 不集中
        self.assertFalse(obj._is_sector_concentrated("600002", [{"code": "600001"}]))
        # 已持仓 2 只 >= 上限2 → 集中，跳过
        self.assertTrue(obj._is_sector_concentrated(
            "600002", [{"code": "600001"}, {"code": "600003"}]))
        # 不同板块不限制
        self.assertFalse(obj._is_sector_concentrated("000001", [{"code": "600001"}]))

    def test_load_today_ai_bought_codes(self):
        """重启恢复当日买入计数：今日 AI_AUTO 持仓的 code 进入集合（盘中重启不丢计数）"""
        from scheduler.monitor_core import _MonitorCoreMixin
        import datetime
        from database.services import HoldingManager
        HoldingManager.add_holding(code="600519", cost_price=100.0, name="茅台", holding_type="AI_AUTO")
        obj = object.__new__(_MonitorCoreMixin)
        codes = obj._load_today_ai_bought_codes()
        self.assertIn("600519", codes)

    def test_auction_verdict_roundtrip(self):
        """竞价结论落库 + 按日期查全状态(含 TRIGGERED，胜率复盘用)"""
        from database import RecommendationManager, db_manager
        from database.models import Recommendation
        RecommendationManager.add_recommendations("20260803", [{"code": "600519", "name": "茅台", "strategy_type": "打板"}])
        RecommendationManager.update_auction_verdicts({"600519": "买入"})
        recs = RecommendationManager.get_pending_recommendations("20260803")
        self.assertEqual(recs[0]["auction_verdict"], "买入")
        # 标记 TRIGGERED 后，按日期查询仍能查到（胜率复盘不漏已买入）
        RecommendationManager.mark_triggered(recs[0]["id"])
        all_recs = RecommendationManager.get_recommendations_by_date("20260803")
        self.assertEqual(len(all_recs), 1)
        self.assertEqual(all_recs[0]["status"], "TRIGGERED")
        self.assertEqual(all_recs[0]["auction_verdict"], "买入")

    def test_sector_cycle_sync(self):
        """板块周期落库：从涨停池聚合出阶段与主线分"""
        from database import SectorCycleManager
        zt = pd.DataFrame([
            {"code": "600001", "name": "A", "industry": "电网设备", "lbc": 3},
            {"code": "600002", "name": "B", "industry": "电网设备", "lbc": 2},
            {"code": "600003", "name": "C", "industry": "电网设备", "lbc": 1},
            {"code": "000001", "name": "D", "industry": "电池", "lbc": 1},
        ])
        SectorCycleManager.sync_from_zt_pool("20260804", zt)
        recs = SectorCycleManager.get_sector_cycle(trade_date="20260804")
        sectors = {r["sector"]: r for r in recs}
        # 电网设备 3 家涨停 + 3 板龙头 → 发酵
        self.assertIn("电网设备", sectors)
        self.assertEqual(sectors["电网设备"]["phase"], "发酵")
        self.assertEqual(sectors["电网设备"]["zt_count"], 3)
        # 电池 1 家 → 启动
        self.assertEqual(sectors["电池"]["phase"], "启动")
        # 电网设备主线分高于电池
        self.assertGreater(sectors["电网设备"]["mainline_score"], sectors["电池"]["mainline_score"])

    def test_seat_profile_sync_and_classify(self):
        """龙虎榜席位画像：名席位种子 + 行为自动分类 + DB 查询"""
        from database import SeatProfileManager
        # 名席位种子（六一中路人工标签优先于自动分类）
        SeatProfileManager.seed_famous_seats()
        # 模拟新营业部连续 5 天净买入（与生产一致：每日同步一次）→ 应自动定型为格局派
        for i in range(5):
            day = f"2026080{i + 1}"
            SeatProfileManager.sync_from_lhb(pd.DataFrame([{
                "seat_name": "华鑫证券某新锐营业部", "trade_date": day,
                "buy_stock_count": 3, "sell_stock_count": 0,
                "buy_amount": 5e7, "sell_amount": 0, "net_amount": 5e7, "buy_stocks": "A,B,C",
            }]), day)
        prof = SeatProfileManager.get_seat_type("华鑫证券某新锐营业部")
        self.assertIsNotNone(prof)
        self.assertIn("格局", prof["type"])
        # 名席位种子：精确匹配返回人工标签
        famous = SeatProfileManager.get_seat_type("六一中路")
        self.assertIsNotNone(famous)
        self.assertIn("格局", famous["type"])

    def test_seat_northbound_special_case(self):
        """北向(外资)专用席位特判：买卖对半也应直接标"外资北向"，不误判为对倒派"""
        from database import SeatProfileManager
        for i in range(5):  # 连续 5 天买卖严格对半 → 行为分类本会判对倒，北向应特判
            day = f"2026081{i + 1}"
            SeatProfileManager.sync_from_lhb(pd.DataFrame([{
                "seat_name": "沪股通专用", "trade_date": day,
                "buy_stock_count": 5, "sell_stock_count": 5,
                "buy_amount": 5e7, "sell_amount": 5e7, "net_amount": 0, "buy_stocks": "A,B,C,D,E",
            }]), day)
        prof = SeatProfileManager.get_seat_type("沪股通专用")
        self.assertIsNotNone(prof)
        self.assertIn("北向", prof["type"])

    def test_seat_profiles_api(self):
        """席位画像查询接口：get_profiles/get_stats（人工种子 + 自动分类都在列表）"""
        from database import SeatProfileManager
        SeatProfileManager.seed_famous_seats()
        for i in range(5):
            day = f"2026081{i + 1}"
            SeatProfileManager.sync_from_lhb(pd.DataFrame([{
                "seat_name": "华鑫证券新锐B", "trade_date": day,
                "buy_stock_count": 3, "sell_stock_count": 0,
                "buy_amount": 5e7, "sell_amount": 0, "net_amount": 5e7, "buy_stocks": "A,B,C",
            }]), day)
        profs = SeatProfileManager.get_profiles(top=10)
        self.assertTrue(any(p["seat_name"] == "华鑫证券新锐B" and p["type"] == "格局派" for p in profs))
        self.assertTrue(any(p["seat_name"] == "六一中路" and p["is_manual"] for p in profs))
        stats = SeatProfileManager.get_stats()
        self.assertGreaterEqual(stats["total"], 2)
        self.assertGreaterEqual(stats["manual"], 1)
        self.assertIn("格局派", stats["by_type"])

    def test_core_pool_beta_real(self):
        """core_pool Beta 实际生效：用个股收盘价与市场指数计算相关性并过滤"""
        from core.core_pool import ActiveCorePool
        df = pd.DataFrame({
            "code": ["600001", "600002"],
            "name": ["A", "B"],
            "price": [10.0, 20.0],
            "change_pct": [2.0, 1.0],
            "amount": [30e8, 25e8],
            "total_market_cap": [500e8, 300e8],
        })
        closes = [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4]
        idx = pd.Series([3000.0, 3020.0, 3040.0, 3060.0, 3080.0, 3100.0, 3120.0, 3140.0])
        with patch("data.fetcher.DataFetcher.get_stock_daily_closes", return_value=closes), \
             patch.object(ActiveCorePool, "_get_market_index_series", return_value=idx):
            res = ActiveCorePool.filter_core_leaders(df)
            self.assertTrue(res)
            self.assertIsNotNone(res[0]["beta"])
            self.assertGreater(res[0]["beta"], 0.5)


if __name__ == "__main__":
    unittest.main()
