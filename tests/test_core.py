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


if __name__ == "__main__":
    unittest.main()
