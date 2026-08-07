import os
# 防止本文件的 test_stock_filtering 触发 database 包导入时绑定生产库（unittest 字母序先于 test_database）
os.environ.setdefault("DB_PATH", "dragon_pulse_test.db")

import unittest
import pandas as pd
from unittest.mock import MagicMock, patch

from core.emotion_index import EmotionVector
from core.core_pool import ActiveCorePool
from core.strategies import StrategyAnalyzer
from core.seat_analyzer import SeatAnalyzer
from core.holding_monitor import HoldingMonitor


class TestQuantCore(unittest.TestCase):
    """
    量化策略核心计算单元测试
    """

    def test_emotion_vector_calculation(self):
        """测试 5D 情绪多维向量评分计算"""
        zt_df = pd.DataFrame([
            {"code": "000001", "name": "股票A", "lbc": 5, "seal_amount": 100000000},
            {"code": "000002", "name": "股票B", "lbc": 2, "seal_amount": 50000000}
        ])
        zhaban_df = pd.DataFrame([{"code": "000003", "name": "股票C"}])
        dt_df = pd.DataFrame([])

        res = EmotionVector.calculate(zt_df, zhaban_df, dt_df)

        self.assertEqual(res["height"], 5)
        self.assertEqual(res["zt_count"], 2)
        self.assertEqual(res["dt_count"], 0)
        self.assertEqual(res["zhaban_count"], 1)
        self.assertGreater(res["sentiment_index"], 0)

    def test_active_core_pool_filter(self):
        """测试动态中军池筛选（Beta 网络取数已 mock，保持确定性单元测试）"""
        from core.core_pool import ActiveCorePool
        board_cons = pd.DataFrame([
            {"code": "000001", "name": "中军A", "price": 10.0, "change_pct": 2.0, "amount": 30e8, "total_market_cap": 500e8},
            {"code": "000002", "name": "跟风B", "price": 5.0, "change_pct": 1.0, "amount": 2e8, "total_market_cap": 20e8}
        ])

        # 无板块指数 + 无个股历史 → beta=None 不参与过滤（只测成交量/市值维度）
        with patch.object(ActiveCorePool, "_get_market_index_series", return_value=None), \
             patch("data.fetcher.DataFetcher.get_stock_daily_closes", return_value=[]):
            results = ActiveCorePool.filter_core_leaders(board_cons)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["code"], "000001")

    def test_strategy_tags(self):
        """测试战法标签分类归因"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="000001",
            stock_name="二波标的",
            change_pct=5.0,
            turnover_rate=10.0,
            is_past_dragon=True,
            retreat_ratio_from_high=0.40
        )
        self.assertIn("二波预警", tags)

    def test_lhb_seat_analysis(self):
        """测试龙虎榜席位画像分析"""
        lhb_df = pd.DataFrame([
            {"name": "测试股", "seat_name": "招商证券福州六一中路", "buy_amount": 50000000, "sell_amount": 0, "net_amount": 50000000}
        ])
        res = SeatAnalyzer.analyze_lhb(lhb_df)
        self.assertEqual(res["risk_warning"], "低")
        self.assertIn("格局派", res["summary"])

    def test_stock_filtering(self):
        """测试科创板、北交所及 ST 股票过滤功能"""
        from data.fetcher import DataFetcher
        df = pd.DataFrame([
            {"code": "600519", "name": "贵州茅台"},   # 主板 -> 保留
            {"code": "688001", "name": "华兴源创"},   # 科创板 -> 剔除
            {"code": "830001", "name": "北交股票"},   # 北交所 -> 剔除
            {"code": "000002", "name": "*ST万科"}    # ST股 -> 剔除
        ])
        filtered = DataFetcher.filter_stocks(df)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered.iloc[0]["code"], "600519")

    def test_regulatory_yidong_calculator(self):
        """测试交易所监管异动计算与风险评估"""
        from core.regulatory_yidong import RegulatoryYidongCalculator
        res = RegulatoryYidongCalculator.evaluate_stock_yidong(
            code="000001",
            name="高位龙头",
            recent_3d_pct=18.0,  # 3日偏离近20%
            recent_10d_pct=85.0, # 10日偏离近100%
            yidong_count_10d=3
        )
        self.assertEqual(res["level"], "CRITICAL_SERIOUS")
        self.assertTrue(len(res["warning_tags"]) > 0)

    def test_holding_monitor_sell_signal(self):
        """测试持仓断板必卖提醒"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001",
            stock_name="断板股",
            current_price=10.0,
            cost_price=9.5,
            avg_vwap_price=10.2,
            ma5_price=10.1,
            is_limit_up=False,
            was_limit_up_today=True
        )
        self.assertTrue(any(s["type"] == "断板必卖" for s in signals))


class TestStyleHysteresis(unittest.TestCase):
    """市场风格滞后缓冲（评审C7）：评分在阈值附近不每15s频闪横跳"""

    def _em(self):
        # zt25/h4/sentiment45 → 低吸 0.45（在退出阈值0.45~进入阈值0.60 的滞回带内）
        return {"height": 4, "zt_count": 25, "dt_count": 3, "zhaban_rate": 10,
                "sentiment_index": 45, "yield_rate": 1.0}

    def test_无prev用基础阈值(self):
        from core.strategies import MarketStyle
        # 低吸 0.45 < 基础攻击阈值 0.55 → 观望
        self.assertEqual(MarketStyle.classify(self._em())["style"], "观望")

    def test_滞后保持原风格(self):
        from core.strategies import MarketStyle
        # 已处低吸且 0.45 ≥ 退出阈值 → 保持（不再因分数略降横跳）
        self.assertEqual(MarketStyle.classify(self._em(), prev_style="低吸")["style"], "低吸")

    def test_进入需更高阈值(self):
        from core.strategies import MarketStyle
        # 观望进低吸需 ≥0.60，0.45 不足 → 仍观望（防弱信号误切）
        self.assertEqual(MarketStyle.classify(self._em(), prev_style="观望")["style"], "观望")

    def test_跌破退出阈值重新选择(self):
        from core.strategies import MarketStyle
        from config.settings import settings
        with patch.object(settings, "STYLE_EXIT_SCORE", 0.5):  # 退出阈值提到 0.5 > 低吸 0.45
            r = MarketStyle.classify(self._em(), prev_style="低吸")
        self.assertNotEqual(r["style"], "低吸")  # 0.45 < 0.5 → 不保持

    def test_开关关闭不滞后(self):
        from core.strategies import MarketStyle
        from config.settings import settings
        with patch.object(settings, "STYLE_HYSTERESIS_ENABLED", False):
            # prev=低吸 但关闭滞后 → 走基础判定(0.45<0.55) → 观望
            self.assertEqual(MarketStyle.classify(self._em(), prev_style="低吸")["style"], "观望")


class TestBaotuanPriority(unittest.TestCase):
    """抱团优先级门控（评审B6）：主线爆发时杂毛跌停不再强制防守，枯竭市场才最高优先"""

    def test_活跃市场抱团降级(self):
        # 涨停30(活跃) + 跌停15(杂毛跌停) + K=1.0 → 旧判抱团，新判主线进攻
        from core.strategies import MarketStyle
        em = {"height": 4, "zt_count": 30, "dt_count": 15, "zhaban_rate": 10,
              "sentiment_index": 55, "yield_rate": -1.0}
        r = MarketStyle.classify(em, market_amount=8000, baseline=8000)  # K=1.0
        self.assertNotEqual(r["style"], "抱团")  # 不再强制防守
        self.assertIn(r["style"], ("共振", "打板", "低吸"))  # 主线进攻优先

    def test_枯竭市场抱团仍最高优先级(self):
        # 涨停8 + 跌停15 + K=0.5 → K<0.8 且 zt<15 → 抱团保持最高优先级
        from core.strategies import MarketStyle
        em = {"height": 2, "zt_count": 8, "dt_count": 15, "zhaban_rate": 30,
              "sentiment_index": 30, "yield_rate": -3.0}
        r = MarketStyle.classify(em, market_amount=4000, baseline=8000)  # K=0.5
        self.assertEqual(r["style"], "抱团")

    def test_涨停多但K枯竭不判抱团(self):
        # AND 条件：K<0.8 且 涨停<15 才最高优先；zt=20≥15 → 不满足 → 不判抱团
        from core.strategies import MarketStyle
        em = {"height": 4, "zt_count": 20, "dt_count": 18, "zhaban_rate": 20,
              "sentiment_index": 50, "yield_rate": -2.0}
        r = MarketStyle.classify(em, market_amount=4000, baseline=8000)  # K=0.5 但 zt=20
        self.assertNotEqual(r["style"], "抱团")


if __name__ == "__main__":
    unittest.main()
