# -*- coding: utf-8 -*-
"""竞价 verdict 闭环（逐票下判 + 前提声明 + 盘中执行权）单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from scheduler.auction import _classify_auction_verdicts, _extract_verdicts_json
from scheduler.helpers import extract_json_block
from scheduler.monitor_core import _MonitorCoreMixin
from notifier.bark import _strip_json_block


class TestVerdictsJson(unittest.TestCase):

    def test_json块解析(self):
        result = """
【推荐标的逐票判定】
- 华菱线缆(001208)：判断=买入，前提=满足，理由=达标
```json
{"verdicts": [
  {"code": "001208", "verdict": "买入", "premise": "满足", "reason": "竞价高开+3%且占比达标"},
  {"code": "002879", "verdict": "观察", "premise": "不满足", "reason": "未达要求"}
]}
```
"""
        v = _extract_verdicts_json(result)
        self.assertEqual(v["001208"]["verdict"], "买入")
        self.assertEqual(v["001208"]["premise"], "满足")
        self.assertEqual(v["002879"]["premise"], "不满足")

    def test_json解析失败返回空(self):
        self.assertEqual(_extract_verdicts_json("无结构化输出"), {})
        self.assertEqual(_extract_verdicts_json(""), {})

    def test_bark统一剔除json块(self):
        """去 JSON 是 bark 层唯一职责（调用方不再重复剥）"""
        result = "风格判断\n```json\n{\"verdicts\": []}\n```\n结束"
        stripped = _strip_json_block(result)
        self.assertNotIn("json", stripped)
        self.assertIn("风格判断", stripped)
        self.assertIn("结束", stripped)
        # 多个/中段 JSON 块也全部剔除（bark 升级后）
        multi = "A\n```json\n{\"a\": 1}\n```\nB\n```json\n{\"b\": 2}\n```\nC"
        s2 = _strip_json_block(multi)
        self.assertNotIn("```", s2)
        self.assertIn("A", s2)
        self.assertIn("C", s2)

    def test_extract_json_block_无围栏兜底(self):
        data = extract_json_block('结论如上 {"a": 1, "b": [1,2]} 完')
        self.assertEqual(data["a"], 1)


class TestRecBuyCondition(unittest.TestCase):
    """推荐标的是否满足买入条件：买入verdict跳过open_requirement正则，保留回落校验"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        # 默认 mock：open_change >= 3 视为满足开盘要求
        self.m._check_open_requirement = lambda oc, req: oc >= 3.0

    def test_买入verdict跳过open_requirement正则(self):
        # open_requirement 若被调用会抛错——买入verdict不应走到正则
        calls = []
        self.m._check_open_requirement = lambda oc, req: calls.append(oc) or False
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "买入", "open_requirement": "高开 +5%~+8%"},
            open_price=10.5, pre_close=10.0, change_pct=4.0)  # 开盘+5%但实际不在+5~8，正则应否
        self.assertTrue(ok)      # 竞价自证前提，正则被跳过
        self.assertEqual(calls, [])  # 正则确实没被调用

    def test_买入verdict回落超过阈值仍不买(self):
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "买入"},
            open_price=10.5, pre_close=10.0, change_pct=2.0)  # 开盘+5%，现+2%，回落3%>2%
        self.assertFalse(ok)

    def test_观察verdict仍走open_requirement正则(self):
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "观察", "open_requirement": "高开 +3%~+6%"},
            open_price=10.2, pre_close=10.0, change_pct=3.5)  # 开盘+2%，未达+3%
        self.assertFalse(ok)

    def test_观察verdict满足条件且无回落则买(self):
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "观察", "open_requirement": "高开 +3%~+6%"},
            open_price=10.5, pre_close=10.0, change_pct=4.0)  # 开盘+5%达标，现+4%无回落
        self.assertTrue(ok)

    def test_无verdict_无open_requirement_放行(self):
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": ""},
            open_price=10.5, pre_close=10.0, change_pct=4.0)
        self.assertTrue(ok)

    def test_竞价量能不足_非买入_否决(self):
        """断链3：观察/无verdict 推荐需过竞价量能校验（auction_vol_ratio vs auction_amount）"""
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "观察", "open_requirement": "高开 +1%~+4%",
             "auction_vol_ratio": "竞价成交额≥1900万", "auction_amount": 10_000_000},  # 1000万 < 1900万
            open_price=10.5, pre_close=10.0, change_pct=4.0)
        self.assertFalse(ok)

    def test_竞价量能达标_非买入_放行(self):
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "观察", "open_requirement": "高开 +1%~+4%",
             "auction_vol_ratio": "竞价成交额≥1900万", "auction_amount": 30_000_000},  # 3000万 ≥ 1900万
            open_price=10.5, pre_close=10.0, change_pct=4.0)
        self.assertTrue(ok)

    def test_买入verdict跳过量能校验(self):
        """信任竞价 LLM 前提自证，量能不重复卡"""
        ok = self.m._check_rec_buy_condition(
            {"auction_verdict": "买入", "auction_vol_ratio": "竞价成交额≥1900万",
             "auction_amount": 5_000_000},  # 500万 < 1900万，但买入verdict跳过
            open_price=10.5, pre_close=10.0, change_pct=4.0)
        self.assertTrue(ok)

    def test_量能解析失败或无金额则放行(self):
        self.assertTrue(self.m._check_auction_volume({"auction_vol_ratio": "竞价量足即可"}))
        self.assertTrue(self.m._check_auction_volume({"auction_vol_ratio": "竞价成交额≥1900万"}))  # 无 amount
        self.assertTrue(self.m._check_auction_volume({}))


class TestAlertedSkip(unittest.TestCase):
    """当日去重锁：非推荐标的推过一次后当日跳过；推荐标的按 B方案走 09:26 竞价一次性评估，推过后同样锁。
    注：早期"推荐推送后不被锁死、仍持续评估"的设计已被 B方案 一次性评估 取代（见 test_推荐已推送同样锁）"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._alerted_burst_codes = {"600999"}  # 已推送过一只非推荐

    def test_非推荐已推送则跳过(self):
        # 600999 已推送过且不是推荐 → 跳过
        self.assertTrue(self.m._skip_alerted_burst("600999", {"600001"}))

    def test_推荐已推送同样锁(self):
        # B方案：推荐标的只做 09:26 竞价一次性评估，推过后同样锁（盘中不再重复评估/推送）
        self.m._alerted_burst_codes.add("600001")
        self.assertTrue(self.m._skip_alerted_burst("600001", {"600001", "600999"}))

    def test_未推送不跳过(self):
        self.assertFalse(self.m._skip_alerted_burst("600002", set()))


class TestCycleFreshness(unittest.TestCase):
    """断链6：周期数据 freshness 校验"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        from core.trade_calendar import get_previous_trading_day
        import datetime
        self._expected = get_previous_trading_day(datetime.date.today())  # 动态取上一交易日

    def test_达到上一交易日为新鲜(self):
        self.assertTrue(self.m._check_cycle_fresh(self._expected, "板块"))

    def test_落后为陈旧(self):
        import datetime
        stale = (datetime.datetime.strptime(self._expected, "%Y%m%d")
                 - datetime.timedelta(days=3)).strftime("%Y%m%d")
        self.assertFalse(self.m._check_cycle_fresh(stale, "板块"))

    def test_空数据为陈旧(self):
        self.assertFalse(self.m._check_cycle_fresh("", "板块"))


class TestClassifyAuctionVerdicts(unittest.TestCase):

    def test_结构化判断与前提字段(self):
        llm = """
【推荐标的逐票判定】
- 华菱线缆(001208)：判断=买入，前提=满足，理由=竞价高开+3%且占比达标
- 长缆科技(002879)：判断=观察，前提=不满足，理由=高开+0.5%未达+1%要求
- 天娱数科(002354)：判断=放弃，理由=一字板风险高
"""
        targets = [{"code": "001208", "name": "华菱线缆"},
                   {"code": "002879", "name": "长缆科技"},
                   {"code": "002354", "name": "天娱数科"}]
        v = _classify_auction_verdicts(llm, targets)
        self.assertEqual(v["001208"], {"verdict": "买入", "premise": "满足"})
        self.assertEqual(v["002879"], {"verdict": "观察", "premise": "不满足"})
        self.assertEqual(v["002354"], {"verdict": "放弃", "premise": ""})

    def test_判断买入但前提不满足(self):
        llm = "- 高争民爆(002827)：判断=买入，前提=不满足，理由=高开+2.5%低于要求+3%"
        v = _classify_auction_verdicts(llm, [{"code": "002827", "name": "高争民爆"}])
        self.assertEqual(v["002827"], {"verdict": "买入", "premise": "不满足"})

    def test_支持冒号分隔变体(self):
        llm = "- 高争民爆(002827)：判断:放弃 前提:不满足 理由=4连板一字买不进"
        v = _classify_auction_verdicts(llm, [{"code": "002827", "name": "高争民爆"}])
        self.assertEqual(v["002827"]["verdict"], "放弃")
        self.assertEqual(v["002827"]["premise"], "不满足")

    def test_无结构化字段回退关键词(self):
        llm = "达实智能(002421)：直接挂单买入 仓位10%"
        v = _classify_auction_verdicts(llm, [{"code": "002421", "name": "达实智能"}])
        self.assertEqual(v["002421"]["verdict"], "买入")
        llm2 = "富瀚微(300613)：放弃介入"
        v2 = _classify_auction_verdicts(llm2, [{"code": "300613", "name": "富瀚微"}])
        self.assertEqual(v2["300613"]["verdict"], "放弃")

    def test_文本未提到则观察(self):
        llm = "今日无推荐标的竞价数据"
        v = _classify_auction_verdicts(llm, [{"code": "600396", "name": "华电辽能"}])
        self.assertEqual(v["600396"], {"verdict": "观察", "premise": ""})

    def test_空文本返回空(self):
        self.assertEqual(_classify_auction_verdicts("", [{"code": "000001", "name": "x"}]), {})


class TestAuctionBuyExecution(unittest.TestCase):

    def setUp(self):
        self.m = _MonitorCoreMixin()
        # _is_limit_up/_is_limit_down 定义于 _MonitorStyleMixin（生产经 MRO 组合）；
        # 单测的裸 mixin 需手动补齐，默认视为未封板
        self._p_up = patch.object(_MonitorCoreMixin, "_is_limit_up",
                                  staticmethod(lambda c, chg: False), create=True)
        self._p_down = patch.object(_MonitorCoreMixin, "_is_limit_down",
                                    staticmethod(lambda c, chg: False), create=True)
        self._p_up.start()
        self._p_down.start()
        self.addCleanup(self._p_up.stop)
        self.addCleanup(self._p_down.stop)

    def _spot(self, rows):
        df = pd.DataFrame(rows, columns=["code", "name", "price", "change_pct",
                                         "volume_ratio", "amount"])
        df["amt_billion"] = df["amount"].astype(float) / 1e8
        return df

    def test_买入前提满足并入候选置顶(self):
        spot = self._spot([
            ("600001", "信号股A", 10.0, 7.0, 3.0, 5e8),
            ("600002", "推荐股B", 20.0, 4.0, 1.2, 2e8),   # 竞价买入+前提满足
            ("600003", "普通股C", 30.0, 6.0, 2.5, 3e8),
        ])
        hit = self._spot([("600001", "信号股A", 10.0, 7.0, 3.0, 5e8),
                          ("600003", "普通股C", 30.0, 6.0, 2.5, 3e8)])
        pending = [{"code": "600002", "name": "推荐股B",
                    "auction_verdict": "买入", "auction_premise": "满足"}]
        merged = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertEqual(list(merged["code"]), ["600002", "600001", "600003"])
        self.assertFalse(bool(merged.iloc[0]["_signal_burst"]))
        self.assertIn("_signal_near_limit", merged.columns)

    def test_买入但前提不满足则不执行(self):
        spot = self._spot([("600002", "推荐股B", 20.0, 4.0, 1.2, 2e8)])
        hit = self._spot([])
        pending = [{"code": "600002", "name": "推荐股B",
                    "auction_verdict": "买入", "auction_premise": "不满足"}]
        merged = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertEqual(len(merged), 0)  # 矛盾：不执行

    def test_买入但前提未声明则不执行(self):
        spot = self._spot([("600002", "推荐股B", 20.0, 4.0, 1.2, 2e8)])
        hit = self._spot([])
        pending = [{"code": "600002", "name": "推荐股B",
                    "auction_verdict": "买入"}]  # 无 premise
        merged = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertEqual(len(merged), 0)

    def test_无买入verdict不改变候选(self):
        spot = self._spot([("600001", "A", 10.0, 7.0, 3.0, 5e8)])
        hit = self._spot([("600001", "A", 10.0, 7.0, 3.0, 5e8)])
        pending = [{"code": "600001", "name": "A", "auction_verdict": "观察"}]
        merged = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertEqual(list(merged["code"]), ["600001"])

    def test_买入标的不在快照则跳过(self):
        spot = self._spot([("600001", "A", 10.0, 7.0, 3.0, 5e8)])
        hit = self._spot([("600001", "A", 10.0, 7.0, 3.0, 5e8)])
        pending = [{"code": "999999", "name": "不在快照",
                    "auction_verdict": "买入", "auction_premise": "满足"}]
        merged = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertEqual(list(merged["code"]), ["600001"])

    def test_候选去重(self):
        spot = self._spot([("600001", "A", 10.0, 7.0, 3.0, 5e8)])
        hit = self._spot([("600001", "A", 10.0, 7.0, 3.0, 5e8)])
        pending = [{"code": "600001", "name": "A",
                    "auction_verdict": "买入", "auction_premise": "满足"}]
        merged = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertEqual(len(merged), 1)
        self.assertEqual(list(merged["code"]), ["600001"])

    def test_一次性评估_二次调用不再进池(self):
        """推荐标的 09:26 竞价后只评估一次，盘中第二次调用不再进池"""
        spot = self._spot([("600002", "推荐股", 20.0, 4.0, 1.2, 2e8),
                           ("600001", "信号股", 10.0, 7.0, 3.0, 5e8)])
        hit = self._spot([("600001", "信号股", 10.0, 7.0, 3.0, 5e8)])
        pending = [{"code": "600002", "name": "推荐股",
                    "auction_verdict": "买入", "auction_premise": "满足"}]
        m1 = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertIn("600002", set(m1["code"]))
        m2 = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertNotIn("600002", set(m2["code"]))
        self.assertEqual(list(m2["code"]), ["600001"])

    def test_封板首轮不标记已评估_破板后重新进池(self):
        """审查#3：开盘封死(涨停)的买入候选暂不标记"已评估"，破板后须能重新进池"""
        spot = self._spot([("600002", "推荐股", 20.0, 10.0, 1.2, 2e8),
                           ("600001", "信号股", 10.0, 7.0, 3.0, 5e8)])
        hit = self._spot([("600001", "信号股", 10.0, 7.0, 3.0, 5e8)])
        pending = [{"code": "600002", "name": "推荐股",
                    "auction_verdict": "买入", "auction_premise": "满足"}]

        with patch.object(_MonitorCoreMixin, "_is_limit_up",
                          staticmethod(lambda c, chg: c == "600002")):
            m1 = self.m._merge_auction_buy_candidates(spot, hit, pending)
            self.assertIn("600002", set(m1["code"]))
            # 600002 封板中：不应标记已评估 → 第二次调用仍进池
            m2 = self.m._merge_auction_buy_candidates(spot, hit, pending)
            self.assertIn("600002", set(m2["code"]))

        # 破板后（不再封板）：首次进池即标记已评估，之后不再重复进池
        m3 = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertIn("600002", set(m3["code"]))
        m4 = self.m._merge_auction_buy_candidates(spot, hit, pending)
        self.assertNotIn("600002", set(m4["code"]))


if __name__ == "__main__":
    unittest.main()
