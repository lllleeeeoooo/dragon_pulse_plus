# -*- coding: utf-8 -*-
"""B方案：LLM 最终决策（规则先拦，LLM 复核，失败降级）单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from database.connection import db_manager, switch_to_test_db
from database.models import Base, Holding
from database import HoldingManager
from llm.sell_advisor import DynamicSellAdvisor
from scheduler.monitor_core import _MonitorCoreMixin


class TestParseVerdict(unittest.TestCase):

    def test_买入(self):
        self.assertEqual(DynamicSellAdvisor._parse_verdict("🔥买入：放量突破分时均线"), "买入")

    def test_出货(self):
        self.assertEqual(DynamicSellAdvisor._parse_verdict("💀出货：跌破VWAP量能萎缩"), "出货")

    def test_观望(self):
        self.assertEqual(DynamicSellAdvisor._parse_verdict("👀观望：高位震荡需确认"), "观望")

    def test_空串(self):
        self.assertEqual(DynamicSellAdvisor._parse_verdict(""), "")


class TestLLMConfirmBuy(unittest.TestCase):

    def setUp(self):
        self.m = _MonitorCoreMixin()

    def test_LLM判买入(self):
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_buy_decision",
                   return_value="🔥买入：放量突破"):
            src, allow = self.m._llm_confirm_buy("600001", "A", 10, 5, 3, "点火", [])
        self.assertEqual((src, allow), ("llm", True))

    def test_LLM判观望不买(self):
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_buy_decision",
                   return_value="👀观望：高位放量需谨慎"):
            src, allow = self.m._llm_confirm_buy("600001", "A", 10, 5, 3, "点火", [])
        self.assertEqual((src, allow), ("llm", False))

    def test_LLM失败降级规则(self):
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_buy_decision",
                   return_value=""):
            src, allow = self.m._llm_confirm_buy("600001", "A", 10, 5, 3, "点火", [])
        self.assertEqual((src, allow), ("rule", True))


class TestLLMConfirmSell(unittest.TestCase):

    def setUp(self):
        self.m = _MonitorCoreMixin()

    def test_LLM判出货卖(self):
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_sell_decision",
                   return_value="💀出货：跌破MA5放量"):
            src, do = self.m._llm_confirm_sell({"code": "600001", "cost_price": 10.0},
                                               {"type": "破位止损"}, 9.0, -10.0, 3)
        self.assertEqual((src, do), ("llm", True))

    def test_LLM判持有不卖(self):
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_sell_decision",
                   return_value="👀持有：缩量假摔"):
            src, do = self.m._llm_confirm_sell({"code": "600001", "cost_price": 10.0},
                                               {"type": "破位止损"}, 9.0, -10.0, 3)
        self.assertEqual((src, do), ("llm", False))

    def test_LLM失败降级卖(self):
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_sell_decision",
                   return_value=""):
            src, do = self.m._llm_confirm_sell({"code": "600001", "cost_price": 10.0},
                                               {"type": "破位止损"}, 9.0, -10.0, 3)
        self.assertEqual((src, do), ("rule", True))


class TestRecheckBuy(unittest.TestCase):
    """LLM 等待后买前复核：封板/跌停/回落不买，正常用最新价"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        # _is_limit_up/_is_limit_down 定义在别的 mixin，打到类上 mock 为"主板±9.5%"行为
        self._p1 = patch.object(_MonitorCoreMixin, "_is_limit_up",
                                staticmethod(lambda code, chg: chg >= 9.5), create=True)
        self._p2 = patch.object(_MonitorCoreMixin, "_is_limit_down",
                                staticmethod(lambda code, chg: chg <= -9.5), create=True)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_封板不买(self):
        spot = pd.DataFrame([{"code": "600001", "price": 11.0, "change_pct": 10.0,
                              "open": 10.3, "pre_close": 10.0}])
        with patch("scheduler.monitor_core.DataFetcher.get_realtime_spot", return_value=spot):
            price, ok = self.m._recheck_buy_after_llm("600001", 10.0)
        self.assertFalse(ok)

    def test_回落超阈值不买(self):
        # open 10.5(+5%)，现 10.1(+1%) → 回落 4% > 2% → 不买
        spot = pd.DataFrame([{"code": "600001", "price": 10.1, "change_pct": 1.0,
                              "open": 10.5, "pre_close": 10.0}])
        with patch("scheduler.monitor_core.DataFetcher.get_realtime_spot", return_value=spot):
            price, ok = self.m._recheck_buy_after_llm("600001", 10.0)
        self.assertFalse(ok)

    def test_正常用最新价(self):
        spot = pd.DataFrame([{"code": "600001", "price": 10.6, "change_pct": 6.0,
                              "open": 10.3, "pre_close": 10.0}])
        with patch("scheduler.monitor_core.DataFetcher.get_realtime_spot", return_value=spot):
            price, ok = self.m._recheck_buy_after_llm("600001", 10.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(price, 10.6)

    def test_快照失败不买(self):
        # 审查#4：复核拿不到最新快照 → fail-closed 不买，避免用 LLM 前旧价记录不可达成交
        with patch("scheduler.monitor_core.DataFetcher.get_realtime_spot",
                   return_value=pd.DataFrame()):
            price, ok = self.m._recheck_buy_after_llm("600001", 10.0)
        self.assertFalse(ok)


class TestSellHoldCooldown(unittest.TestCase):
    """卖出 LLM 判「持有」后冷却：持续信号不每 15s 重复咨询 LLM（审计#3 补充）"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._pending_sell_codes = set()
        self.holding = {
            "code": "600001", "name": "A", "cost_price": 10.0,
            "current_price": 9.0, "holding_type": "AI_AUTO",
            "buy_date": "2026-01-01", "was_limit_up_today": False,
        }
        self.spot = pd.DataFrame([{
            "code": "600001", "price": 9.0, "change_pct": -10.0,
            "open": 9.5, "high": 9.6, "low": 9.0, "volume": 1000000, "amount": 9000000,
        }])
        self._sell = patch("scheduler.monitor_core.DynamicSellAdvisor.format_sell_decision",
                           return_value="👀持有：缩量假摔")
        self._patches = [
            patch.object(self.m, "_get_ma_prices", return_value={"ma5": 9.5}),
            patch("scheduler.monitor_core.HoldingMonitor.check_sell_signals",
                  return_value=[{"type": "破位止损", "level": "HIGH", "reason": "跌破MA5"}]),
            patch("scheduler.monitor_core.HoldingManager.batch_update_profit_rates"),
            patch("scheduler.monitor_core.HoldingManager.update_was_limit_up"),
            patch("scheduler.monitor_core.HoldingManager.close_holding"),
            patch("scheduler.monitor_core.bark_notifier.send"),
            patch.object(_MonitorCoreMixin, "_is_limit_up", staticmethod(lambda c, chg: False), create=True),
            patch.object(_MonitorCoreMixin, "_is_limit_down", staticmethod(lambda c, chg: False), create=True),
            self._sell,
        ]
        self._sell_mock = None
        for _p in self._patches:
            m = _p.start()
            if _p is self._sell:
                self._sell_mock = m
        self.addCleanup(self._teardown)

    def _teardown(self):
        for _p in self._patches:
            _p.stop()

    def test_判持有后冷却期不重复咨询(self):
        self.m._monitor_holdings(self.spot, [self.holding], 5, 20.0)
        self.assertEqual(self._sell_mock.call_count, 1)
        # 冷却未到期：再轮询不应重新咨询 LLM
        self.m._monitor_holdings(self.spot, [self.holding], 5, 20.0)
        self.assertEqual(self._sell_mock.call_count, 1)

    def test_冷却到期后重新复核(self):
        self.m._monitor_holdings(self.spot, [self.holding], 5, 20.0)
        # 冷却键为 "code:sig_type"（审查#2），统一拨到已过期
        for _k in self.m._llm_sell_hold_until:
            self.m._llm_sell_hold_until[_k] = 0.0
        self.m._monitor_holdings(self.spot, [self.holding], 5, 20.0)
        self.assertEqual(self._sell_mock.call_count, 2)

    def test_冷却按信号类型区分(self):
        """审查#2：破位止损判持有的冷却，不拦截同股更严重的断板必卖"""
        # setUp 已 patch 好 _sell/close_holding/_is_limit_up 等，此处只需覆盖信号列表
        with patch("scheduler.monitor_core.HoldingMonitor.check_sell_signals",
                   return_value=[
                       {"type": "破位止损", "level": "HIGH", "reason": "跌破MA5"},
                       {"type": "断板必卖", "level": "CRITICAL", "reason": "断板"},
                   ]):
            self.m._monitor_holdings(self.spot, [self.holding], 5, 20.0)
            self.assertEqual(self._sell_mock.call_count, 2)  # 两个信号各咨询一次，未互相拦截


class TestDecisionSourceRecord(unittest.TestCase):
    """买入决策来源落库"""

    @classmethod
    def setUpClass(cls):
        switch_to_test_db()
        Base.metadata.drop_all(bind=db_manager.engine)
        Base.metadata.create_all(bind=db_manager.engine)

    @classmethod
    def tearDownClass(cls):
        db_manager.engine.dispose()

    def setUp(self):
        session = db_manager.get_session()
        try:
            session.query(Holding).delete()
            session.commit()
        finally:
            session.close()

    def test_decision_source落库(self):
        HoldingManager.add_holding(code="600001", cost_price=10.0, name="A",
                                   holding_type="AI_AUTO", decision_source="llm")
        session = db_manager.get_session()
        try:
            h = session.query(Holding).filter_by(code="600001").first()
        finally:
            session.close()
        self.assertEqual(h.decision_source, "llm")


if __name__ == "__main__":
    unittest.main()
