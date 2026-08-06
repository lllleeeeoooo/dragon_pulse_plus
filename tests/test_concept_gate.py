# -*- coding: utf-8 -*-
"""盘中概念因子买入闸门（切片3）单元测试"""
import datetime
import unittest

from scheduler.monitor_core import _MonitorCoreMixin


class TestConceptGate(unittest.TestCase):

    def setUp(self):
        self.m = _MonitorCoreMixin()
        # 注入当日缓存（绕过 DB/网络），日期置今日使 _ensure_concept_cycle_cache 直接返回
        self.m._concept_cache_date = datetime.datetime.now().strftime("%Y%m%d")
        self.m._concept_cycle_info = {}
        self.m._concept_member_map = {}

    def _seed(self, cycle=None, member=None):
        self.m._concept_cycle_info = cycle or {}
        self.m._concept_member_map = member or {}

    def test_无概念数据不否决(self):
        self._seed(member={})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_发酵概念放行(self):
        self._seed(cycle={"华为概念": {"phase": "发酵", "is_mainline": True}},
                   member={"600001": ["华为概念"]})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_启动概念放行(self):
        self._seed(cycle={"机器人概念": {"phase": "启动", "is_mainline": False}},
                   member={"600001": ["机器人概念"]})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_高潮主线放行(self):
        self._seed(cycle={"光伏概念": {"phase": "高潮", "is_mainline": True}},
                   member={"600001": ["光伏概念"]})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_高潮非主线否决(self):
        self._seed(cycle={"光伏概念": {"phase": "高潮", "is_mainline": False}},
                   member={"600001": ["光伏概念"]})
        self.assertTrue(self.m._get_concept_blocks_buy("600001"))

    def test_全部退潮否决(self):
        self._seed(cycle={"旧题材": {"phase": "退潮", "is_mainline": False}},
                   member={"600001": ["旧题材"]})
        self.assertTrue(self.m._get_concept_blocks_buy("600001"))

    def test_全部冰点否决(self):
        self._seed(cycle={"冷门": {"phase": "冰点", "is_mainline": False}},
                   member={"600001": ["冷门"]})
        self.assertTrue(self.m._get_concept_blocks_buy("600001"))

    def test_混合_存在一个可买概念即放行(self):
        self._seed(cycle={
                "华为概念": {"phase": "发酵", "is_mainline": True},
                "旧题材": {"phase": "退潮", "is_mainline": False},
            }, member={"600001": ["华为概念", "旧题材"]})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_混合_全部高潮非主线或退潮则否决(self):
        self._seed(cycle={
                "A": {"phase": "高潮", "is_mainline": False},
                "B": {"phase": "退潮", "is_mainline": False},
            }, member={"600001": ["A", "B"]})
        self.assertTrue(self.m._get_concept_blocks_buy("600001"))

    def test_概念无周期记录放行(self):
        # 概念无周期记录（phase 未知）→ 与无概念数据一致，未知即放行，
        # 避免误杀数据源未覆盖题材的股票（如仅"参股金融"无记录的冷门标签）
        self._seed(member={"600001": ["参股金融"]})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_混合_退潮与无记录概念放行(self):
        # 一个明确退潮 + 一个无周期记录 → 存在非明确负向 → 放行（不再"全部负向"）
        self._seed(cycle={"旧题材": {"phase": "退潮", "is_mainline": False}},
                   member={"600001": ["旧题材", "参股金融"]})
        self.assertFalse(self.m._get_concept_blocks_buy("600001"))

    def test_标签取最优概念(self):
        self._seed(cycle={
                "华为概念": {"phase": "发酵", "is_mainline": True},
                "旧题材": {"phase": "退潮", "is_mainline": False},
            }, member={"600001": ["华为概念", "旧题材"]})
        self.assertEqual(self.m._get_stock_concept_tag("600001"), "华为概念·发酵·主线")

    def test_标签无概念为空(self):
        self._seed(member={})
        self.assertEqual(self.m._get_stock_concept_tag("600001"), "")


if __name__ == "__main__":
    unittest.main()
