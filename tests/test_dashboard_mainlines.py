# -*- coding: utf-8 -*-
"""双维度主线对照（切片3）单元测试"""
import unittest
from unittest.mock import patch

from dashboard.data import _build_mainlines_section


class TestMainlinesSection(unittest.TestCase):

    def test_双维度并排归一化(self):
        concept_cycle = [{"concept": "华为概念", "trade_date": "20260804", "phase": "发酵",
                          "zt_count": 7, "max_lbc": 2, "is_mainline": True,
                          "mainline_score": 0.60}]
        sector_cycle = [{"sector": "元件", "trade_date": "20260804", "phase": "发酵",
                         "zt_count": 15, "max_lbc": 2, "is_mainline": True,
                         "mainline_score": 0.55}]
        with patch("database.ConceptCycleManager.get_concept_cycle", return_value=concept_cycle), \
             patch("database.SectorCycleManager.get_sector_cycle", return_value=sector_cycle):
            res = _build_mainlines_section()
        self.assertEqual(res["date"], "20260804")
        self.assertEqual(res["concepts"][0]["name"], "华为概念")
        self.assertEqual(res["industries"][0]["name"], "元件")
        self.assertEqual(res["concepts"][0]["mainline"], True)
        self.assertEqual(res["industries"][0]["zt"], 15)
        self.assertAlmostEqual(res["concepts"][0]["score"], 0.60)

    def test_无数据返回空结构(self):
        with patch("database.ConceptCycleManager.get_concept_cycle", return_value=[]), \
             patch("database.SectorCycleManager.get_sector_cycle", return_value=[]):
            res = _build_mainlines_section()
        self.assertEqual(res["date"], "")
        self.assertEqual(res["concepts"], [])
        self.assertEqual(res["industries"], [])

    def test_概念空但行业有数据(self):
        sector_cycle = [{"sector": "元件", "trade_date": "20260804", "phase": "发酵",
                         "zt_count": 15, "max_lbc": 2, "is_mainline": True,
                         "mainline_score": 0.55}]
        with patch("database.ConceptCycleManager.get_concept_cycle", return_value=[]), \
             patch("database.SectorCycleManager.get_sector_cycle", return_value=sector_cycle):
            res = _build_mainlines_section()
        self.assertEqual(res["date"], "20260804")
        self.assertEqual(res["industries"][0]["name"], "元件")
        self.assertEqual(res["concepts"], [])


if __name__ == "__main__":
    unittest.main()
