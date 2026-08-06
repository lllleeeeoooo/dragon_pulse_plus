# -*- coding: utf-8 -*-
"""尾盘博弈实盘接入测试：独立闸门 / 扫描买入(AI_TAIL) / 次日早盘兑现 / 盈亏报告纳入"""
import datetime
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scheduler.monitor_core import _MonitorCoreMixin


def _tail_spot():
    """一行尾盘博弈命中（涨幅4%/量比4/收阳/短上影/收盘≥均价）的 spot"""
    return pd.DataFrame([{
        "code": "600002", "name": "B", "price": 10.4, "change_pct": 4.0, "amount": 6e8,
        "volume": 6e7, "volume_ratio": 4.0, "high": 10.45, "low": 10.0, "open": 10.1,
        "pre_close": 10.0, "amplitude": 4.0,
    }])


class TestTailGates(unittest.TestCase):
    """尾盘博弈独立闸门：TAIL_MAX_POSITIONS / TAIL_MAX_DAILY_BUYS / 大盘熔断"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._tail_auto_bought_codes = set()
        # _is_daily_loss_breaker_triggered 定义在 _MonitorSignalsMixin，裸 mixin 需 create=True
        self._loss = patch.object(_MonitorCoreMixin, "_is_daily_loss_breaker_triggered",
                                  return_value=False, create=True)
        self._loss.start()
        self.addCleanup(self._loss.stop)

    def test_独立持仓上限拦截(self):
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings",
                   return_value=[{"code": "A"}, {"code": "B"}]):  # 已满 TAIL_MAX_POSITIONS=2
            self.assertFalse(self.m._tail_gates_open("600003", 5.0, False))

    def test_独立当日次数拦截(self):
        self.m._tail_auto_bought_codes = {"600001", "600002"}  # 已满 TAIL_MAX_DAILY_BUYS=2
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]):
            self.assertFalse(self.m._tail_gates_open("600003", 5.0, False))

    def test_大盘熔断拦截(self):
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]):
            self.assertFalse(self.m._tail_gates_open("600003", 5.0, True))

    def test_正常放行(self):
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]):
            self.assertTrue(self.m._tail_gates_open("600003", 5.0, False))


class TestTailAboveMa(unittest.TestCase):
    """指南：上升趋势站上 5/10 日均线（MA 缺失放行）"""

    def setUp(self):
        self.m = _MonitorCoreMixin()

    def test_站上均线放行(self):
        self.m._get_ma_prices = Mock(return_value={"ma5": 10.0, "ma10": 9.8})
        self.assertTrue(self.m._tail_above_ma("600002", 10.4))

    def test_低于均线拦截(self):
        self.m._get_ma_prices = Mock(return_value={"ma5": 10.5, "ma10": 10.4})
        self.assertFalse(self.m._tail_above_ma("600002", 10.4))

    def test_MA缺失放行(self):
        self.m._get_ma_prices = Mock(return_value={"ma5": None, "ma10": None})
        self.assertTrue(self.m._tail_above_ma("600002", 10.4))


class TestScanTailGame(unittest.TestCase):
    """尾盘博弈扫描：闸门+LLM+AI_TAIL 买入 + 独立计数"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._tail_auto_bought_codes = set()
        self._add = patch("scheduler.monitor_core.HoldingManager.add_holding")
        self._add_mock = self._add.start()
        self.addCleanup(self._add.stop)
        self._patches = [
            patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]),
            patch("scheduler.monitor_core.bark_notifier.send"),
            patch.object(self.m, "_get_ma_prices", return_value={"ma5": 10.0, "ma10": 9.0}),
            patch.object(_MonitorCoreMixin, "is_tail_end_time", return_value=True),
            patch.object(_MonitorCoreMixin, "_tail_gates_open", return_value=True),
            patch.object(_MonitorCoreMixin, "_llm_confirm_buy", return_value=("llm", True)),
            patch.object(_MonitorCoreMixin, "_recheck_buy_after_llm",
                         return_value=(10.4, True, False)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_命中则AI_TAIL买入并计数(self):
        self.m._scan_tail_game(_tail_spot(), False)
        self.assertTrue(self._add_mock.called)
        kw = self._add_mock.call_args.kwargs
        self.assertEqual(kw.get("holding_type"), "AI_TAIL")
        self.assertIn("600002", self.m._tail_auto_bought_codes)

    def test_非尾盘时段不买(self):
        with patch.object(_MonitorCoreMixin, "is_tail_end_time", return_value=False):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertFalse(self._add_mock.called)

    def test_闸门拦截不买(self):
        with patch.object(_MonitorCoreMixin, "_tail_gates_open", return_value=False):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertFalse(self._add_mock.called)


class TestTailGameExit(unittest.TestCase):
    """尾盘博弈次日早盘兑现卖出（09:30-10:30，AI_TAIL）"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._pending_sell_codes = set()
        self.m._alerted_sell_signals = {}
        self._close = patch("scheduler.monitor_core.HoldingManager.close_holding")
        self._close_mock = self._close.start()
        self.addCleanup(self._close.stop)
        self._patches = [
            patch("scheduler.monitor_core.HoldingManager.batch_update_profit_rates"),
            patch("scheduler.monitor_core.HoldingManager.update_was_limit_up"),
            patch("scheduler.monitor_core.bark_notifier.send"),
            patch.object(_MonitorCoreMixin, "_is_limit_up",
                         staticmethod(lambda c, chg: False), create=True),
            patch.object(_MonitorCoreMixin, "_is_limit_down",
                         staticmethod(lambda c, chg: False), create=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, spot_df, holding):
        now = datetime.datetime(2026, 8, 6, 9, 45)  # 09:45 在兑现窗口内
        with patch("scheduler.monitor_core.datetime.datetime") as dt:
            dt.now.return_value = now
            dt.time.return_value = now.time()
            self.m._monitor_holdings(spot_df, [holding], 5, 20.0)

    def test_高开冲高兑现(self):
        holding = {"code": "600002", "name": "B", "cost_price": 10.0, "holding_type": "AI_TAIL",
                   "buy_date": "2026-08-05", "was_limit_up_today": False}
        spot = pd.DataFrame([{"code": "600002", "price": 10.5, "change_pct": 5.0, "open": 10.3,
                              "high": 10.8, "low": 10.0, "volume": 1000000, "amount": 10500000}])
        self._run(spot, holding)
        self.assertTrue(self._close_mock.called)
        kw = self._close_mock.call_args.kwargs
        self.assertEqual(kw.get("holding_type"), "AI_TAIL")
        # 高开: open 10.3 ≥ 10×1.02=10.2 → open+(high-open)×0.5 = 10.55 → ×0.997 = 10.518
        self.assertAlmostEqual(kw.get("sell_price"), round(10.55 * 0.997, 2), places=2)

    def test_未高开按开盘兑现(self):
        holding = {"code": "600002", "name": "B", "cost_price": 10.0, "holding_type": "AI_TAIL",
                   "buy_date": "2026-08-05", "was_limit_up_today": False}
        spot = pd.DataFrame([{"code": "600002", "price": 10.1, "change_pct": 1.0, "open": 10.1,
                              "high": 10.2, "low": 10.0, "volume": 1000000, "amount": 10100000}])
        self._run(spot, holding)
        self.assertTrue(self._close_mock.called)
        kw = self._close_mock.call_args.kwargs
        # open 10.1 < 10.2 → 按 open 10.1 ×0.997 = 10.07
        self.assertAlmostEqual(kw.get("sell_price"), round(10.1 * 0.997, 2), places=2)


class TestPnlIncludeTail(unittest.TestCase):
    """每日盈亏报告纳入 AI_TAIL 持仓"""

    @classmethod
    def setUpClass(cls):
        from database.connection import db_manager, switch_to_test_db
        from database.models import Base
        switch_to_test_db()
        Base.metadata.drop_all(bind=db_manager.engine)
        Base.metadata.create_all(bind=db_manager.engine)

    @classmethod
    def tearDownClass(cls):
        from database.connection import db_manager
        db_manager.engine.dispose()

    def setUp(self):
        from database.connection import db_manager
        from database.models import Holding
        session = db_manager.get_session()
        try:
            session.query(Holding).delete()
            session.commit()
        finally:
            session.close()

    def test_盈亏报告纳入AI_TAIL(self):
        from database import HoldingManager
        HoldingManager.add_holding(code="600002", name="B", cost_price=10.0,
                                   holding_type="AI_TAIL", strategy="尾盘博弈-次日高开")
        report = HoldingManager.get_daily_pnl_report()
        self.assertEqual(report.get("active_positions"), 1)  # AI_TAIL 计入活跃持仓数
        codes = [h.get("code") for h in report.get("holdings", [])]
        self.assertIn("600002", codes)  # AI_TAIL 持仓进入盈亏报告明细


if __name__ == "__main__":
    unittest.main()
