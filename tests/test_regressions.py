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
