# -*- coding: utf-8 -*-
"""全市场日线缓存（daily_kline 表 + DailyKlineManager + ETL 逻辑）单元测试"""
import unittest
from unittest.mock import patch

import pandas as pd

from database.connection import db_manager, switch_to_test_db
from database.models import Base, DailyKline
from database.kline import DailyKlineManager


class TestDailyKlineManager(unittest.TestCase):

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
            session.query(DailyKline).delete()
            session.commit()
        finally:
            session.close()

    def _row(self, code, date, close=10.0, change_pct=3.0, volume=1000.0):
        return {"code": code, "trade_date": date, "open": 9.9, "high": 10.5,
                "low": 9.8, "close": close, "volume": volume, "amount": close * volume,
                "pre_close": 9.9, "change_pct": change_pct, "amplitude": 7.0}

    def test_upsert幂等(self):
        DailyKlineManager.upsert_batch([self._row("600001", "20260701")])
        DailyKlineManager.upsert_batch([self._row("600001", "20260701", close=11.0)])
        session = db_manager.get_session()
        try:
            rows = session.query(DailyKline).all()
        finally:
            session.close()
        self.assertEqual(len(rows), 1)          # 同 (code, trade_date) 覆盖为 1 行
        self.assertAlmostEqual(rows[0].close, 11.0)

    def test_complete_codes断点续传(self):
        # 600001 覆盖全部 3 天 → 视为完整；600002 只覆盖 1 天 → 跳过排除
        rows = [self._row("600001", d) for d in ("20260701", "20260702", "20260703")]
        rows += [self._row("600002", "20260701")]
        DailyKlineManager.upsert_batch(rows)
        complete = DailyKlineManager.complete_codes("20260701", "20260703", expected_days=3)
        self.assertEqual(complete, {"600001"})
        complete2 = DailyKlineManager.complete_codes("20260701", "20260703", expected_days=1)
        self.assertEqual(complete2, {"600001", "600002"})

    def test_load_range列正确(self):
        DailyKlineManager.upsert_batch([self._row("600001", "20260701")])
        df = DailyKlineManager.load_range("20260701", "20260701")
        self.assertFalse(df.empty)
        for col in ("code", "trade_date", "open", "high", "low", "close",
                    "volume", "amount", "pre_close", "change_pct", "amplitude"):
            self.assertIn(col, df.columns)

    def test_count_rows(self):
        DailyKlineManager.upsert_batch([self._row("600001", "20260701"),
                                        self._row("600002", "20260701")])
        self.assertEqual(DailyKlineManager.count_rows(), 2)

    def test_max_trade_date(self):
        self.assertEqual(DailyKlineManager.max_trade_date(), "")  # 空表
        DailyKlineManager.upsert_batch([self._row("600001", "20260701"),
                                        self._row("600002", "20260703")])
        self.assertEqual(DailyKlineManager.max_trade_date(), "20260703")


class TestKlineEtl(unittest.TestCase):
    """ETL 逻辑：fetch_one 归一化 / build_day_spot 组装 / 断点续传 / 增量起点"""

    def setUp(self):
        session = db_manager.get_session()
        try:
            session.query(DailyKline).delete()
            session.commit()
        finally:
            session.close()
        import data.kline_etl as _k
        _k._disabled_sources.clear()  # 清空模块级源熔断缓存，避免测试间污染

    def _krow(self, code, date):
        return {"code": code, "trade_date": date, "open": 9.9, "high": 10.5, "low": 9.8,
                "close": 10.0, "volume": 1000.0, "amount": 10000.0, "pre_close": 9.9,
                "change_pct": 3.0, "amplitude": 7.0}

    def test_run_incremental起点为已同步最大日期加1(self):
        from data.kline_etl import KlineEtl
        DailyKlineManager.upsert_batch([self._krow("600001", "20260703")])
        with patch("data.kline_etl.KlineEtl.run", return_value={}) as m:
            KlineEtl.run_incremental()
        start, end = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(start, "20260704")  # last=20260703 → 从次日增量拉
        import datetime as _dt
        self.assertEqual(end, _dt.date.today().strftime("%Y%m%d"))

    def test_run_incremental缓存已最新则不拉(self):
        from data.kline_etl import KlineEtl
        import datetime as _dt
        today = _dt.date.today().strftime("%Y%m%d")
        DailyKlineManager.upsert_batch([self._krow("600001", today)])
        with patch("data.kline_etl.KlineEtl.run", return_value={}) as m:
            r = KlineEtl.run_incremental()
        self.assertIn("已是最新", r["message"])
        m.assert_not_called()

    def test_fetch_one归一化(self):
        from data.kline_etl import KlineEtl
        raw = pd.DataFrame({
            "日期": ["2026-07-01"], "开盘": [10.0], "收盘": [10.5], "最高": [10.8],
            "最低": [9.9], "成交量": [100000], "成交额": [1050000.0], "振幅": [9.0],
            "涨跌幅": [5.0], "涨跌额": [0.5], "换手率": [3.0],
        })
        with patch("akshare.stock_zh_a_hist", return_value=raw):
            df = KlineEtl.fetch_one("600001", "20260701", "20260731")
        self.assertEqual(df["trade_date"].iloc[0], "20260701")  # 去 '-'
        self.assertEqual(df["code"].iloc[0], "600001")
        self.assertAlmostEqual(df["pre_close"].iloc[0], 10.0)  # close 10.5 - 涨跌额 0.5

    def test_fetch_one东财限流降级新浪(self):
        from data.kline_etl import KlineEtl
        sina_raw = pd.DataFrame({
            "date": ["2026-06-30", "2026-07-01"],
            "open": [10.0, 10.5], "high": [10.2, 10.8], "low": [9.9, 10.3],
            "close": [10.0, 10.6], "volume": [100000, 120000],
            "amount": [1000000.0, 1300000.0],
        })
        with patch("akshare.stock_zh_a_hist", side_effect=ConnectionError("东财限流")), \
             patch("akshare.stock_zh_a_daily", return_value=sina_raw):
            df = KlineEtl.fetch_one("600001", "20260620", "20260805")
        self.assertFalse(df.empty)
        self.assertEqual(df["code"].iloc[0], "600001")
        self.assertAlmostEqual(df["change_pct"].iloc[-1], 6.0)  # (10.6-10.0)/10.0 自算
        self.assertAlmostEqual(df["amplitude"].iloc[-1], 5.0)   # (10.8-10.3)/10.0 自算
        self.assertAlmostEqual(df["pre_close"].iloc[-1], 10.0)  # 前一日收盘作昨收

    def test_fetch_one东财新浪失败降级腾讯(self):
        from data.kline_etl import KlineEtl
        tx_raw = pd.DataFrame({
            "date": ["2026-06-30", "2026-07-01"],
            "open": [10.0, 10.5], "high": [10.2, 10.8], "low": [9.9, 10.3],
            "close": [10.0, 10.6], "volume": [100000, 120000],
            "amount": [1000000.0, 1300000.0],
        })
        with patch("akshare.stock_zh_a_hist", side_effect=ConnectionError("东财限流")), \
             patch("akshare.stock_zh_a_daily", return_value=pd.DataFrame()), \
             patch("akshare.stock_zh_a_hist_tx", return_value=tx_raw):
            df = KlineEtl.fetch_one("600001", "20260620", "20260805")
        self.assertFalse(df.empty)
        self.assertAlmostEqual(df["change_pct"].iloc[-1], 6.0)
        self.assertAlmostEqual(df["amplitude"].iloc[-1], 5.0)

    def test_build_day_spot_price等于close且量比正确(self):
        from data.kline_etl import KlineEtl
        dates = ["20260701", "20260702", "20260703", "20260706", "20260707", "20260708"]
        rows = []
        for i, d in enumerate(dates):
            rows.append({"trade_date": d, "open": 10.0, "high": 10.5, "low": 9.8,
                         "close": 10.3 + i * 0.1, "volume": 2000, "amount": 20000.0,
                         "pre_close": 10.0, "change_pct": 3.0, "amplitude": 7.0})
        rows[-1] = {**rows[-1], "volume": 12000}  # 最后一日放量 12000
        bars = pd.DataFrame(rows).set_index("trade_date")
        spot = KlineEtl.build_day_spot({"600001": bars}, "20260708")
        self.assertEqual(len(spot), 1)
        self.assertEqual(spot.iloc[0]["price"], spot.iloc[0]["close"])  # price=close 列统一
        self.assertAlmostEqual(spot.iloc[0]["volume_ratio"], 12000 / 2000)  # 当日量/前5日均量

    def test_etl断点续传跳过已完整code(self):
        from data.kline_etl import KlineEtl
        fake_df = pd.DataFrame({"code": ["600002"], "trade_date": ["20260701"]})
        with patch("data.kline_etl.KlineEtl.fetch_universe",
                   return_value=pd.DataFrame({"code": ["600001", "600002"]})), \
             patch("database.kline.DailyKlineManager.complete_codes",
                   return_value={"600001"}), \
             patch("core.backtest.AIBacktestEngine._build_trade_date_list",
                   return_value=["20260701", "20260702"]), \
             patch("database.kline.DailyKlineManager.upsert_batch"), \
             patch("data.kline_etl.KlineEtl.fetch_one", return_value=fake_df) as m:
            r = KlineEtl.run("20260701", "20260702", workers=2)
        self.assertEqual(r["pulled"], 1)  # 600001 已完整 → 跳过，只拉 600002
        self.assertEqual(r["skipped"], 1)
        m.assert_called_once_with("600002", "20260701", "20260702")


if __name__ == "__main__":
    unittest.main()
