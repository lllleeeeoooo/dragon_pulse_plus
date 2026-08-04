# -*- coding: utf-8 -*-
"""盘后复盘 LLM 引用概念主线（切片3）单元测试"""
import re
import unittest
from unittest.mock import patch

import pandas as pd

from llm.post_market import PostMarketAnalyzer
from database.concept_cycle import ConceptCycleManager


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a):
        return _FakeQuery(self._rows)

    def close(self):
        pass


class TestDetectLeadingConcepts(unittest.TestCase):

    def test_概念主线输出含代表股与阶段(self):
        cycle = [
            {"concept": "华为概念", "phase": "发酵", "zt_count": 7, "max_lbc": 2,
             "is_mainline": True, "mainline_score": 0.60},
            {"concept": "旧题材", "phase": "退潮", "zt_count": 1, "max_lbc": 1,
             "is_mainline": False, "mainline_score": 0.10},
        ]
        member = [("600001", "华为概念"), ("600002", "华为概念"), ("600003", "旧题材")]
        zt = pd.DataFrame([{"code": "600001", "name": "通宇", "lbc": 2},
                           {"code": "600002", "name": "鹏鼎", "lbc": 1}])
        with patch.object(ConceptCycleManager, "get_concept_cycle", return_value=cycle), \
             patch("database.connection.db_manager.get_session",
                   return_value=_FakeSession(member)):
            txt = PostMarketAnalyzer._detect_leading_concepts(zt)
        self.assertIn("华为概念", txt)
        self.assertIn("★", txt)                 # 主线概念带星
        self.assertIn("发酵", txt)              # 阶段
        self.assertIn("0.60", txt)             # 主线分
        self.assertIn("通宇(600001)", txt)     # 代表股
        self.assertIn("旧题材", txt)           # 非主线也展示
        self.assertNotIn("★：退潮", txt)       # 非主线无星

    def test_无概念周期返回空(self):
        with patch.object(ConceptCycleManager, "get_concept_cycle", return_value=[]):
            self.assertEqual(PostMarketAnalyzer._detect_leading_concepts(None), "")

    def test_模板占位符与format参数完整匹配(self):
        """POST_MARKET_USER_TEMPLATE 的所有 {占位符} 都能被 run_review 的 format 提供"""
        from config.prompt_templates import POST_MARKET_USER_TEMPLATE
        import llm.post_market as pm
        # 提取模板占位符
        placeholders = set(re.findall(r"\{(\w+)\}", POST_MARKET_USER_TEMPLATE))
        # 提取 format 调用里的关键字（静态扫描 run_review 源码）
        src = open(pm.__file__, encoding="utf-8").read()
        fmt_block = src[src.index("POST_MARKET_USER_TEMPLATE.format"):src.index(")", src.index("POST_MARKET_USER_TEMPLATE.format"))]
        fmt_keys = set(re.findall(r"(\w+)\s*=", fmt_block))
        missing = placeholders - fmt_keys
        self.assertEqual(missing, set(), f"模板占位符未被 format 提供: {missing}")


if __name__ == "__main__":
    unittest.main()
