# -*- coding: utf-8 -*-
"""交易日历工具测试：get_n_trading_days_ago（龙头过期 30 个交易日按交易日回溯）
   与 _is_td_best_effort（表内真实交易日 / 表外工作日兜底，防覆盖不足死循环）"""
import datetime
import unittest
from unittest.mock import patch

from core.trade_calendar import get_n_trading_days_ago, _is_td_best_effort


def _noop():
    pass


class TestGetNTradingDaysAgo(unittest.TestCase):
    """按交易日回溯循环：跳过周末/非交易日，跨月正确（内部交易日判断 mock 成周一~周五）"""

    def setUp(self):
        self._p = [
            patch("core.trade_calendar._ensure_synced", _noop),
            patch("core.trade_calendar._get_calendar_earliest", return_value=None),
            patch("core.trade_calendar._is_td_best_effort",
                  side_effect=lambda d, e: d.weekday() < 5),
        ]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    def test_跨周末跳过(self):
        # 2026-08-10 周一往前 1 个交易日 → 跳过 8/8、8/9 回到上周五 8/7
        self.assertEqual(get_n_trading_days_ago(1, datetime.date(2026, 8, 10)), "20260807")

    def test_n个交易日连续计数(self):
        self.assertEqual(get_n_trading_days_ago(3, datetime.date(2026, 8, 10)), "20260805")
        self.assertEqual(get_n_trading_days_ago(2, datetime.date(2026, 8, 14)), "20260812")

    def test_30个交易日即6周自然日(self):
        # 每周5个交易日 → 30 交易日 = 42 自然日：2026-08-07(周五) 回退 42 天 = 2026-06-26(周五)
        self.assertEqual(get_n_trading_days_ago(30, datetime.date(2026, 8, 7)), "20260626")


class TestIsTdBestEffort(unittest.TestCase):
    """_is_td_best_effort：覆盖范围内走 trade_calendar 真实交易日，范围外/表空按工作日估算"""

    def test_覆盖范围内查表(self):
        with patch("database.services.TradeCalendarManager.is_trading_day",
                   return_value=True):
            self.assertTrue(_is_td_best_effort(datetime.date(2026, 8, 10), "2026-07-01"))
        with patch("database.services.TradeCalendarManager.is_trading_day",
                   return_value=False):
            self.assertFalse(_is_td_best_effort(datetime.date(2026, 8, 10), "2026-07-01"))

    def test_范围外按工作日估算(self):
        # 早于表最早日期（覆盖范围外）→ 周一~周五算交易日（周末跳过）
        self.assertTrue(_is_td_best_effort(datetime.date(2026, 8, 10), "2026-09-01"))   # 周一
        self.assertFalse(_is_td_best_effort(datetime.date(2026, 8, 15), "2026-09-01"))  # 周六

    def test_表空按工作日估算(self):
        self.assertTrue(_is_td_best_effort(datetime.date(2026, 8, 10), None))   # 表空 → 周一
        self.assertFalse(_is_td_best_effort(datetime.date(2026, 8, 16), None))  # 周日


if __name__ == "__main__":
    unittest.main()
