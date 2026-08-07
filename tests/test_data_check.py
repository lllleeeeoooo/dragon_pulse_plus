# -*- coding: utf-8 -*-
"""盘前数据完整性检查测试：check_prev_day_data / job_data_check（缺失 Bark 告警）"""
import unittest
from unittest.mock import patch

from database.connection import db_manager, switch_to_test_db
from database.models import (
    Base, DailySentiment, MarketIndex, DailyEquitySnapshot, DailyKline,
    DailyZtPool, SectorStrength, SectorCycle, ConceptCycle, Recommendation,
)
import scheduler.data_check as dc

_MODELS = (DailySentiment, MarketIndex, DailyEquitySnapshot, DailyKline,
           DailyZtPool, SectorStrength, SectorCycle, ConceptCycle, Recommendation)
_PREV = "20260807"


class TestDataCheck(unittest.TestCase):

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
            for m in _MODELS:
                session.query(m).delete()
            session.commit()
        finally:
            session.close()
        dc._last_check_date = ""

    def _insert_hard(self, prev=_PREV):
        """插入 4 个硬项：情绪向量/大盘指数/盈亏快照/日线"""
        session = db_manager.get_session()
        try:
            session.add(DailySentiment(trade_date=prev))
            session.add(MarketIndex(trade_date=prev))
            session.add(DailyEquitySnapshot(trade_date=prev))
            session.add(DailyKline(code="600001", trade_date=prev))
            session.commit()
        finally:
            session.close()

    @patch("scheduler.data_check.get_previous_trading_day", return_value=_PREV)
    def test_数据齐全complete(self, _):
        self._insert_hard()
        r = dc.check_prev_day_data()
        self.assertEqual(r["trade_date"], _PREV)
        self.assertTrue(r["complete"])
        self.assertEqual(r["missing"], [])

    @patch("scheduler.data_check.get_previous_trading_day", return_value=_PREV)
    def test_日线缺失标记不完整(self, _):
        self._insert_hard()
        session = db_manager.get_session()
        try:
            session.query(DailyKline).delete()
            session.commit()
        finally:
            session.close()
        r = dc.check_prev_day_data()
        self.assertFalse(r["complete"])
        self.assertIn("日线", r["missing"])

    @patch("scheduler.data_check.get_previous_trading_day", return_value=_PREV)
    def test_软项缺失不影响complete(self, _):
        self._insert_hard()
        r = dc.check_prev_day_data()
        self.assertTrue(r["complete"])          # 硬项齐全
        self.assertGreater(len(r["soft_missing"]), 0)  # 涨停池/推荐/板块等为空

    @patch("scheduler.data_check.get_previous_trading_day", return_value=_PREV)
    def test_缺失时bark告警(self, _):
        self._insert_hard()
        session = db_manager.get_session()
        try:
            session.query(DailyKline).delete()
            session.query(MarketIndex).delete()
            session.commit()
        finally:
            session.close()
        with patch("scheduler.data_check.bark_notifier.send") as bark, \
             patch("scheduler.data_check._record_job_run"):
            dc.job_data_check(force=True)
        bark.assert_called_once()
        self.assertIn("20260807", bark.call_args[1]["body"])
        self.assertIn("日线", bark.call_args[1]["body"])
        self.assertIn("大盘指数", bark.call_args[1]["body"])

    @patch("scheduler.data_check.get_previous_trading_day", return_value=_PREV)
    def test_齐全时也推送成功(self, _):
        """不管结果如何都推送：齐全时推 ✅"""
        self._insert_hard()
        with patch("scheduler.data_check.bark_notifier.send") as bark, \
             patch("scheduler.data_check._record_job_run"):
            dc.job_data_check(force=True)
        bark.assert_called_once()
        self.assertIn("✅", bark.call_args[1]["title"])
        self.assertIn("20260807", bark.call_args[1]["title"])
        self.assertEqual(bark.call_args[1]["level"], "passive")

    @patch("scheduler.data_check.get_previous_trading_day", return_value=_PREV)
    def test_缺日线推警告(self, _):
        """缺失时推 ⚠️"""
        self._insert_hard()
        session = db_manager.get_session()
        try:
            session.query(DailyKline).delete()
            session.commit()
        finally:
            session.close()
        with patch("scheduler.data_check.bark_notifier.send") as bark, \
             patch("scheduler.data_check._record_job_run"):
            dc.job_data_check(force=True)
        bark.assert_called_once()
        self.assertIn("⚠️", bark.call_args[1]["title"])
        self.assertEqual(bark.call_args[1]["level"], "timeSensitive")


if __name__ == "__main__":
    unittest.main()
