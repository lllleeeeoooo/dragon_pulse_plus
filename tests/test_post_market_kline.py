# -*- coding: utf-8 -*-
"""post_market 盘后日线增量同步失败告警测试（_sync_kline_incremental / _alert_kline_fail）"""
import unittest
from unittest.mock import patch


class TestPostMarketKlineSync(unittest.TestCase):
    """异常或空 universe 时记 error 并发 Bark 告警；正常同步不打扰"""

    def test_正常同步不告警(self):
        from scheduler.post_market import _sync_kline_incremental
        with patch("data.kline_etl.KlineEtl.run_incremental",
                   return_value={"universe": 10, "pulled": 3, "skipped": 7}), \
             patch("scheduler.post_market.bark_notifier.send") as bark:
            _sync_kline_incremental()
        bark.assert_not_called()

    def test_执行异常时error并告警(self):
        from scheduler.post_market import _sync_kline_incremental
        with patch("data.kline_etl.KlineEtl.run_incremental",
                   side_effect=ConnectionError("源超时")), \
             patch("scheduler.post_market.bark_notifier.send") as bark, \
             patch("scheduler.post_market.logger.error") as log:
            _sync_kline_incremental()
        bark.assert_called_once()
        self.assertEqual(bark.call_args[1]["group"], "系统告警")
        self.assertIn("增量同步执行异常", bark.call_args[1]["title"])
        log.assert_called_once()

    def test_空universe返回error时告警(self):
        from scheduler.post_market import _sync_kline_incremental
        with patch("data.kline_etl.KlineEtl.run_incremental",
                   return_value={"universe": 0, "pulled": 0, "skipped": 0,
                                 "error": "universe 为空"}), \
             patch("scheduler.post_market.bark_notifier.send") as bark, \
             patch("scheduler.post_market.logger.error") as log:
            _sync_kline_incremental()
        bark.assert_called_once()
        self.assertEqual(bark.call_args[1]["group"], "系统告警")
        self.assertIn("增量同步未执行", bark.call_args[1]["title"])
        log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
