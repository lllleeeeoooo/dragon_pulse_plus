# -*- coding: utf-8 -*-
"""尾盘博弈实盘接入测试：独立闸门 / 扫描买入(AI_TAIL) / 次日早盘兑现 / 盈亏报告纳入"""
import datetime
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scheduler.monitor_core import _MonitorCoreMixin


def _tail_spot():
    """一行尾盘博弈命中（涨幅4%/量比2温和放量/收阳/短上影/收盘≥均价）的 spot"""
    return pd.DataFrame([{
        "code": "600002", "name": "B", "price": 10.4, "change_pct": 4.0, "amount": 6e8,
        "volume": 6e7, "volume_ratio": 2.0, "high": 10.45, "low": 10.0, "open": 10.1,
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


def _tail_spot_two():
    """两行尾盘博弈命中候选（A 量比2.4/B 量比2.0），用于统一选排序与买满测试"""
    return pd.DataFrame([
        {"code": "600001", "name": "A", "price": 10.2, "change_pct": 3.0, "amount": 5e8,
         "volume": 5e7, "volume_ratio": 2.4, "high": 10.25, "low": 10.0, "open": 10.1,
         "pre_close": 10.0, "amplitude": 3.0},
        {"code": "600002", "name": "B", "price": 10.4, "change_pct": 4.0, "amount": 6e8,
         "volume": 6e7, "volume_ratio": 2.0, "high": 10.45, "low": 10.0, "open": 10.1,
         "pre_close": 10.0, "amplitude": 4.0},
    ])


class TestScanTailGame(unittest.TestCase):
    """尾盘博弈扫描：先入池(不买) → 选时点统一LLM选 → 按序买入 + 独立计数"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._tail_auto_bought_codes = set()
        self.m._tail_pool = {}
        self.m._tail_select_order = []
        self.m._tail_select_source = ""
        self.m._tail_select_attempted = set()
        self.m._tail_select_done = False
        self._add = patch("scheduler.monitor_core.HoldingManager.add_holding")
        self._add_mock = self._add.start()
        self.addCleanup(self._add.stop)
        self._patches = [
            patch("scheduler.monitor_core.HoldingManager.get_active_holdings", return_value=[]),
            patch("scheduler.monitor_core.bark_notifier.send"),
            patch.object(self.m, "_get_ma_prices", return_value={"ma5": 10.0, "ma10": 9.0}),
            patch.object(_MonitorCoreMixin, "is_tail_end_time", return_value=True),
            patch.object(_MonitorCoreMixin, "_tail_gates_open", return_value=True),
            patch.object(_MonitorCoreMixin, "_tail_above_ma", return_value=True),
            patch.object(_MonitorCoreMixin, "_recheck_buy_after_llm",
                         return_value=(10.4, True, False)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_选时点前仅入池不买(self):
        """窗口内(选时点前)候选只收进池，不做买入"""
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=False):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertFalse(self._add_mock.called)
        self.assertIn("600002", self.m._tail_pool)

    def test_选时点后按LLM序买入(self):
        """选时点后按统一LLM选购顺序买入，独立计数入 _tail_auto_bought_codes"""
        self.m._tail_pool = {"600002": {"code": "600002", "name": "B", "price": 10.4,
                                        "change_pct": 4.0, "vol_ratio": 2.0, "amt_billion": 6.0}}
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True), \
             patch.object(_MonitorCoreMixin, "_tail_llm_select", return_value=["600002"]):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertTrue(self._add_mock.called)
        kw = self._add_mock.call_args.kwargs
        self.assertEqual(kw.get("holding_type"), "AI_TAIL")
        self.assertIn("600002", self.m._tail_auto_bought_codes)
        self.assertEqual(self.m._tail_select_source, "llm")

    def test_LLM失败降级规则买入(self):
        """统一LLM失败 → 降级规则（量比涨幅前2）兜底买入，decision_source=rule"""
        self.m._tail_pool = {"600002": {"code": "600002", "name": "B", "price": 10.4,
                                        "change_pct": 4.0, "vol_ratio": 2.0, "amt_billion": 6.0}}
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True), \
             patch.object(_MonitorCoreMixin, "_tail_llm_select", return_value=[]):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertTrue(self._add_mock.called)
        self.assertEqual(self.m._tail_select_source, "rule")

    def test_空池即done(self):
        """选时点池内无有效候选 → 不买并结束选购"""
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertFalse(self._add_mock.called)
        self.assertTrue(self.m._tail_select_done)

    def test_池中候选已劣化跳过(self):
        """选购顺序已冻结，但当前 spot 已不满足尾盘信号(涨幅1%<2%) → 终态跳过不买"""
        self.m._tail_select_order = ["600002"]
        degraded = pd.DataFrame([{"code": "600002", "name": "B", "price": 10.4, "change_pct": 1.0,
                                  "amount": 6e8, "volume": 6e7, "volume_ratio": 2.0, "high": 10.45,
                                  "low": 10.0, "open": 10.1, "pre_close": 10.0, "amplitude": 4.0}])
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True):
            self.m._scan_tail_game(degraded, False)
        self.assertFalse(self._add_mock.called)
        self.assertIn("600002", self.m._tail_select_attempted)
        self.assertTrue(self.m._tail_select_done)

    def test_闸门拦截不买(self):
        """选购候选被闸门拦截 → 标记终态不买"""
        self.m._tail_select_order = ["600002"]
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True), \
             patch.object(_MonitorCoreMixin, "_tail_gates_open", return_value=False):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertFalse(self._add_mock.called)
        self.assertIn("600002", self.m._tail_select_attempted)

    def test_买满done(self):
        """统一LLM选2只一轮买满 → done，后续不再扫"""
        self.m._tail_pool = {
            "600001": {"code": "600001", "name": "A", "price": 10.2, "change_pct": 3.0,
                       "vol_ratio": 2.4, "amt_billion": 5.0},
            "600002": {"code": "600002", "name": "B", "price": 10.4, "change_pct": 4.0,
                       "vol_ratio": 2.0, "amt_billion": 6.0},
        }
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True), \
             patch.object(_MonitorCoreMixin, "_tail_llm_select", return_value=["600001", "600002"]):
            self.m._scan_tail_game(_tail_spot_two(), False)
        self.assertEqual(len(self.m._tail_auto_bought_codes), 2)
        self.assertTrue(self.m._tail_select_done)

    def test_复核瞬时失败下轮重试(self):
        """买前复核瞬时失败(封板/快照不可用) → 不标记终态，下轮重试"""
        self.m._tail_pool = {"600002": {"code": "600002", "name": "B", "price": 10.4,
                                        "change_pct": 4.0, "vol_ratio": 2.0, "amt_billion": 6.0}}
        recheck = patch.object(_MonitorCoreMixin, "_recheck_buy_after_llm",
                               side_effect=[(10.4, False, True), (10.4, True, False)])
        recheck.start()
        self.addCleanup(recheck.stop)
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True), \
             patch.object(_MonitorCoreMixin, "_tail_llm_select", return_value=["600002"]):
            self.m._scan_tail_game(_tail_spot(), False)  # 第1轮：瞬时失败
        self.assertFalse(self._add_mock.called)
        self.assertNotIn("600002", self.m._tail_select_attempted)
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True):
            self.m._scan_tail_game(_tail_spot(), False)  # 第2轮：复核通过买入
        self.assertTrue(self._add_mock.called)

    def test_复核最终失败标记(self):
        """买前复核最终失败(跌停/冲高回落) → 标记终态并结束选购"""
        self.m._tail_pool = {"600002": {"code": "600002", "name": "B", "price": 10.4,
                                        "change_pct": 4.0, "vol_ratio": 2.0, "amt_billion": 6.0}}
        with patch.object(_MonitorCoreMixin, "_tail_select_time_reached", return_value=True), \
             patch.object(_MonitorCoreMixin, "_tail_llm_select", return_value=["600002"]), \
             patch.object(_MonitorCoreMixin, "_recheck_buy_after_llm",
                          return_value=(10.4, False, False)):
            self.m._scan_tail_game(_tail_spot(), False)
        self.assertFalse(self._add_mock.called)
        self.assertIn("600002", self.m._tail_select_attempted)
        self.assertTrue(self.m._tail_select_done)

    def test_LLM统一选解析保序并过滤幻觉码(self):
        """统一LLM文本解析：按序提取6位代码，过滤不在候选集内的幻觉码"""
        self.m._current_market_style = {
            "style": "低吸", "reason": "测试", "zt_count": 30, "dt_count": 3,
            "zhaban_count": 5, "zhaban_rate": 10.0, "height": 5,
            "sentiment_index": 60, "capacity_factor": 1.1, "cycle_phase": "进攻",
            "promotion_rate": 50, "premium_opening": 2.0, "premium_intraday": 1.0,
            "high_open_ratio": 40, "positive_ratio": 60, "total_count": 80,
        }
        cands = [{"code": "600001", "name": "A"}, {"code": "600002", "name": "B"}]
        with patch("scheduler.monitor_core.DynamicSellAdvisor.format_tail_select_decision",
                   return_value="600002\n999999\n600001"):
            order = self.m._tail_llm_select(cands, False)
        self.assertEqual(order, ["600002", "600001"])  # 幻觉码 999999 被过滤，且保持 LLM 输出顺序

    def test_非尾盘时段不买(self):
        with patch.object(_MonitorCoreMixin, "is_tail_end_time", return_value=False):
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

    def _run(self, spot_df, holding, hour=9, minute=45):
        now = datetime.datetime(2026, 8, 6, hour, minute)
        with patch("scheduler.monitor_core.datetime.datetime") as dt:
            dt.now.return_value = now
            dt.time.return_value = now.time()
            self.m._monitor_holdings(spot_df, [holding], 5, 20.0)

    def test_09_35统一卖出(self):
        """到点(≥TAIL_GAME_SELL_TIME=09:35)按现价清仓 AI_TAIL（不区分高开低开）"""
        holding = {"code": "600002", "name": "B", "cost_price": 10.0, "holding_type": "AI_TAIL",
                   "buy_date": "2026-08-05", "was_limit_up_today": False}
        spot = pd.DataFrame([{"code": "600002", "price": 10.5, "change_pct": 5.0, "open": 10.3,
                              "high": 10.8, "low": 10.0, "volume": 1000000, "amount": 10500000}])
        self._run(spot, holding, hour=9, minute=36)  # 09:36 ≥ 09:35
        self.assertTrue(self._close_mock.called)
        kw = self._close_mock.call_args.kwargs
        self.assertEqual(kw.get("holding_type"), "AI_TAIL")
        self.assertAlmostEqual(kw.get("sell_price"), round(10.5 * 0.997, 2), places=2)  # 现价×滑点

    def test_09_35前持有(self):
        """09:35 前统一持有不卖（次日必清，到点清仓）"""
        holding = {"code": "600002", "name": "B", "cost_price": 10.0, "holding_type": "AI_TAIL",
                   "buy_date": "2026-08-05", "was_limit_up_today": False}
        spot = pd.DataFrame([{"code": "600002", "price": 10.5, "change_pct": 5.0, "open": 10.3,
                              "high": 10.8, "low": 10.0, "volume": 1000000, "amount": 10500000}])
        self._run(spot, holding, hour=9, minute=30)  # 09:30 < 09:35
        self.assertFalse(self._close_mock.called)


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


class TestTailWindow(unittest.TestCase):
    """尾盘博弈买入窗口（默认 14:45-14:52：候选先入池，14:51 统一选，窗口末 14:52 前完成）"""

    def test_窗口14_45到14_52(self):
        from unittest.mock import Mock
        import scheduler.monitor_core as mc
        from scheduler.monitor_core import _MonitorCoreMixin
        m = _MonitorCoreMixin()
        # patch 整个模块的 datetime 引用：datetime(类) mock + time(类) 用真实现
        fake = Mock()
        fake.time = datetime.time  # 真实 time 类
        with patch.object(mc, "datetime", fake):
            for hh, mm, expected in [(14, 30, False), (14, 44, False), (14, 45, True),
                                     (14, 51, True), (14, 52, True), (14, 53, False), (15, 0, False)]:
                fake.datetime.now.return_value = datetime.datetime(2026, 8, 6, hh, mm)  # 周四
                self.assertEqual(m.is_tail_end_time(), expected, f"{hh}:{mm}")


class TestTailSelectTime(unittest.TestCase):
    """尾盘博弈统一选时点 _tail_select_time_reached（TAIL_GAME_SELECT_TIME=14:51）"""

    def test_14_51前为False_到点及之后为True(self):
        from unittest.mock import Mock
        import scheduler.monitor_core as mc
        from scheduler.monitor_core import _MonitorCoreMixin
        m = _MonitorCoreMixin()
        fake = Mock()
        fake.time = datetime.time  # 真实 time 类
        with patch.object(mc, "datetime", fake):
            for hh, mm, expected in [(14, 50, False), (14, 51, True), (14, 52, True), (15, 0, True)]:
                fake.datetime.now.return_value = datetime.datetime(2026, 8, 6, hh, mm)  # 周四
                self.assertEqual(m._tail_select_time_reached(), expected, f"{hh}:{mm}")


if __name__ == "__main__":
    unittest.main()
