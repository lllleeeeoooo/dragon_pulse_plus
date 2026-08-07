# -*- coding: utf-8 -*-
"""post_market 独立日线同步任务测试：job_kline_sync 调度 / _sync_kline_incremental 进度落库与告警 /
   _get_job_status 看板状态解析（job_kline_sync 含 status/progress）"""
import unittest
from unittest.mock import patch


class TestSyncKlineThread(unittest.TestCase):
    """日线同步线程体：串行摊速 + 进度/完成/失败状态落库 + 失败告警"""

    def test_正常同步不告警并记录完成状态(self):
        from scheduler.post_market import _sync_kline_incremental
        with patch("data.kline_etl.KlineEtl.run_incremental_paced",
                   return_value={"universe": 10, "pulled": 8, "remaining": 2}), \
             patch("scheduler.post_market._record_job_run") as rec, \
             patch("scheduler.post_market.bark_notifier.send") as bark:
            _sync_kline_incremental()
        bark.assert_not_called()
        # 完成态：state=完成, progress=pulled/universe
        last = rec.call_args_list[-1]
        self.assertEqual(last.args[0], "job_kline_sync")
        self.assertEqual(last.kwargs.get("state"), "完成")
        self.assertEqual(last.kwargs.get("progress"), "8/10")

    def test_执行异常时error并告警(self):
        from scheduler.post_market import _sync_kline_incremental
        with patch("data.kline_etl.KlineEtl.run_incremental_paced",
                   side_effect=ConnectionError("源超时")), \
             patch("scheduler.post_market._record_job_run"), \
             patch("scheduler.post_market.bark_notifier.send") as bark, \
             patch("scheduler.post_market.logger.error") as log:
            _sync_kline_incremental()
        bark.assert_called_once()
        self.assertEqual(bark.call_args[1]["group"], "系统告警")
        self.assertIn("增量同步执行异常", bark.call_args[1]["title"])
        log.assert_called_once()

    def test_空universe返回error时告警并记失败(self):
        from scheduler.post_market import _sync_kline_incremental
        with patch("data.kline_etl.KlineEtl.run_incremental_paced",
                   return_value={"universe": 0, "pulled": 0, "remaining": 0,
                                 "error": "universe 为空"}), \
             patch("scheduler.post_market._record_job_run") as rec, \
             patch("scheduler.post_market.bark_notifier.send") as bark, \
             patch("scheduler.post_market.logger.error") as log:
            _sync_kline_incremental()
        bark.assert_called_once()
        self.assertEqual(bark.call_args[1]["group"], "系统告警")
        self.assertIn("增量同步未执行", bark.call_args[1]["title"])
        log.assert_called_once()
        self.assertEqual(rec.call_args_list[-1].kwargs.get("state"), "失败")


class TestJobKlineSync(unittest.TestCase):
    """15:30 独立日线同步定时任务：交易日启动后台线程 / 非交易日跳过 / 启动失败告警"""

    def test_交易日启动线程(self):
        from scheduler.post_market import job_kline_sync
        with patch("scheduler.post_market.is_trading_day", return_value=True), \
             patch("scheduler.post_market._record_job_run") as rec, \
             patch("scheduler.post_market.threading.Thread") as Thread:
            job_kline_sync()
        # 启动即记 运行中
        self.assertEqual(rec.call_args_list[0].args[0], "job_kline_sync")
        self.assertEqual(rec.call_args_list[0].kwargs.get("state"), "运行中")
        Thread.assert_called_once()  # 后台线程串行摊速

    def test_非交易日跳过(self):
        from scheduler.post_market import job_kline_sync
        with patch("scheduler.post_market.is_trading_day", return_value=False), \
             patch("scheduler.post_market._record_job_run") as rec, \
             patch("scheduler.post_market.threading.Thread") as Thread:
            job_kline_sync()
        self.assertEqual(rec.call_args_list[-1].kwargs.get("state"), "跳过")
        Thread.assert_not_called()

    def test_线程启动失败记失败并告警(self):
        from scheduler.post_market import job_kline_sync
        with patch("scheduler.post_market.is_trading_day", return_value=True), \
             patch("scheduler.post_market._record_job_run") as rec, \
             patch("scheduler.post_market.threading.Thread",
                   side_effect=RuntimeError("线程创建失败")), \
             patch("scheduler.post_market.bark_notifier.send") as bark:
            job_kline_sync()
        self.assertEqual(rec.call_args_list[-1].kwargs.get("state"), "失败")
        bark.assert_called_once()


class TestJobStatus(unittest.TestCase):
    """看板定时任务状态：job_kline_sync 出现在列表，status/progress 正确解析"""

    def test_日线同步任务与状态解析(self):
        from scheduler.helpers import _get_job_status
        logs = [
            {"detail": "15:30:00|job_kline_sync|运行中|123/4800"},
            {"detail": "18:01:05|job_post_market|"},
        ]
        with patch("scheduler.helpers.SystemLogManager.get_logs", return_value=logs):
            jobs = _get_job_status()
        by_id = {j["id"]: j for j in jobs}
        self.assertIn("job_kline_sync", by_id)
        k = by_id["job_kline_sync"]
        self.assertEqual(k["time"], "15:30")
        self.assertTrue(k["ran_today"])
        self.assertEqual(k["status"], "运行中")
        self.assertEqual(k["progress"], "123/4800")
        self.assertEqual(k["last_run"], "15:30:00")

    def test_无状态时回退已执行未执行(self):
        from scheduler.helpers import _get_job_status
        with patch("scheduler.helpers.SystemLogManager.get_logs", return_value=[]):
            jobs = _get_job_status()
        by_id = {j["id"]: j for j in jobs}
        self.assertFalse(by_id["job_kline_sync"]["ran_today"])
        self.assertEqual(by_id["job_kline_sync"]["status"], "")
        self.assertEqual(by_id["job_kline_sync"]["last_run"], "-")


if __name__ == "__main__":
    unittest.main()
