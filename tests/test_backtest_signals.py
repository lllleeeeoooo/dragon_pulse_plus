# -*- coding: utf-8 -*-
"""回测信号模式（mode="signals"）单元测试：信号买入/尾盘博弈次日兑现/胜率对比"""
import unittest
from unittest.mock import patch

import pandas as pd

from core.backtest import AIBacktestEngine
from core.signal_flags import compute_signal_flags


def _spot_with_signal():
    """一行逼近封板信号（同时命中尾盘博弈候选）的 spot_df"""
    df = pd.DataFrame([{
        "code": "600001", "name": "A", "price": 10.9, "change_pct": 9.0, "amount": 6e8,
        "volume_ratio": 6.0, "high": 10.95, "low": 10.3, "open": 10.4, "pre_close": 10.0,
        "amplitude": 6.5,
    }])
    return compute_signal_flags(df)


class TestSignalBuys(unittest.TestCase):

    def test_process_signal_buys标签与卖出模式(self):
        day_data = {"spot_df": _spot_with_signal()}
        out = AIBacktestEngine._process_signal_buys("20260701", "逼近封板", day_data, 0.3)
        self.assertTrue(out)
        self.assertEqual(out[0]["strategy"], "逼近封板")
        self.assertEqual(out[0]["sell_mode"], "regular")
        self.assertEqual(out[0]["cost_price"], round(10.9 * 1.003, 2))  # 买入=收盘价含滑点
        # 尾盘博弈 → sell_mode="tail_game"
        out2 = AIBacktestEngine._process_signal_buys("20260701", "尾盘博弈", day_data, 0.3)
        self.assertTrue(out2)
        self.assertEqual(out2[0]["sell_mode"], "tail_game")


class TestTailGameSell(unittest.TestCase):

    def _pos(self):
        return {"code": "600001", "name": "A", "cost_price": 10.0, "buy_date": "20260701",
                "sell_mode": "tail_game", "hold_days": 0}

    def test_次日高开冲高兑现(self):
        day_data = {"ohlc_cache": {"600001": {"open": 10.3, "high": 10.8, "close": 10.5}}}
        remaining, closed, _ = AIBacktestEngine._process_tail_game_sells(
            [self._pos()], "20260702", day_data, 1e12, 0.3)
        self.assertEqual(remaining, [])
        self.assertEqual(len(closed), 1)
        self.assertIn("高开", closed[0]["reason"])
        # open 10.3 ≥ 10.0×1.02 → high 卖 10.8×0.997 → return ≈ +7.68%
        self.assertAlmostEqual(closed[0]["return_pct"], 7.68, places=1)

    def test_次日未高开按开盘兑现(self):
        day_data = {"ohlc_cache": {"600001": {"open": 9.8, "high": 9.9, "close": 9.7}}}
        remaining, closed, _ = AIBacktestEngine._process_tail_game_sells(
            [self._pos()], "20260702", day_data, 1e12, 0.3)
        self.assertIn("未高开", closed[0]["reason"])
        # open 9.8 < 10.2 → open 卖 9.8×0.997 → return ≈ -2.29%
        self.assertAlmostEqual(closed[0]["return_pct"], -2.29, places=1)


class TestSummarizeSignalCompare(unittest.TestCase):

    def test_signal_compare按策略桶统计(self):
        trades = [
            {"code": "1", "name": "A", "strategy": "逼近封板", "return_pct": 3.0, "hold_days": 1},
            {"code": "2", "name": "B", "strategy": "逼近封板", "return_pct": -1.0, "hold_days": 1},
            {"code": "3", "name": "C", "strategy": "尾盘博弈", "return_pct": 2.0, "hold_days": 1},
        ]
        res = AIBacktestEngine._summarize(trades, [], ["20260701"], "20260701", "20260701",
                                          10 ** 6, 10 ** 6, set(), signal_mode=True)
        sc = res["signal_compare"]
        self.assertIn("逼近封板", sc)
        self.assertIn("尾盘博弈", sc)
        self.assertIn("全部信号", sc)
        self.assertEqual(sc["逼近封板"]["win_rate_pct"], 50.0)
        self.assertEqual(sc["逼近封板"]["avg_return_pct"], 1.0)
        self.assertEqual(sc["尾盘博弈"]["win_rate_pct"], 100.0)
        self.assertEqual(sc["全部信号"]["trades"], 3)
        self.assertEqual(res["max_positions"], "不限")


class TestRunSignalsEndToEnd(unittest.TestCase):

    def test_run_signals_end_to_end(self):
        # 8 个交易日：600001 最后一日放量逼近封板（量比=60000/10000=6>5）触发信号
        dates = [f"202606{i+2:02d}" for i in range(8)]
        bars = {}
        for code in ("600001", "600002"):
            rows = []
            for i, d in enumerate(dates):
                active = (code == "600001" and i == 7)
                rows.append({"trade_date": d, "open": 10.0, "high": 10.5, "low": 9.8,
                             "close": 10.9 if active else 10.0,
                             "volume": 60000.0 if active else 10000.0,
                             "amount": 600000.0 if active else 100000.0,
                             "pre_close": 10.0,
                             "change_pct": 9.0 if active else 0.0, "amplitude": 7.0})
            bars[code] = pd.DataFrame(rows).set_index("trade_date")
        with patch("data.kline_etl.KlineEtl.load_cache", return_value=bars), \
             patch("core.backtest.AIBacktestEngine._build_trade_date_list", return_value=dates):
            res = AIBacktestEngine.run(dates[0], dates[-1], mode="signals")
        self.assertIn("signal_compare", res)
        self.assertGreater(res["total_trades"], 0)
        self.assertIn("逼近封板", res["signal_compare"])
        self.assertIn("尾盘博弈", res["signal_compare"])


if __name__ == "__main__":
    unittest.main()
