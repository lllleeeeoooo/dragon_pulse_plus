# -*- coding: utf-8 -*-
"""概念情绪周期（切片3）单元测试：聚合、非题材过滤、阶段判定、成分股刷新"""
import datetime
import unittest
from unittest.mock import patch

import pandas as pd

from database.connection import db_manager, switch_to_test_db
from database.models import Base, ConceptMember, ConceptCycle
from database.concept_cycle import ConceptCycleManager


def _mk_zt_df(rows):
    """构造涨停池 DataFrame（code/lbc）"""
    return pd.DataFrame(rows, columns=["code", "lbc", "industry"])


class TestConceptCycle(unittest.TestCase):

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
            session.query(ConceptCycle).delete()
            session.query(ConceptMember).delete()
            session.commit()
        finally:
            session.close()

    def _seed_membership(self, mapping, refresh_date="20260804"):
        """mapping: {股票代码: [概念名]}"""
        session = db_manager.get_session()
        try:
            for code, names in mapping.items():
                for n in names:
                    session.add(ConceptMember(
                        concept_code="gn_test", concept_name=n,
                        stock_code=code, refresh_date=refresh_date))
            session.commit()
        finally:
            session.close()

    def test_聚合与题材过滤(self):
        """只聚合题材型概念，事件型(股权激励)被过滤"""
        self._seed_membership({
            "600001": ["华为概念", "股权激励"],
            "600002": ["军工航天"],
            "600003": ["股权激励"],
        })
        zt = _mk_zt_df([("600001", 1, "电子"), ("600002", 3, "军工"), ("600003", 1, "电子")])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)

        session = db_manager.get_session()
        try:
            rows = {r.concept_name: r for r in session.query(ConceptCycle).all()}
        finally:
            session.close()
        self.assertIn("华为概念", rows)
        self.assertIn("军工航天", rows)
        self.assertNotIn("股权激励", rows)  # 事件型被过滤
        self.assertEqual(rows["军工航天"].zt_count, 1)
        self.assertEqual(rows["军工航天"].max_lbc, 3)
        # 1家涨停 lbc3 → 启动（cur<3 且 不满足发酵）
        self.assertEqual(rows["军工航天"].phase, "启动")

    def test_发酵阶段判定(self):
        """3家涨停 → 发酵"""
        self._seed_membership({"600001": ["军工航天"], "600002": ["军工航天"], "600003": ["军工航天"]})
        zt = _mk_zt_df([("600001", 1, "军工"), ("600002", 2, "军工"), ("600003", 3, "军工")])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        session = db_manager.get_session()
        try:
            row = session.query(ConceptCycle).filter_by(concept_name="军工航天").first()
        finally:
            session.close()
        self.assertEqual(row.zt_count, 3)
        self.assertEqual(row.max_lbc, 3)
        self.assertEqual(row.phase, "发酵")

    def test_全部非题材则不落库(self):
        self._seed_membership({"600001": ["股权激励"]})
        zt = _mk_zt_df([("600001", 1, "电子")])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        session = db_manager.get_session()
        try:
            count = session.query(ConceptCycle).count()
        finally:
            session.close()
        self.assertEqual(count, 0)

    def test_同日幂等(self):
        self._seed_membership({"600001": ["华为概念"]})
        zt = _mk_zt_df([("600001", 1, "电子")])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        session = db_manager.get_session()
        try:
            count = session.query(ConceptCycle).filter_by(trade_date="20260804").count()
        finally:
            session.close()
        self.assertEqual(count, 1)

    def test_无概念命中则跳过(self):
        # 种入别的股票 membership（使当日刷新判定跳过、不触发真实网络），涨停股 999999 无概念 → 不落库
        self._seed_membership({"600001": ["华为概念"]})
        zt = _mk_zt_df([("999999", 1, "电子")])  # 无 membership
        session = db_manager.get_session()
        try:
            count = session.query(ConceptCycle).count()
        finally:
            session.close()
        self.assertEqual(count, 0)

    def test_get_concept_cycle_按主线分降序(self):
        self._seed_membership({"600001": ["华为概念"], "600002": ["军工航天"]})
        zt = _mk_zt_df([("600001", 1, "电子"), ("600002", 1, "军工")])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        result = ConceptCycleManager.get_concept_cycle("20260804", top=10)
        self.assertEqual(len(result), 2)
        scores = [r["mainline_score"] for r in result]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("concept", result[0])
        self.assertIn("phase", result[0])

    def test_refresh_membership_写入与幂等(self):
        boards = pd.DataFrame({"code": ["gn_hwqc", "gn_gykc"], "name": ["华为汽车", "高压快充"]})
        cons = pd.DataFrame({"code": ["600001", "600002"]})

        with patch("data.fetcher_pool._PoolMixin.get_concept_boards", return_value=boards), \
             patch("data.fetcher_pool._PoolMixin.get_concept_cons", return_value=cons):
            ok = ConceptCycleManager.refresh_membership("20260804", force=True)
        self.assertTrue(ok)
        session = db_manager.get_session()
        try:
            rows = session.query(ConceptMember).all()
        finally:
            session.close()
        self.assertEqual(len(rows), 4)  # 2概念 × 2成分股
        self.assertEqual(ConceptCycleManager.last_refresh_date(), "20260804")

        # 同日再次调用：未到期，跳过，行数不变
        with patch("data.fetcher_pool._PoolMixin.get_concept_boards", return_value=boards):
            ConceptCycleManager.refresh_membership("20260804")
        session = db_manager.get_session()
        try:
            rows2 = session.query(ConceptMember).all()
        finally:
            session.close()
        self.assertEqual(len(rows2), 4)

    def test_refresh_清理孤儿快照(self):
        """概念刷新后清除 concept_code 不在当前概念列表的旧行（历史 gn_x 占位脏数据根治）"""
        session = db_manager.get_session()
        try:
            session.add(ConceptMember(concept_code="gn_x", concept_name="参股金融",
                                      stock_code="600892", refresh_date="20260804"))
            session.commit()
        finally:
            session.close()
        boards = pd.DataFrame({"code": ["gn_cgjr"], "name": ["参股金融"]})
        cons = pd.DataFrame({"code": ["600892"]})
        with patch("data.fetcher_pool._PoolMixin.get_concept_boards", return_value=boards), \
             patch("data.fetcher_pool._PoolMixin.get_concept_cons", return_value=cons):
            ConceptCycleManager.refresh_membership("20260805", force=True)
        session = db_manager.get_session()
        try:
            rows = session.query(ConceptMember).all()
            codes = {(r.concept_code, r.stock_code) for r in rows}
        finally:
            session.close()
        self.assertIn(("gn_cgjr", "600892"), codes)
        self.assertNotIn(("gn_x", "600892"), codes)  # 孤儿脏行被清
        self.assertEqual(len(rows), 1)

    def test_membership_map去重(self):
        """_membership_map 按 (code, 概念名) 去重，跨日/同名重复行不重复"""
        session = db_manager.get_session()
        try:
            session.add(ConceptMember(concept_code="gn_x", concept_name="参股金融",
                                      stock_code="600892", refresh_date="20260804"))
            session.add(ConceptMember(concept_code="gn_cgjr", concept_name="参股金融",
                                      stock_code="600892", refresh_date="20260805"))
            session.commit()
        finally:
            session.close()
        session = db_manager.get_session()
        try:
            m = ConceptCycleManager._membership_map(session)
        finally:
            session.close()
        self.assertEqual(m["600892"], ["参股金融"])  # 只一条

    def test_get_stock_concepts去重(self):
        """get_stock_concepts 概念名去重，同名重复行只返回一次"""
        session = db_manager.get_session()
        try:
            session.add(ConceptMember(concept_code="gn_x", concept_name="参股金融",
                                      stock_code="600892", refresh_date="20260804"))
            session.add(ConceptMember(concept_code="gn_cgjr", concept_name="参股金融",
                                      stock_code="600892", refresh_date="20260805"))
            session.commit()
        finally:
            session.close()
        cons = ConceptCycleManager.get_stock_concepts("600892")
        names = [c["concept"] for c in cons]
        self.assertEqual(names.count("参股金融"), 1)


if __name__ == "__main__":
    unittest.main()
