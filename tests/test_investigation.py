# -*- coding: utf-8 -*-
"""立案调查同步加固（列名解析 + None 防御）单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from database.connection import db_manager, switch_to_test_db
from database.models import Base, InvestigationRecord
from database.investigation import InvestigationManager


class TestInvestigationSync(unittest.TestCase):

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
            session.query(InvestigationRecord).delete()
            session.commit()
        finally:
            session.close()

    def _mock_ak(self, df):
        return patch("akshare.stock_gsrl_gsdt_em", return_value=df)

    def test_列序变化仍能解析并过滤非风险(self):
        """加固核心：列名解析免疫列序变化；非风险事件(对外担保)被过滤"""
        df = pd.DataFrame({
            "交易日": ["2026-08-05", "2026-08-05"],
            "事件类型": ["立案调查", "对外担保"],
            "简称": ["风险股A", "担保股B"],
            "代码": ["600001", "600002"],
            "具体事项": ["涉嫌信息披露违规被立案", "对外担保"],
            "序号": [1, 2],
        })
        with self._mock_ak(df):
            new = InvestigationManager.sync_from_gsrl("20260805")
        self.assertEqual(len(new), 1)  # 只留立案调查
        session = db_manager.get_session()
        try:
            rows = session.query(InvestigationRecord).all()
        finally:
            session.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "600001")
        self.assertEqual(rows[0].event_type, "立案调查")
        self.assertEqual(rows[0].announce_date, "20260805")

    def test_字段为None不崩溃(self):
        """None 防御：具体事项为 None 也能落库不抛错"""
        df = pd.DataFrame({
            "代码": ["600001"],
            "简称": ["风险股A"],
            "事件类型": ["监管警示"],
            "具体事项": [None],   # 内容缺失
            "交易日": ["2026-08-05"],
        })
        with self._mock_ak(df):
            new = InvestigationManager.sync_from_gsrl("20260805")
        self.assertEqual(len(new), 1)  # 不崩溃、正常落库

    def test_全部非风险则空(self):
        df = pd.DataFrame({
            "代码": ["600001"],
            "简称": ["担保股"],
            "事件类型": ["对外担保"],
            "具体事项": ["对外担保"],
            "交易日": ["2026-08-05"],
        })
        with self._mock_ak(df):
            new = InvestigationManager.sync_from_gsrl("20260805")
        self.assertEqual(new, [])

    def test_空数据返回空(self):
        with self._mock_ak(pd.DataFrame()):
            self.assertEqual(InvestigationManager.sync_from_gsrl("20260805"), [])


if __name__ == "__main__":
    unittest.main()
