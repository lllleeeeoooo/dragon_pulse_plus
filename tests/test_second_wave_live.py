# -*- coding: utf-8 -*-
"""龙头二波独立实盘策略测试：候选识别 / 独立闸门 / 扫描买入(AI_SW) / 卖出(突破前高/N天未创新高) / 盈亏纳入"""
import datetime
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from core.signal_flags import compute_signal_flags
from scheduler.monitor_core import _MonitorCoreMixin


class TestSecondWaveSignal(unittest.TestCase):
    """二波候选：龙头 + 回撤30-50% + 涨幅>3%"""

    def test_命中与不命中(self):
        spot = pd.DataFrame([
            {"code": "600001", "name": "A", "price": 13.0, "change_pct": 4.0, "amount": 5e8,
             "volume": 5e7, "volume_ratio": 3.0, "high": 13.5, "low": 12.5, "open": 12.8,
             "pre_close": 12.5, "amplitude": 8.0},
            {"code": "600002", "name": "B", "price": 13.0, "change_pct": 4.0, "amount": 5e8,
             "volume": 5e7, "volume_ratio": 3.0, "high": 13.5, "low": 12.5, "open": 12.8,
             "pre_close": 12.5, "amplitude": 8.0},
            {"code": "600003", "name": "C", "price": 8.0, "change_pct": 4.0, "amount": 5e8,
             "volume": 5e7, "volume_ratio": 3.0, "high": 8.5, "low": 7.5, "open": 7.8,
             "pre_close": 7.7, "amplitude": 10.0},
            {"code": "600004", "name": "D", "price": 15.0, "change_pct": 1.0, "amount": 5e8,
             "volume": 5e7, "volume_ratio": 3.0, "high": 15.5, "low": 14.5, "open": 14.8,
             "pre_close": 14.9, "amplitude": 6.0},
        ])
        dragons = {"600001": 20.0, "600003": 20.0, "600004": 20.0}  # peak 20
        df = compute_signal_flags(spot, dragons=dragons)
        self.assertTrue(bool(df.iloc[0]["_signal_second_wave"]))   # 龙头+回撤35%+涨幅4%
        self.assertFalse(bool(df.iloc[1]["_signal_second_wave"]))  # 非龙头
        self.assertFalse(bool(df.iloc[2]["_signal_second_wave"]))  # 回撤60%>50%
        self.assertFalse(bool(df.iloc[3]["_signal_second_wave"]))  # 涨幅1%<3%

    def test_无龙头参数全部False(self):
        spot = pd.DataFrame([{"code": "600001", "name": "A", "price": 13.0, "change_pct": 4.0,
                              "amount": 5e8, "volume": 5e7, "volume_ratio": 3.0, "high": 13.5,
                              "low": 12.5, "open": 12.8, "pre_close": 12.5, "amplitude": 8.0}])
        df = compute_signal_flags(spot)
        self.assertFalse(bool(df.iloc[0]["_signal_second_wave"]))


class TestSecondWaveGates(unittest.TestCase):
    """二波独立闸门"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._sw_auto_bought_codes = set()
        self._loss = patch.object(_MonitorCoreMixin, "_is_daily_loss_breaker_triggered",
                                  return_value=False, create=True)
        self._loss.start()
        self.addCleanup(self._loss.stop)

    def test_独立持仓上限拦截(self):
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings",
                   return_value=[{"code": "A"}, {"code": "B"}]):
            self.assertFalse(self.m._second_wave_gates_open("600001", 5.0, False))

    def test_独立当日次数拦截(self):
        self.m._sw_auto_bought_codes = {"600001", "600002"}
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]):
            self.assertFalse(self.m._second_wave_gates_open("600003", 5.0, False))

    def test_正常放行(self):
        with patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]):
            self.assertTrue(self.m._second_wave_gates_open("600001", 5.0, False))


class TestScanSecondWave(unittest.TestCase):
    """二波扫描：闸门+LLM+AI_SW 买入 + strategy 带 peak"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._sw_auto_bought_codes = set()
        self._add = patch("scheduler.monitor_core.HoldingManager.add_holding")
        self._add_mock = self._add.start()
        self.addCleanup(self._add.stop)
        self._patches = [
            patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]),
            patch("scheduler.monitor_core.bark_notifier.send"),
            patch.object(self.m, "_sw_dragons", return_value={"600001": 20.0}),
            patch.object(self.m, "_get_ma_prices", return_value={"ma5": 12.0, "ma10": 11.0}),
            patch.object(_MonitorCoreMixin, "is_trading_time", return_value=True),
            patch.object(_MonitorCoreMixin, "_second_wave_gates_open", return_value=True),
            patch.object(_MonitorCoreMixin, "_llm_confirm_buy", return_value=("llm", True)),
            patch.object(_MonitorCoreMixin, "_recheck_buy_after_llm",
                         return_value=(13.0, True, False)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _spot(self):
        return pd.DataFrame([{"code": "600001", "name": "A", "price": 13.0, "change_pct": 4.0,
                              "amount": 5e8, "volume": 5e7, "volume_ratio": 3.0, "high": 13.5,
                              "low": 12.5, "open": 12.8, "pre_close": 12.5, "amplitude": 8.0}])

    def test_命中则AI_SW买入且strategy带peak(self):
        self.m._scan_second_wave(self._spot(), False)
        self.assertTrue(self._add_mock.called)
        kw = self._add_mock.call_args.kwargs
        self.assertEqual(kw.get("holding_type"), "AI_SW")
        self.assertIn("PEAK20.0", kw.get("strategy", ""))
        self.assertIn("600001", self.m._sw_auto_bought_codes)


class TestSecondWaveExit(unittest.TestCase):
    """二波卖出：突破前高兑现 / N 天未创新高离场"""

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
        self.m._monitor_holdings(spot_df, [holding], 5, 20.0)

    def test_黄金分割止盈(self):
        """PEAK20.0/VALLEY12.0 → tp1=12+(20-12)×0.618=16.94；high≥tp1 → 止盈（不再死守突破前高20）"""
        holding = {"code": "600001", "name": "A", "cost_price": 13.0, "holding_type": "AI_SW",
                   "buy_strategy": "二波战法-PEAK20.0-VALLEY12.0", "buy_date": "2026-07-25",
                   "was_limit_up_today": False}
        spot = pd.DataFrame([{"code": "600001", "price": 17.0, "change_pct": 5.0, "open": 16.5,
                              "high": 17.0, "low": 16.0, "volume": 1000000, "amount": 17000000}])
        self._run(spot, holding)
        self.assertTrue(self._close_mock.called)
        self.assertEqual(self._close_mock.call_args.kwargs.get("holding_type"), "AI_SW")
        self.assertAlmostEqual(self._close_mock.call_args.kwargs.get("sell_price"),
                               round(17.0 * 0.997, 2), places=2)

    def test_N天未达止盈位离场(self):
        """已持≥5天且 high < tp1 → 坚决离场（不恋战）"""
        holding = {"code": "600001", "name": "A", "cost_price": 13.0, "holding_type": "AI_SW",
                   "buy_strategy": "二波战法-PEAK20.0-VALLEY12.0", "buy_date": "2026-07-01",
                   "was_limit_up_today": False}
        spot = pd.DataFrame([{"code": "600001", "price": 12.5, "change_pct": 1.0, "open": 12.5,
                              "high": 12.8, "low": 12.0, "volume": 1000000, "amount": 12500000}])
        self._run(spot, holding)
        self.assertTrue(self._close_mock.called)
        self.assertEqual(self._close_mock.call_args.kwargs.get("holding_type"), "AI_SW")


class TestPnlIncludeSw(unittest.TestCase):
    """每日盈亏报告纳入 AI_SW"""

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

    def test_盈亏报告纳入AI_SW(self):
        from database import HoldingManager
        HoldingManager.add_holding(code="600001", name="A", cost_price=13.0,
                                   holding_type="AI_SW", strategy="二波战法-PEAK20.0")
        report = HoldingManager.get_daily_pnl_report()
        self.assertEqual(report.get("active_positions"), 1)
        codes = [h.get("code") for h in report.get("holdings", [])]
        self.assertIn("600001", codes)


class TestGroundVolumeBottom(unittest.TestCase):
    """二波地量止跌确认（评审：防 A 杀半山腰/死猫跳）"""

    def _rows(self, tail_vol, tail_amp, peak_vol=1e8, n=30):
        rows = []
        for i in range(n):
            r = Mock()
            r.close = 10.0 + i * 0.1
            r.high = r.close + 0.2
            r.low = r.close - 0.2
            # 开头有峰值量，末尾 4 天用 tail_vol/tail_amp
            r.volume = peak_vol if i == 0 else (tail_vol if i >= n - 5 else 3e7)
            r.amplitude = tail_amp if i >= n - 5 else 8.0
            rows.append(r)
        return rows[::-1]  # 模拟查询 desc 排序（方法内会再反转为升序）

    def _run(self, rows, today_vol):
        from scheduler.monitor_core import _MonitorCoreMixin
        m = _MonitorCoreMixin()
        with patch("database.connection.db_manager.get_session") as gs:
            sess = Mock()
            gs.return_value = sess
            sess.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows
            sess.close = Mock()
            return m._second_wave_bottom_confirmed("600001", today_vol)

    def test_地量缩量收敛确认通过(self):
        # 峰值1e8，尾部地量5e6(≤0.35×1e8)，振幅3%收敛 → 确认
        rows = self._rows(tail_vol=5e6, tail_amp=3.0)
        self.assertTrue(self._run(rows, 6e6))  # 今日量>地量日

    def test_未缩量不确认(self):
        # 尾部量仍大(4e7 > 0.35×1e8) → 未地量 → 不确认
        rows = self._rows(tail_vol=4e7, tail_amp=3.0)
        self.assertFalse(self._run(rows, 5e7))

    def test_振幅未收敛不确认(self):
        # 地量但振幅8%未收敛 → 未止跌 → 不确认
        rows = self._rows(tail_vol=5e6, tail_amp=8.0)
        self.assertFalse(self._run(rows, 6e6))

    def test_今日未放量不确认(self):
        # 地量收敛，但今日量(1e6) < 地量日(5e6) → 未点火 → 不确认
        rows = self._rows(tail_vol=5e6, tail_amp=3.0)
        self.assertFalse(self._run(rows, 1e6))

    def test_数据不足放行(self):
        self.assertTrue(self._run(self._rows(5e6, 3.0, n=10), 6e6))  # 仅10根 → 放行


if __name__ == "__main__":
    unittest.main()
