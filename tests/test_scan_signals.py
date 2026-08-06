# -*- coding: utf-8 -*-
"""_scan_signals 集成回归测试：审查#3(封板不锁死破板重评)/#4(买前复核fail-closed不锁)/
#1(买入LLM预算用尽不跳过推送) 的控制流验证。"""
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scheduler.monitor_core import _MonitorCoreMixin


class ScanSignalsHarness(unittest.TestCase):
    """构造 _scan_signals 所需 mock：闸门/板块/概念全放行，去重状态真实。"""

    def setUp(self):
        self.m = _MonitorCoreMixin()
        self.m._DF = Mock()
        self.m._DF.get_market_total_amount.return_value = 0  # 触发 fallback 分支

        # 买入前置：全放行
        self.m._buy_gates_open = Mock(return_value=True)
        self.m._get_stock_industry = Mock(return_value="")
        self.m._get_sector_phase = Mock(return_value="")
        self.m._get_sector_is_mainline = Mock(return_value=False)
        self.m._get_concept_blocks_buy = Mock(return_value=False)
        self.m._get_stock_concept_tag = Mock(return_value="")
        self.m._check_rec_buy_condition = Mock(return_value=True)
        self.m._check_fund_inflow_alert = Mock()
        # 默认：LLM 判买入、复核通过；各用例可按需覆盖
        self.m._llm_confirm_buy = Mock(return_value=("llm", True))
        self.m._recheck_buy_after_llm = Mock(return_value=(0.0, True, False))

        # 盘中被触达的 DB/推送/标签：mock 掉
        self.bark = patch("scheduler.monitor_core.bark_notifier.send").start()
        self.addCleanup(self.bark.stop)
        self._patches = [
            patch("scheduler.monitor_core.HoldingManager.get_active_holdings",
                  return_value=[]),
            patch("scheduler.monitor_core.HoldingManager.add_holding"),
            patch("scheduler.monitor_core.RecommendationManager.mark_triggered"),
            patch("scheduler.monitor_core.StrategyAnalyzer.identify_tags",
                  return_value=["测试"]),
            patch("scheduler.monitor_core._MonitorCoreMixin._submit_llm_alert"),
            patch.object(_MonitorCoreMixin, "_is_limit_up",
                         staticmethod(lambda c, chg: False), create=True),
            patch.object(_MonitorCoreMixin, "_is_limit_down",
                         staticmethod(lambda c, chg: False), create=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _row(self, code, name, chg, vol_ratio, amount=5e8, pre_close=10.0,
             open_p=None, price=None):
        """构造快照行：默认平开，冲高回落信号不触发；其余列齐备避免 KeyError。"""
        open_p = pre_close if open_p is None else open_p
        price = price if price is not None else round(pre_close * (1 + chg / 100), 2)
        return {
            "code": code, "name": name, "price": price, "change_pct": chg,
            "volume_ratio": vol_ratio, "amount": amount,
            "open": open_p, "pre_close": pre_close,
            "high": max(price, open_p), "low": min(price, open_p),
            "amplitude": 8.0, "turnover_rate": 5.0,
        }

    def _scan(self, rows, pending, index_breaker=False):
        spot = pd.DataFrame(rows)
        market_style = {"style": "共振", "priority_strategy": ""}
        pending_codes = {r["code"] for r in pending}
        self.m._scan_signals(spot, market_style, pending, pending_codes, index_breaker)


class TestSealedAuctionBuyReentry(ScanSignalsHarness):
    """审查#3：竞价买入候选开盘封板 → 推送『封板待重评』但不下 _alerted_burst_codes；
    破板后下轮重新评估买入（修复第一轮推送即锁死的集成失效）。"""

    def test_封板不锁_破板后重新评估(self):
        pending = [{"id": 1, "code": "600002", "name": "推荐股B",
                    "auction_verdict": "买入", "auction_premise": "满足"}]
        # 第一轮：600002 封板(+10%)
        with patch.object(_MonitorCoreMixin, "_is_limit_up",
                          staticmethod(lambda c, chg: c == "600002")):
            self._scan([self._row("600002", "推荐股B", 10.0, 6.0, amount=2e8)], pending)
        # 封板 → 不锁买入评估锁，只记『待重评』去重；不触发 LLM
        self.assertNotIn("600002", self.m._alerted_burst_codes)
        self.assertIn("600002", self.m._deferred_alerted_codes)
        self.assertEqual(self.m._llm_confirm_buy.call_count, 0)

        # 第二轮：破板(+9%) → 重新评估买入（不再被 _skip_alerted_burst 跳过）
        self._scan([self._row("600002", "推荐股B", 9.0, 6.0, amount=2e8)], pending)
        self.assertGreaterEqual(self.m._llm_confirm_buy.call_count, 1)

    def test_未封板推荐候选首轮正常评估并锁(self):
        """对照：非封板竞价买入候选首轮即评估买入并落锁，不受本修复影响。"""
        pending = [{"id": 1, "code": "600002", "name": "推荐股B",
                    "auction_verdict": "买入", "auction_premise": "满足"}]
        self._scan([self._row("600002", "推荐股B", 4.0, 1.2, amount=2e8)], pending)
        self.assertIn("600002", self.m._alerted_burst_codes)


class TestRecheckFailNoLock(ScanSignalsHarness):
    """审查#4：买前复核 fail-closed(快照瞬时故障)后候选不锁入 _alerted_burst_codes；
    快照恢复后下轮重新评估买入。"""

    def test_快照失败不锁_恢复后重新评估(self):
        row = self._row("600001", "信号股A", 9.0, 6.0)
        # 第一轮：LLM 判买入但复核快照不可用 → fail-closed，推送『复核待重评』但不锁
        self.m._recheck_buy_after_llm = Mock(return_value=(9.0, False, True))
        self._scan([row], [])
        self.assertNotIn("600001", self.m._alerted_burst_codes)
        self.assertIn("600001", self.m._deferred_alerted_codes)
        calls_after_fail = self.m._llm_confirm_buy.call_count

        # 第二轮：快照恢复、复核通过 → 重新评估并买入
        self.m._recheck_buy_after_llm = Mock(return_value=(9.0, True, False))
        self._scan([row], [])
        self.assertGreater(self.m._llm_confirm_buy.call_count, calls_after_fail)
        self.assertIn("600001", self.m._alerted_burst_codes)

    def test_回落复核失败_final不重评(self):
        """对照：数据可靠判回落(retry=False) → 视为本轮已评估，正常推送并落锁。"""
        row = self._row("600001", "信号股A", 9.0, 6.0)
        self.m._recheck_buy_after_llm = Mock(return_value=(9.0, False, False))
        self._scan([row], [])
        self.assertIn("600001", self.m._alerted_burst_codes)
        self.assertNotIn("600001", self.m._deferred_alerted_codes)
        self.assertEqual(self.m._llm_confirm_buy.call_count, 1)


class TestBudgetExhaustedDeferredPush(ScanSignalsHarness):
    """审查#1/#3：LLM 确认预算用尽的低排名候选 → 推送『预算待重评』但不下锁，
    下轮预算空出后仍重新评估。"""

    def test_预算用尽候选推送待重评且下轮可重评(self):
        rows = [self._row("600001", "信号A", 9.0, 6.0),
                self._row("600002", "信号B", 8.6, 6.0)]
        self.m._llm_confirm_buy = Mock(return_value=("llm", False))  # 观望
        # 第一轮：600001 消耗唯一预算(观望→推送锁)，600002 预算用尽 → 待重评推送不锁
        self._scan(rows, [])
        self.assertIn("600001", self.m._alerted_burst_codes)
        self.assertNotIn("600002", self.m._alerted_burst_codes)
        self.assertIn("600002", self.m._deferred_alerted_codes)
        # 600002 虽未锁但已推送告警（审查#3 不再漏报）
        self.assertTrue(any("600002" in str(c) for c in self.bark.call_args_list))
        calls_r1 = self.m._llm_confirm_buy.call_count

        # 第二轮：600001 已锁跳过，600002 重新评估(观望→落锁)
        self._scan(rows, [])
        self.assertGreater(self.m._llm_confirm_buy.call_count, calls_r1)
        self.assertIn("600002", self.m._alerted_burst_codes)


if __name__ == "__main__":
    unittest.main()
