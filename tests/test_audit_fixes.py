# -*- coding: utf-8 -*-
"""审计🔴5项修复的单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from core.backtest import AIBacktestEngine
from data.fetcher_history import _HistoryMixin
from scheduler.monitor_core import _MonitorCoreMixin


class TestAuctionCacheClear(unittest.TestCase):
    """审计①：竞价预判缓存跨日清空"""

    def test_新交易日清空预判缓存(self):
        import scheduler.monitor_auction as ma
        ma._auction_prediction_cache = "昨天的预判"
        m = _MonitorCoreMixin()
        m._load_cycle_phase = lambda: None  # _reset_daily_state 跨 mixin 调用，mock 掉
        m._alert_date = "20260804"  # 模拟昨日状态
        m._reset_daily_state()
        self.assertEqual(ma._auction_prediction_cache, "")


class TestVolumeUnit(unittest.TestCase):
    """审计②：分时成交量单位统一（东财手→股，新浪保持股）"""

    @patch("akshare.stock_zh_a_hist_min_em")
    def test_东财手转股(self, mock_ak):
        mock_ak.return_value = pd.DataFrame({
            "时间": ["2026-08-05 09:35", "2026-08-05 09:40"],
            "开盘": [10, 10], "收盘": [10.5, 10.6],
            "最高": [10.6, 10.6], "最低": [10, 10],
            "成交量": [1000, 2000],  # 手
            "涨跌幅": [1, 2],
        })
        df = _HistoryMixin._fetch_intraday_5min_eastmoney("600001")
        self.assertEqual(df["volume"].iloc[0], 100000)   # 1000手→100000股
        self.assertEqual(df["volume"].iloc[1], 200000)

    @patch("akshare.stock_zh_a_minute")
    def test_新浪保持股(self, mock_ak):
        mock_ak.return_value = pd.DataFrame({
            "day": ["2026-08-05 09:35"], "open": [10], "high": [10.6],
            "low": [10], "close": [10.5], "volume": [150000],  # 股
        })
        df = _HistoryMixin._fetch_intraday_5min_sina("600001")
        self.assertEqual(df["volume"].iloc[0], 150000)  # 保持股


class TestBacktestSellRules(unittest.TestCase):
    """审计③：断板必卖 + 破位止损"""

    def _mk_day(self, zt_codes, ma5_map, close_map):
        return {
            "zt_df": pd.DataFrame({"code": zt_codes, "name": zt_codes}) if zt_codes else pd.DataFrame(),
            "ma5_cache": ma5_map,
            "close_cache": close_map,
        }

    def test_断板必卖(self):
        pos = {"code": "600001", "name": "A", "cost_price": 10.0,
               "buy_date": "20260804", "hold_days": 1, "strategy": "打板接力-2连板"}
        day = self._mk_day(["600002"], {"600001": 11.0}, {"600001": 10.5})
        _, closed, _ = AIBacktestEngine._process_sells([pos], "20260805", day, 100000, 0.1)
        self.assertEqual(len(closed), 1)
        self.assertIn("断板必卖", closed[0]["reason"])

    def test_破位止损(self):
        pos = {"code": "600001", "name": "A", "cost_price": 10.0,
               "buy_date": "20260804", "hold_days": 1, "strategy": "首板低吸"}
        day = self._mk_day([], {"600001": 11.0}, {"600001": 10.5})  # close 10.5 < MA5 11
        _, closed, _ = AIBacktestEngine._process_sells([pos], "20260805", day, 100000, 0.1)
        self.assertEqual(len(closed), 1)
        self.assertIn("破位止损", closed[0]["reason"])

    def test_仍在涨停池不触发断板(self):
        pos = {"code": "600001", "name": "A", "cost_price": 10.0,
               "buy_date": "20260804", "hold_days": 1, "strategy": "打板接力-2连板"}
        # 还在涨停池 + 收盘10.8(+8%<强止盈20%) + 高于MA5 → 不应触发任何卖出
        day = self._mk_day(["600001"], {"600001": 9.0}, {"600001": 10.8})
        remaining, closed, _ = AIBacktestEngine._process_sells([pos], "20260805", day, 100000, 0.1)
        self.assertEqual(len(closed), 0)
        self.assertEqual(len(remaining), 1)


class TestBacktestNaN(unittest.TestCase):
    """审计④：买入 NaN 防御"""

    def test_NaN行跳过不崩溃(self):
        day_data = {"zt_df": pd.DataFrame([
            {"code": "600001", "name": "A", "price": None, "lbc": None},  # NaN 行
            {"code": "600002", "name": "B", "price": 10.0, "lbc": 2},
        ])}
        positions, _ = AIBacktestEngine._process_buys("20260805", 3, 100000, day_data, 0.1)
        self.assertEqual(len(positions), 1)  # 只买 600002
        self.assertEqual(positions[0]["code"], "600002")


class TestApiPoolFallback(unittest.TestCase):
    """审计⑤：池端点上游异常兜底，不裸 500"""

    def test_异常返回空(self):
        from api_server import _safe_pool_df

        def boom():
            raise Exception("上游数据源挂了")
        r = _safe_pool_df(boom)
        self.assertEqual(r["code"], 200)
        self.assertEqual(r["count"], 0)
        self.assertIn("warning", r)

    def test_正常返回数据(self):
        from api_server import _safe_pool_df
        df = pd.DataFrame([{"code": "600001", "name": "A"}])
        r = _safe_pool_df(lambda: df)
        self.assertEqual(r["count"], 1)


if __name__ == "__main__":
    unittest.main()
