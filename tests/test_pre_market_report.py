# -*- coding: utf-8 -*-
"""盘前简报持久化（断链4修复）单元测试"""
import unittest
from unittest.mock import patch

from database.connection import db_manager, switch_to_test_db
from database.models import Base, PreMarketReport
from database.pre_market_report import PreMarketReportManager


class TestPreMarketReport(unittest.TestCase):

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
            session.query(PreMarketReport).delete()
            session.commit()
        finally:
            session.close()

    def test_save_get_roundtrip(self):
        PreMarketReportManager.save("20260805", "今日重点关注AI与军工")
        self.assertEqual(PreMarketReportManager.get("20260805"), "今日重点关注AI与军工")

    def test_同日upsert覆盖(self):
        PreMarketReportManager.save("20260805", "第一版")
        PreMarketReportManager.save("20260805", "第二版")
        session = db_manager.get_session()
        try:
            rows = session.query(PreMarketReport).filter(
                PreMarketReport.trade_date == "20260805").all()
            self.assertEqual(len(rows), 1)  # unique trade_date，仅一条
        finally:
            session.close()
        self.assertEqual(PreMarketReportManager.get("20260805"), "第二版")

    def test_无记录返回空串(self):
        self.assertEqual(PreMarketReportManager.get("19990101"), "")

    def test_空报告不落库(self):
        PreMarketReportManager.save("20260805", "")
        self.assertEqual(PreMarketReportManager.get("20260805"), "")


class TestPreMarketSelfHeal(unittest.TestCase):
    """盘中自愈：08:30 盘前简报缺失（进程未运行/系统睡眠错过定时任务）时，交易时段补发一次"""

    def setUp(self):
        from scheduler.monitor_core import _MonitorCoreMixin
        self.m = _MonitorCoreMixin()

    def test_当日缺失时盘中补发一次(self):
        with patch("database.pre_market_report.PreMarketReportManager.get", return_value=""), \
             patch("scheduler.pre_market.job_pre_market") as jpm, \
             patch("scheduler.monitor_core.bark_notifier.send") as bark:
            self.m._self_heal_pre_market_report()
            self.assertEqual(jpm.call_count, 1)  # 缺失 → 补发
            self.assertTrue(bark.called)          # 补发告警推送
            # 每日只补一次：二次调用不再重复补发
            self.m._self_heal_pre_market_report()
            self.assertEqual(jpm.call_count, 1)

    def test_当日已有简报不补发(self):
        with patch("database.pre_market_report.PreMarketReportManager.get", return_value="今日简报"), \
             patch("scheduler.pre_market.job_pre_market") as jpm, \
             patch("scheduler.monitor_core.bark_notifier.send") as bark:
            self.m._self_heal_pre_market_report()
            jpm.assert_not_called()
            bark.assert_not_called()

    def test_补发异常不中断(self):
        with patch("database.pre_market_report.PreMarketReportManager.get", return_value=""), \
             patch("scheduler.pre_market.job_pre_market",
                   side_effect=RuntimeError("boom")), \
             patch("scheduler.monitor_core.bark_notifier.send"):
            self.m._self_heal_pre_market_report()  # 异常被捕获，不抛出


if __name__ == "__main__":
    unittest.main()
