# -*- coding: utf-8 -*-
"""同花顺概念指数趋势：落库/查询/指标计算/拉取(mock)/概念周期集成"""
import datetime
import unittest
from unittest.mock import patch

import pandas as pd

from database.connection import db_manager, switch_to_test_db
from database.models import Base, ConceptMember, ConceptCycle, ThsConceptTrend
from database.concept_cycle import ConceptCycleManager
from database.ths_concept import ThsConceptTrendManager, _normalize_name


def _mk_hist(closes, vols=None, cols=None):
    """构造同花顺概念指数历史 DataFrame（列：日期/今开价/最高价/最低价/昨收价/成交量/成交额）"""
    n = len(closes)
    vols = vols or [1000] * n
    dates = [f"2026-07-{(i % 28) + 1:02d}" for i in range(n)]
    df = pd.DataFrame({
        "日期": dates,
        "今开价": closes,
        "最高价": [c * 1.01 for c in closes],
        "最低价": [c * 0.99 for c in closes],
        "昨收价": closes,  # 同花顺该列实为当日收盘
        "成交量": vols,
        "成交额": [v * c for v, c in zip(vols, closes)],
    })
    return df


class TestThsConceptManager(unittest.TestCase):
    """落库/查询/规范化"""

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
            session.query(ThsConceptTrend).delete()
            session.commit()
        finally:
            session.close()

    def test_normalize_name去后缀空格(self):
        self.assertEqual(_normalize_name("华为概念"), "华为")
        self.assertEqual(_normalize_name("AI 应用产业链"), "AI应用")
        self.assertEqual(_normalize_name("军工"), "军工")

    def test_save与trend_map与strong(self):
        ThsConceptTrendManager.save_trends("20260804", [
            {"concept_code": "309001", "concept_name": "AI概念", "close": 100.0,
             "chg_pct_1d": 2.0, "chg_pct_5d": 8.5, "volume_ratio_5d": 1.5},
            {"concept_code": "309002", "concept_name": "冷门", "close": 50.0,
             "chg_pct_1d": -1.0, "chg_pct_5d": 1.0, "volume_ratio_5d": 0.8},
        ])
        m = ThsConceptTrendManager.get_trend_map("20260804")
        self.assertEqual(m["AI概念"]["chg_5d"], 8.5)
        # 规范化键
        mn = ThsConceptTrendManager.get_trend_map_normalized("20260804")
        self.assertIn("AI", mn)  # AI概念 → AI
        # 强势榜只取 ≥5%
        strong = ThsConceptTrendManager.get_strong_concepts(top_n=5, min_chg_5d=5.0)
        self.assertEqual([s["concept"] for s in strong], ["AI概念"])
        # upsert 覆盖
        ThsConceptTrendManager.save_trends("20260804", [
            {"concept_code": "309001", "concept_name": "AI概念", "close": 105.0,
             "chg_pct_1d": 3.0, "chg_pct_5d": 9.0, "volume_ratio_5d": 1.6}])
        self.assertEqual(ThsConceptTrendManager.get_trend_map("20260804")["AI概念"]["chg_5d"], 9.0)


class TestThsFetch(unittest.TestCase):
    """指标计算 + 拉取(mock)"""

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
            session.query(ThsConceptTrend).delete()
            session.commit()
        finally:
            session.close()

    def test_compute_trends指标(self):
        from data.ths_concept import _compute_trends
        closes = [90, 91, 93, 92, 95, 100]  # 6个值：closes[-6]=90 为5个交易日前
        hist = _mk_hist(closes)
        t = _compute_trends("309", "测试", hist)
        self.assertAlmostEqual(t["chg_pct_1d"], (100 - 95) / 95 * 100, places=1)
        self.assertAlmostEqual(t["chg_pct_5d"], (100 - 90) / 90 * 100, places=1)
        self.assertEqual(t["close"], 100.0)

    def test_compute_trends数据不足返回None(self):
        from data.ths_concept import _compute_trends
        self.assertIsNone(_compute_trends("309", "测试", _mk_hist([90, 91])))

    def test_fetch_mock拉取并落库(self):
        from data.ths_concept import fetch_ths_concept_trends
        boards = pd.DataFrame({"name": ["AI概念", "华为概念"], "code": ["309001", "309002"]})
        with patch("akshare.stock_board_concept_name_ths", return_value=boards), \
             patch("akshare.stock_board_concept_index_ths",
                   side_effect=lambda symbol: _mk_hist([90, 91, 92, 93, 94, 100])), \
             patch("data.ths_concept.time.sleep"):
            r = fetch_ths_concept_trends("20260804", max_concepts=2)
        self.assertEqual(r["success"], 2)
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["saved"], 2)
        m = ThsConceptTrendManager.get_trend_map("20260804")
        self.assertIn("AI概念", m)
        self.assertAlmostEqual(m["华为概念"]["chg_5d"], (100 - 90) / 90 * 100, places=1)


class TestConceptCycleIntegration(unittest.TestCase):
    """sync_from_zt_pool 集成：ths_chg_5d 写入 / 主线分加分 / 阶段修正 / 未匹配跳过"""

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
            session.query(ThsConceptTrend).delete()
            session.commit()
        finally:
            session.close()

    def _seed_membership(self, mapping, refresh_date="20260804"):
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

    def _seed_ths(self, rows):
        ThsConceptTrendManager.save_trends("20260804", rows)

    def test_匹配概念写入ths并加分升阶段(self):
        self._seed_membership({"600001": ["华为概念"]})
        # 华为概念 5日涨幅 8% ≥ 5% → 强
        self._seed_ths([{"concept_code": "309001", "concept_name": "华为概念",
                         "close": 100, "chg_pct_1d": 2.0, "chg_pct_5d": 8.0,
                         "volume_ratio_5d": 1.5}])
        zt = pd.DataFrame([{"code": "600001", "lbc": 1, "industry": "电子"}])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        recs = ConceptCycleManager.get_concept_cycle("20260804", top=10)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["concept"], "华为概念")
        self.assertEqual(r["ths_chg_5d"], 8.0)   # 已写入
        self.assertEqual(r["phase"], "发酵")      # 1涨停基础=启动 + 指数强 → 发酵
        # 基础分 mainline_score(1,1,1,1)=0.20 + THS加分 0.1×min(8/5,1)=0.1 → 0.30
        self.assertAlmostEqual(r["mainline_score"], 0.30, places=2)

    def test_未匹配概念ths为空不改分(self):
        self._seed_membership({"600002": ["光伏概念"]})
        # 无对应 THS 趋势
        zt = pd.DataFrame([{"code": "600002", "lbc": 2, "industry": "电力"}])
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        r = ConceptCycleManager.get_concept_cycle("20260804", top=10)[0]
        self.assertIsNone(r["ths_chg_5d"])
        # 1涨停基础 phase=启动，分 mainline_score(1,1,1,2)=0.24，无 THS 加分
        self.assertEqual(r["mainline_score"], 0.24)
        self.assertEqual(r["phase"], "启动")

    def test_指数弱降档(self):
        self._seed_membership({"600003": ["机器人"]})
        self._seed_ths([{"concept_code": "309003", "concept_name": "机器人",
                         "close": 100, "chg_pct_1d": -2.0, "chg_pct_5d": -6.0,
                         "volume_ratio_5d": 0.7}])
        # 4只涨停 → 基础 phase=发酵(cur_zt≥3)
        zt = pd.DataFrame([{"code": "600003", "lbc": 1, "industry": "机械"}] * 4)
        ConceptCycleManager.sync_from_zt_pool("20260804", zt)
        r = ConceptCycleManager.get_concept_cycle("20260804", top=10)[0]
        self.assertEqual(r["ths_chg_5d"], -6.0)
        self.assertEqual(r["phase"], "退潮")  # 发酵 + 指数≤-5% → 退潮


if __name__ == "__main__":
    unittest.main()
