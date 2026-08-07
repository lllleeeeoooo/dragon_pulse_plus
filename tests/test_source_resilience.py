# -*- coding: utf-8 -*-
"""数据源韧性测试：东财 spot 多主机轮换补丁、源熔断跨重启持久化、主循环周期时间预算"""
import datetime
import os
import time
import unittest
from unittest.mock import patch

import pandas as pd


class TestEastmoneyHostRotation(unittest.TestCase):
    """Fix1：_fetch_spot_eastmoney 打补丁 akshare 分页层（加大 pz + 轮换主机），解析仍交给 akshare"""

    def test_补丁应用且调用后恢复(self):
        from data.fetcher_spot import _SpotMixin
        import akshare as _ak
        import akshare.utils.func as _func
        import akshare.stock_feature.stock_hist_em as _hist_em
        orig_func = _func.fetch_paginated_data
        orig_hist = getattr(_hist_em, "fetch_paginated_data", None)
        captured = {}

        def fake_spot_em():
            captured["patched_during_call"] = _func.fetch_paginated_data is not orig_func
            return pd.DataFrame([{
                "代码": "600001", "名称": "测试股", "最新价": 10.0, "涨跌幅": 1.0,
                "涨跌额": 0.1, "成交量": 1000, "成交额": 100000, "振幅": 2.0,
                "最高": 10.2, "最低": 9.9, "今开": 10.0, "昨收": 9.9,
                "量比": 1.0, "换手率": 1.0, "市盈率-动态": 10.0, "市净率": 1.0,
                "总市值": 100000000, "流通市值": 50000000,
            }])

        with patch("akshare.stock_zh_a_spot_em", side_effect=fake_spot_em):
            df = _SpotMixin._fetch_spot_eastmoney()
        self.assertTrue(captured.get("patched_during_call"))  # 调用期间补丁生效
        self.assertIs(_func.fetch_paginated_data, orig_func)  # 结束后恢复
        if orig_hist is not None:
            self.assertIs(_hist_em.fetch_paginated_data, orig_hist)
        self.assertFalse(df.empty)
        self.assertEqual(df["code"].iloc[0], "600001")
        self.assertEqual(df["volume"].iloc[0], 100000)  # 成交量 手→股 ×100

    def test_补丁增大pz并轮换主机(self):
        """验证补丁对 akshare 分页调用的实际转换：pz=2000 减少分页 + 轮换 push2 主机"""
        from data.fetcher_spot import _SpotMixin
        import akshare.utils.func as _func
        import akshare.stock_feature.stock_hist_em as _hist_em
        calls = []
        orig_hist = getattr(_hist_em, "fetch_paginated_data", None)

        def fake_paginated(url, params, timeout=15):
            calls.append((url, dict(params or {})))
            raise ConnectionError("IP限流(测试预期)")  # 模拟东财被限流

        with patch.object(_func, "fetch_paginated_data", side_effect=fake_paginated):
            with self.assertRaises(ConnectionError):
                _SpotMixin._fetch_spot_eastmoney()
        # 清理：_fetch_spot_eastmoney 的 finally 把 _hist_em.fetch_paginated_data 还原为 _orig(=fake)，
        # 手动恢复避免污染后续测试（生产环境 _orig 恒为真函数，无此问题）
        _hist_em.fetch_paginated_data = orig_hist
        self.assertTrue(calls)
        url, p = calls[0]
        self.assertEqual(p.get("pz"), "2000")  # 分页次数从 59 次降到 ~3 次
        self.assertTrue(any(h in url for h in
                            ["82.push2", "92.push2", "push2", "7.push2", "30.push2"]))


class TestCircuitPersistence(unittest.TestCase):
    """Fix2：源熔断状态落盘，看门狗重启后新进程恢复（当天不重打被限流源）"""

    def setUp(self):
        from data import core as _core
        self._core = _core
        self._orig_open = dict(_core._source_circuit_open)
        self._orig_date = _core._source_fail_date

    def tearDown(self):
        self._core._source_circuit_open.clear()
        self._core._source_circuit_open.update(self._orig_open)
        self._core._source_fail_date = self._orig_date
        if os.path.exists(self._core._CIRCUIT_STATE_FILE):
            try:
                os.remove(self._core._CIRCUIT_STATE_FILE)
            except OSError:
                pass

    def test_熔断落盘并跨重启恢复(self):
        today = datetime.date.today().strftime("%Y%m%d")
        self._core._source_circuit_open["东财"] = True
        self._core._source_fail_date = today
        self._core._persist_circuit_state()
        # 模拟看门狗重启：新进程内存态清零
        self._core._source_circuit_open.clear()
        self._core._source_fail_date = ""
        self._core._restore_circuit_state()
        self.assertTrue(self._core.source_blocked("东财"))  # 当天仍熔断，不重打
        self.assertFalse(self._core.source_blocked("新浪"))  # 未熔断源不受影响

    def test_隔日落盘不恢复(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        self._core._source_circuit_open["东财"] = True
        self._core._source_fail_date = yesterday
        self._core._persist_circuit_state()
        self._core._source_circuit_open.clear()
        self._core._source_fail_date = ""
        self._core._restore_circuit_state()
        self.assertFalse(self._core.source_blocked("东财"))  # 昨天熔断，今天不恢复


class TestCycleBudget(unittest.TestCase):
    """Fix3：单轮监控周期时间预算——慢源拖累时跳过非关键步骤，防触发看门狗重启"""

    def test_cycle_over_budget阈值(self):
        from scheduler.monitor_core import _MonitorCoreMixin
        from config.settings import settings
        m = _MonitorCoreMixin()
        m._cycle_started_at = time.time() - (settings.MONITOR_CYCLE_BUDGET_SECONDS + 10)
        self.assertTrue(m._cycle_over_budget())
        m._cycle_started_at = time.time()
        self.assertFalse(m._cycle_over_budget())
        # 无起点(未设)时保守返回 False，不误跳过
        del m._cycle_started_at
        self.assertFalse(m._cycle_over_budget())

    def test_超预算时_scan_signals跳过资金流(self):
        from scheduler.monitor_core import _MonitorCoreMixin
        m = _MonitorCoreMixin()
        m._cycle_started_at = time.time() - 999  # 远超预算
        # 构造命中"逼近封板"的行（有候选才会走到资金流检测；预算超时须被跳过）
        row = {"code": "600001", "name": "X", "price": 10.9, "change_pct": 9.0, "amount": 1e8,
               "volume_ratio": 6.0, "high": 10.9, "low": 10.0, "open": 10.0, "pre_close": 10.0,
               "amplitude": 9.0, "volume": 100000}
        spot = pd.DataFrame([row])

        def _merge(spot_df, hit_df, pending_recs):
            return hit_df  # 保留命中候选（非空），确保走到资金流检测分支

        with patch.object(m, "_check_fund_inflow_alert", create=True) as fund, \
             patch.object(m, "_merge_auction_buy_candidates", side_effect=_merge), \
             patch.object(_MonitorCoreMixin, "_is_limit_up",
                          staticmethod(lambda c, chg: False), create=True), \
             patch.object(m, "_get_stock_industry", return_value="半导体"), \
             patch.object(m, "_get_sector_phase", return_value=""), \
             patch.object(m, "_get_sector_is_mainline", return_value=False), \
             patch.object(m, "_get_concept_blocks_buy", return_value=False), \
             patch.object(m, "_get_stock_concept_tag", return_value=""):
            m._scan_signals(spot, {"priority_strategy": "", "style": "观望"},
                            [], set(), False)
        fund.assert_not_called()  # 预算超时 → 跳过资金流检测


class TestParallelSpotFetch(unittest.TestCase):
    """腾讯/新浪 spot 并行分页抓取 + SPOT_FETCH_PARALLEL 开关 + 失败回退 + 反爬自动切串行"""

    def setUp(self):
        from data.fetcher_spot import _parallel_fail_count, _parallel_blocked_until
        self._pf = _parallel_fail_count
        self._pb = _parallel_blocked_until
        self._pf.clear()
        self._pb.clear()

    def tearDown(self):
        self._pf.clear()
        self._pb.clear()

    def _fake_tencent_raw(self):
        return pd.DataFrame([{"code": "sh600001", "name": "X", "zxj": "10.0", "zdf": "1.0", "zd": "0.1",
                              "volume": "1000", "turnover": "100000", "zf": "2.0", "hsl": "1.0",
                              "lb": "1.0", "ltsz": "500000000", "zsz": "1000000000", "pe_ttm": "10"}])

    def test_腾讯并行开启走并行(self):
        from data.fetcher_spot import _SpotMixin
        from config.settings import settings
        with patch.object(settings, "SPOT_FETCH_PARALLEL", True), \
             patch.object(_SpotMixin, "_fetch_spot_tencent_parallel",
                          return_value=self._fake_tencent_raw()) as mp, \
             patch("akshare.stock_zh_a_spot_tx") as ms:
            r = _SpotMixin._fetch_spot_tencent()
        mp.assert_called_once()
        ms.assert_not_called()
        self.assertEqual(r["code"].iloc[0], "600001")  # 前缀被归一化去掉

    def test_腾讯并行失败回退akshare(self):
        from data.fetcher_spot import _SpotMixin
        from config.settings import settings
        with patch.object(settings, "SPOT_FETCH_PARALLEL", True), \
             patch.object(_SpotMixin, "_fetch_spot_tencent_parallel",
                          side_effect=RuntimeError("boom")), \
             patch("akshare.stock_zh_a_spot_tx",
                   return_value=self._fake_tencent_raw()) as ms, \
             patch.object(_SpotMixin, "_normalize_tencent_spot",
                          side_effect=lambda df: df):
            r = _SpotMixin._fetch_spot_tencent()
        ms.assert_called_once()  # 并行失败 → 回退 akshare 串行

    def test_腾讯并行关闭直接串行(self):
        from data.fetcher_spot import _SpotMixin
        from config.settings import settings
        with patch.object(settings, "SPOT_FETCH_PARALLEL", False), \
             patch.object(_SpotMixin, "_fetch_spot_tencent_parallel") as mp, \
             patch("akshare.stock_zh_a_spot_tx",
                   return_value=self._fake_tencent_raw()) as ms, \
             patch.object(_SpotMixin, "_normalize_tencent_spot",
                          side_effect=lambda df: df):
            _SpotMixin._fetch_spot_tencent()
        mp.assert_not_called()
        ms.assert_called_once()

    def test_并行分页合并按页排序(self):
        from data.fetcher_spot import _parallel_fetch_pages

        class _Resp:
            def __init__(self, code):
                self._code = code

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"rank_list": [{"code": self._code}]}}

        def fake_get(url, params, timeout):
            idx = int(params["offset"]) // 200
            return _Resp(f"c{idx}")

        with patch("requests.get", side_effect=fake_get):
            rows = _parallel_fetch_pages("http://x", lambda i: {"offset": str(i * 200)},
                                         lambda j: j["data"]["rank_list"], [2, 0, 1])
        self.assertEqual([r["code"] for r in rows], ["c0", "c1", "c2"])  # 按页序合并

    def test_新浪并行列映射(self):
        from data.fetcher_spot import _SpotMixin
        raw = [{"code": "sh600001", "name": "X", "trade": "10.5", "pricechange": "0.5",
                "changepercent": "5.0", "settlement": "10.0", "open": "10.1", "high": "10.8",
                "low": "10.0", "volume": 100000, "amount": 1000000}]

        class _Resp:
            text = "6000"

            def raise_for_status(self):
                pass

        with patch("requests.get", return_value=_Resp()), \
             patch("data.fetcher_spot._parallel_fetch_pages", return_value=raw):
            df = _SpotMixin._fetch_spot_sina_parallel()
        self.assertEqual(df["代码"].iloc[0], "sh600001")
        self.assertEqual(df["最新价"].iloc[0], 10.5)
        self.assertEqual(df["涨跌幅"].iloc[0], 5.0)
        self.assertEqual(df["最高"].iloc[0], 10.8)

    def test_并行连续失败自动熔断切串行(self):
        """触发反爬/连续失败 2 次后，自动熔断并行、直接走串行（不再每轮白试并行）"""
        from data.fetcher_spot import _SpotMixin
        from config.settings import settings
        with patch.object(settings, "SPOT_FETCH_PARALLEL", True), \
             patch.object(_SpotMixin, "_fetch_spot_tencent_parallel",
                          side_effect=RuntimeError("疑似反爬")) as mp, \
             patch("akshare.stock_zh_a_spot_tx",
                   return_value=self._fake_tencent_raw()) as ms, \
             patch.object(_SpotMixin, "_normalize_tencent_spot", side_effect=lambda df: df):
            _SpotMixin._fetch_spot_tencent()   # 失败1
            _SpotMixin._fetch_spot_tencent()   # 失败2 → 熔断
            _SpotMixin._fetch_spot_tencent()   # 熔断后：跳过并行，直接串行
        self.assertEqual(mp.call_count, 2)      # 熔断后并行不再被调用
        self.assertEqual(ms.call_count, 3)      # 每次都回退串行

    def test_并行成功后清零失败计数不熔断(self):
        """单次成功后清零连续失败计数，不误熔断"""
        from data.fetcher_spot import _SpotMixin
        from config.settings import settings
        raw = self._fake_tencent_raw()
        with patch.object(settings, "SPOT_FETCH_PARALLEL", True), \
             patch.object(_SpotMixin, "_fetch_spot_tencent_parallel",
                          side_effect=[RuntimeError("e1"), raw, raw]) as mp, \
             patch("akshare.stock_zh_a_spot_tx", return_value=pd.DataFrame()) as ms, \
             patch.object(_SpotMixin, "_normalize_tencent_spot", side_effect=lambda df: df):
            _SpotMixin._fetch_spot_tencent()   # 失败1
            _SpotMixin._fetch_spot_tencent()   # 成功 → 清零
            _SpotMixin._fetch_spot_tencent()   # 并行仍允许（未熔断）
        self.assertEqual(mp.call_count, 3)      # 并行始终被调用
        self.assertEqual(ms.call_count, 1)      # 只有第一次失败回退串行


if __name__ == "__main__":
    unittest.main()
