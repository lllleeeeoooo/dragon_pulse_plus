# -*- coding: utf-8 -*-
"""推荐评估粒度 + 跳评窗口（断链8）单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from database.connection import db_manager, switch_to_test_db
from database.models import Base, Recommendation
from database.recommendations import RecommendationManager
import scheduler.helpers as helpers


class TestRecommendationEval(unittest.TestCase):

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
            session.query(Recommendation).delete()
            session.commit()
        finally:
            session.close()

    def _seed_recs(self, date="20260803"):
        RecommendationManager.add_recommendations(date, [
            {"code": "600001", "name": "A", "strategy_type": "打板"},
            {"code": "600002", "name": "B", "strategy_type": "共振"},
        ])

    def _spot(self):
        return pd.DataFrame([
            {"code": "600001", "name": "A", "price": 11.0, "change_pct": 8.0},
            {"code": "600002", "name": "B", "price": 20.0, "change_pct": -2.0},
        ])

    def test_逐标的评估写入各自eval_note与score(self):
        """粒度修复：每只推荐拿到自己的评分与点评，而非同一整段文本"""
        self._seed_recs()
        fake = ('```json\n{"evaluations": ['
                '{"code": "600001", "score": 90, "comment": "打板成功"}, '
                '{"code": "600002", "score": 30, "comment": "共振失败"}]}\n```')
        with patch("scheduler.helpers.llm_client.generate", return_value=fake):
            helpers._evaluate_yesterday_recommendations("20260804", self._spot())
        session = db_manager.get_session()
        try:
            rows = {r.code: r for r in session.query(Recommendation).all()}
        finally:
            session.close()
        self.assertEqual(rows["600001"].eval_note, "打板成功")
        self.assertEqual(rows["600001"].eval_score, 90)
        self.assertEqual(rows["600002"].eval_note, "共振失败")
        self.assertEqual(rows["600002"].eval_score, 30)
        self.assertNotEqual(rows["600001"].eval_note, rows["600002"].eval_note)

    def test_spot为空用涨停池兜底不跳过(self):
        """spot 为空不再整环跳过——用 zt_df 兜底仍产出评估（防跳评）"""
        self._seed_recs()
        fake = '{"evaluations": [{"code": "600001", "score": 80, "comment": "涨停达标"}]}'
        zt = pd.DataFrame([{"code": "600001", "name": "A", "lbc": 1}])
        with patch("scheduler.helpers.llm_client.generate", return_value=fake):
            helpers._evaluate_yesterday_recommendations("20260804", spot_df=None, zt_df=zt)
        session = db_manager.get_session()
        try:
            rows = {r.code: r for r in session.query(Recommendation).all()}
        finally:
            session.close()
        self.assertEqual(rows["600001"].eval_score, 80)  # 评估未跳过
        self.assertTrue(rows["600002"].eval_note)        # 未命中 JSON → 兜底整段

    def test_无推荐则不调LLM(self):
        with patch("scheduler.helpers.llm_client.generate", return_value="x") as gen:
            helpers._evaluate_yesterday_recommendations("20260804", self._spot())
        gen.assert_not_called()

    def test_LLM失败写评估失败兜底(self):
        self._seed_recs()
        with patch("scheduler.helpers.llm_client.generate",
                   side_effect=Exception("llm down")):
            helpers._evaluate_yesterday_recommendations("20260804", self._spot())
        session = db_manager.get_session()
        try:
            rows = session.query(Recommendation).all()
        finally:
            session.close()
        self.assertTrue(all(r.eval_note for r in rows))  # 兜底"评估失败"已写入


class TestExpireUnevaluated(unittest.TestCase):

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
            session.query(Recommendation).delete()
            session.commit()
        finally:
            session.close()

    def test_未评估过期时标记可见(self):
        """跳评窗口修复：未评估的过期推荐标记"已过期未评估"，而非静默消失"""
        RecommendationManager.add_recommendations("20260801", [
            {"code": "600001", "name": "未评估", "strategy_type": "打板"},  # eval_note 空
        ])
        RecommendationManager.add_recommendations("20260802", [
            {"code": "600002", "name": "已评估", "strategy_type": "共振"},
        ])
        session = db_manager.get_session()
        try:
            rec2 = session.query(Recommendation).filter_by(code="600002").first()
            rec2.eval_note = "昨日已评估"
            session.commit()
        finally:
            session.close()

        RecommendationManager.expire_old_recommendations("20260803")
        session = db_manager.get_session()
        try:
            r1 = session.query(Recommendation).filter_by(code="600001").first()
            r2 = session.query(Recommendation).filter_by(code="600002").first()
        finally:
            session.close()
        self.assertEqual(r1.status, "EXPIRED")
        self.assertEqual(r1.eval_note, "已过期未评估(盘后评估未执行)")
        self.assertEqual(r2.status, "EXPIRED")
        self.assertEqual(r2.eval_note, "昨日已评估")  # 已评估的不覆盖


class TestVerdictDateLimit(unittest.TestCase):
    """断链7：update_auction_verdicts 限定 trade_date，避免写错旧记录"""

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
            session.query(Recommendation).delete()
            session.commit()
        finally:
            session.close()

    def test_同一code多日PENDING_verdict只写指定日期(self):
        # 两日都有 PENDING 的同 code（监控多日未跑导致累积）
        RecommendationManager.add_recommendations("20260801", [
            {"code": "600001", "name": "旧日", "strategy_type": "打板"},
        ])
        RecommendationManager.add_recommendations("20260803", [
            {"code": "600001", "name": "昨日", "strategy_type": "打板"},
        ])
        RecommendationManager.update_auction_verdicts(
            {"600001": {"verdict": "买入", "premise": "满足"}}, trade_date="20260803")
        session = db_manager.get_session()
        try:
            old = session.query(Recommendation).filter_by(trade_date="20260801").first()
            new = session.query(Recommendation).filter_by(trade_date="20260803").first()
        finally:
            session.close()
        self.assertEqual(old.auction_verdict, None)  # 旧记录不被写
        self.assertEqual(new.auction_verdict, "买入")
        self.assertEqual(new.auction_premise, "满足")

    def test_save_auction_amounts限定日期(self):
        RecommendationManager.add_recommendations("20260801", [
            {"code": "600001", "name": "旧日", "strategy_type": "打板"},
        ])
        RecommendationManager.add_recommendations("20260803", [
            {"code": "600001", "name": "昨日", "strategy_type": "打板"},
        ])
        RecommendationManager.save_auction_amounts({"600001": 30_000_000}, trade_date="20260803")
        session = db_manager.get_session()
        try:
            old = session.query(Recommendation).filter_by(trade_date="20260801").first()
            new = session.query(Recommendation).filter_by(trade_date="20260803").first()
        finally:
            session.close()
        self.assertIsNone(old.auction_amount)
        self.assertEqual(new.auction_amount, 30_000_000)


if __name__ == "__main__":
    unittest.main()
