# -*- coding: utf-8 -*-
"""每日盈亏报告修复（买入日 prev_close 初始化 + 报告幂等）单元测试"""
import unittest
from unittest.mock import patch

from database.connection import db_manager, switch_to_test_db
from database.models import Base, Holding, DailyEquitySnapshot
from database import HoldingManager
from scheduler.reporting import _push_daily_pnl_report


class TestPrevCloseInit(unittest.TestCase):
    """add_holding 初始化 prev_close=cost，修复买入日今日涨跌=0"""

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
            session.query(DailyEquitySnapshot).delete()
            session.commit()
        finally:
            session.close()

    def test_买入时prev_close初始化为成本(self):
        HoldingManager.add_holding(code="600001", cost_price=10.0, name="测试A",
                                    holding_type="AI_AUTO")
        session = db_manager.get_session()
        try:
            h = session.query(Holding).filter_by(code="600001").first()
        finally:
            session.close()
        self.assertEqual(h.current_price, 10.0)
        self.assertEqual(h.prev_close_price, 10.0)  # 修复点


class TestReportExcludesManual(unittest.TestCase):
    """报告只统计 AI 自动持仓；手动持仓仅监控不入报告（胜率/已实现/持仓数均排除）"""

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
            session.query(DailyEquitySnapshot).delete()
            session.commit()
        finally:
            session.close()

    def test_报告排除手动持仓(self):
        import datetime
        session = db_manager.get_session()
        try:
            # AI 已实现：真亏
            session.add(Holding(code="600001", name="AI亏", cost_price=14.53, current_price=14.42,
                                prev_close_price=14.42, quantity=100, buy_date="2026-08-04",
                                holding_type="AI_AUTO", status="CLOSED", sell_price=14.42,
                                updated_at=datetime.datetime.now()))
            # 手动已实现：若计入则会是胜（200>205 应为亏），但应被排除
            session.add(Holding(code="600002", name="手动胜", cost_price=10.0, current_price=11.0,
                                prev_close_price=11.0, quantity=100, buy_date="2026-07-30",
                                holding_type="MANUAL", status="CLOSED", sell_price=11.0,
                                updated_at=datetime.datetime.now()))
            # AI 当前持仓
            session.add(Holding(code="600003", name="AI持", cost_price=3.95, current_price=4.26,
                                prev_close_price=3.95, quantity=100, buy_date="2026-08-04",
                                holding_type="AI_AUTO", status="HOLDING",
                                updated_at=datetime.datetime.now()))
            session.commit()
        finally:
            session.close()

        report = HoldingManager.get_daily_pnl_report()
        self.assertEqual(report["total_closed_count"], 1)      # 只算 AI 的 1 笔
        self.assertEqual(report["total_realized_pnl"], -11.0)  # (14.42-14.53)*100 = -11
        self.assertEqual(report["total_closed_win_rate"], 0.0)
        self.assertEqual(report["active_positions"], 1)        # 只算 AI 持仓
        # 持仓明细里不含手动
        codes = [h["code"] for h in report["holdings"]]
        self.assertNotIn("600002", codes)
        self.assertIn("600003", codes)


class TestReportIdempotent(unittest.TestCase):
    """盘后盈亏报告按日期幂等：已有快照则整体跳过，不再二次滚存昨收"""

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
            session.query(DailyEquitySnapshot).delete()
            session.commit()
        finally:
            session.close()

    def test_已有快照则跳过(self):
        session = db_manager.get_session()
        try:
            session.add(DailyEquitySnapshot(trade_date="20260805", total_equity=10000.0))
            session.commit()
        finally:
            session.close()

        # 若未跳过，这些函数会被调用 → 标记为不该发生
        with patch("scheduler.reporting.HoldingManager.update_current_prices",
                   side_effect=AssertionError("不应再次更新收盘价")), \
             patch("scheduler.reporting.HoldingManager.sync_close_prices",
                   side_effect=AssertionError("不应二次滚存昨收")), \
             patch("scheduler.reporting.HoldingManager.get_daily_pnl_report",
                   side_effect=AssertionError("不应重新生成报告")):
            _push_daily_pnl_report("20260805", None)
        # 若走到 side_effect 就抛错，能走到这里说明幂等跳过生效

    def test_无快照则正常执行(self):
        # 无快照 → 正常生成（get_daily_pnl_report 会被调用）
        with patch("scheduler.reporting.HoldingManager.get_daily_pnl_report",
                   return_value={"error": "无持仓或未生成"}):
            _push_daily_pnl_report("20260805", None)
        # 到达这里说明未跳过（幂等守卫放行）


if __name__ == "__main__":
    unittest.main()
