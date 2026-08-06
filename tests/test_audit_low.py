# -*- coding: utf-8 -*-
"""低优先审计 8 项的可测部分单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from core.seat_analyzer import SeatAnalyzer
from core.strategies import StrategyAnalyzer
from core.core_pool import ActiveCorePool
from scheduler.monitor_core import _MonitorCoreMixin
from scheduler.monitor_signals import _MonitorSignalsMixin


class _TestMonitor(_MonitorCoreMixin, _MonitorSignalsMixin):
    pass


class TestLossBreaker(unittest.TestCase):
    """审计🟡①：熔断口径统一，昨收缺失按中性 0 计，不混入总盈亏"""

    def setUp(self):
        self.m = _TestMonitor()
        # 熔断触发会真实 bark 推送（外部副作用）：测试必须隔离，避免跑全量测试时
        # 给用户手机推送假的"当日亏损熔断"告警（曾真发生，见 2026-08-06 09:29）
        self._bark = patch("scheduler.monitor_signals.bark_notifier.send")
        self._bark.start()
        self.addCleanup(self._bark.stop)

    def test_昨收缺失按0计不触发(self):
        # prev=0(当日新买), profit_rate=-10% 但不应混入 → avg=0 > -5 不触发
        triggered = self.m._is_daily_loss_breaker_triggered([
            {"prev_close_price": 0, "current_price": 10.0, "profit_rate": -10.0}])
        self.assertFalse(triggered)

    def test_正常当日亏损触发(self):
        # 两只有昨收且当日大跌 → avg <= -5 触发
        triggered = self.m._is_daily_loss_breaker_triggered([
            {"prev_close_price": 10.0, "current_price": 9.0},   # -10%
            {"prev_close_price": 20.0, "current_price": 18.0},  # -10%
        ])
        self.assertTrue(triggered)


class TestLimitLine(unittest.TestCase):
    """审计🟡②：北交所 30cm 涨停线"""

    def test_北交所30_触发打板(self):
        tags = StrategyAnalyzer.identify_tags("830001", "北交A", change_pct=30.0, turnover_rate=5.0)
        self.assertIn("打板接力", tags)

    def test_北交所10_不触发打板(self):
        tags = StrategyAnalyzer.identify_tags("830001", "北交A", change_pct=10.0, turnover_rate=5.0)
        self.assertNotIn("打板接力", tags)

    def test_主板10_触发打板(self):
        tags = StrategyAnalyzer.identify_tags("600001", "主板A", change_pct=10.0, turnover_rate=5.0)
        self.assertIn("打板接力", tags)


class TestBetaAlign(unittest.TestCase):
    """审计🟡③：Beta 按最近 N 天对齐，停牌/新股不错位"""

    def test_指数更长时按最近对齐(self):
        stock = [1, 2, 3, 4, 5]
        index = [100, 100, 100, 1, 2, 3, 4, 5]  # 前3天是"另一段行情"
        beta = ActiveCorePool.calculate_beta(pd.Series(stock), pd.Series(index))
        # tail(5) 对齐 → [1..5] vs [1,2,3,4,5] 完全正相关 → 接近 1
        # 若按位置从头对齐 → [1..5] vs [100,100,100,1,2] → 强负相关
        self.assertGreater(beta, 0.9)

    def test_样本不足返回0(self):
        self.assertEqual(ActiveCorePool.calculate_beta(pd.Series([1, 2]), pd.Series([1, 2])), 0.0)


class TestSeatAnalyzer(unittest.TestCase):
    """审计🟡④：空/非空返回结构统一"""

    def test_空数据含detected_seats(self):
        r = SeatAnalyzer.analyze_lhb(None)
        self.assertIn("detected_seats", r)
        self.assertEqual(r["detected_seats"], [])


if __name__ == "__main__":
    unittest.main()
