# -*- coding: utf-8 -*-
"""盘前简报持久化（断链4修复）单元测试"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
