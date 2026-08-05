# -*- coding: utf-8 -*-
"""资金流向替代源（同花顺全市场即时快照）单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from data.fetcher_pool import _PoolMixin as Pool
from scheduler.monitor_core import _MonitorCoreMixin
from scheduler.monitor_signals import _MonitorSignalsMixin


class _TestMonitor(_MonitorCoreMixin, _MonitorSignalsMixin):
    """组合 Core+Signals 两个 mixin，供 fund_inflow_alert 测试"""
    pass


class TestFundFlowInstant(unittest.TestCase):
    """同花顺全市场资金流快照：万/亿字符串解析为元"""

    @patch("akshare.stock_fund_flow_individual")
    def test_万亿解析(self, mock_ak):
        mock_ak.return_value = pd.DataFrame({
            "股票代码": ["000815", "300686"],
            "股票简称": ["美利云", "智动力"],
            "净额": ["7558.55万", "1.91亿"],
            "流入资金": ["1.91亿", "0.00"],
            "流出资金": ["0.00", "1.13亿"],
            "成交额": ["25.58亿", "2.03亿"],
        })
        df = Pool.get_fund_flow_instant()
        self.assertEqual(df.iloc[0]["code"], "000815")
        self.assertEqual(df.iloc[0]["net_amount"], 75_585_500)   # 7558.55万 → 元
        self.assertEqual(df.iloc[1]["net_amount"], 191_000_000)  # 1.91亿 → 元
        self.assertEqual(df.iloc[0]["amount"], 2_558_000_000)    # 25.58亿 → 元

    @patch("akshare.stock_fund_flow_individual")
    def test_空返回空(self, mock_ak):
        mock_ak.return_value = pd.DataFrame()
        self.assertTrue(Pool.get_fund_flow_instant().empty)


class TestFundInflowAlert(unittest.TestCase):
    """大单抱团监控：同花顺快照优先，东财兜底"""

    def setUp(self):
        self.m = _TestMonitor()
        self.m._DF = Pool

    def test_同花顺优先且东财不调用(self):
        instant = pd.DataFrame([
            {"code": "600001", "name": "扫货股", "net_amount": 5_000_000_000.0},  # 50亿 超阈值
            {"code": "600002", "name": "普通股", "net_amount": 100.0},
        ])
        with patch("data.fetcher_pool._PoolMixin.get_fund_flow_instant", return_value=instant), \
             patch("data.fetcher_pool._PoolMixin.get_individual_fund_flow",
                   side_effect=AssertionError("东财不应被调用(同花顺已覆盖)")):
            self.m._check_fund_inflow_alert(["600001", "600002"], {"600001": 1e9})
        # 走到这里说明同花顺路径生效、未落到东财兜底

    def test_同花顺失败回退东财(self):
        with patch("data.fetcher_pool._PoolMixin.get_fund_flow_instant", return_value=pd.DataFrame()), \
             patch("data.fetcher_pool._PoolMixin.get_individual_fund_flow", return_value=pd.DataFrame()):
            self.m._check_fund_inflow_alert(["600001"], {})  # 不抛错即可


if __name__ == "__main__":
    unittest.main()
