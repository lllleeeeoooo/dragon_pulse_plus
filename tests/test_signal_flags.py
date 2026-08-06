# -*- coding: utf-8 -*-
"""compute_signal_flags 纯函数单元测试（实盘 _scan_signals 与回测共用的信号口径）"""
import unittest

import pandas as pd

from core.signal_flags import compute_signal_flags, signal_labels, primary_signal_label


def _spot(rows):
    df = pd.DataFrame(rows, columns=[
        "code", "name", "price", "change_pct", "amount", "volume_ratio",
        "high", "low", "open", "pre_close", "amplitude"])
    return df


class TestSignalFlags(unittest.TestCase):

    def test_四类信号各自命中(self):
        spot = _spot([
            # 点火异动: 量比>=3 涨幅>=3 未涨停
            ("600001", "A", 10.5, 5.0, 5e8, 4.0, 10.6, 10.1, 10.2, 10.0, 4.0),
            # 逼近封板: 涨幅8-9.5 量比>5
            ("600002", "B", 10.9, 9.0, 6e8, 6.0, 10.95, 10.3, 10.4, 10.0, 6.5),
            # 低开猛拉: 低开<昨收*0.98 量比>3 拉升强度>0.8 涨幅>0
            ("600003", "C", 10.4, 4.0, 4e8, 4.0, 10.5, 9.7, 9.7, 10.0, 7.0),
            # 振幅放量: 振幅>7 量比>3 涨幅>3
            ("600004", "D", 10.4, 4.0, 5e8, 4.0, 11.0, 9.5, 10.0, 10.0, 15.0),
        ])
        df = compute_signal_flags(spot)
        self.assertTrue(bool(df.iloc[0]["_signal_burst"]))
        self.assertFalse(bool(df.iloc[0]["_signal_near_limit"]))
        self.assertTrue(bool(df.iloc[1]["_signal_near_limit"]))
        self.assertTrue(bool(df.iloc[2]["_signal_low_open_rally"]))
        self.assertTrue(bool(df.iloc[3]["_signal_amplitude"]))
        # _signal_hit = 任一命中
        self.assertTrue(all(bool(df.iloc[i]["_signal_hit"]) for i in range(4)))

    def test_阈值边界(self):
        spot = _spot([
            ("600001", "A", 10.29, 2.9, 5e8, 4.0, 10.4, 10.1, 10.2, 10.0, 3.0),  # 涨幅<3 不触发点火
            ("600002", "B", 10.30, 3.0, 5e8, 3.0, 10.4, 10.1, 10.2, 10.0, 3.0),  # 恰好 3.0 触发
            ("600003", "C", 10.9, 9.0, 6e8, 5.0, 10.95, 10.3, 10.4, 10.0, 4.0),  # 量比=5 不触发逼近封板(需>5)
        ])
        df = compute_signal_flags(spot)
        self.assertFalse(bool(df.iloc[0]["_signal_burst"]))
        self.assertTrue(bool(df.iloc[1]["_signal_burst"]))
        self.assertFalse(bool(df.iloc[2]["_signal_near_limit"]))  # 量比 5 不 >5

    def test_尾盘博弈候选(self):
        spot = _spot([
            # 逼近封板区间(8-9.5, 量比6) 未封板 → 尾盘候选命中
            ("600001", "A", 10.9, 9.0, 6e8, 6.0, 10.95, 10.3, 10.4, 10.0, 4.0),
            # 已封板(主板 9.9>=9.8) → 尾盘候选不命中
            ("600002", "B", 10.99, 9.9, 6e8, 6.0, 11.0, 10.4, 10.4, 10.0, 6.0),
            # 涨停附近放量(涨幅>=9.5*0.92=8.74, 量比3) → 命中
            ("600003", "C", 10.88, 8.8, 6e8, 3.0, 10.9, 10.4, 10.4, 10.0, 4.0),
            # 涨幅够但未放量(量比<3) → 不命中
            ("600004", "D", 10.88, 8.8, 6e8, 2.0, 10.9, 10.4, 10.4, 10.0, 4.0),
        ])
        df = compute_signal_flags(spot)
        self.assertTrue(bool(df.iloc[0]["_signal_tail_game"]))
        self.assertFalse(bool(df.iloc[1]["_signal_tail_game"]))  # 封板买不进
        self.assertTrue(bool(df.iloc[2]["_signal_tail_game"]))
        self.assertFalse(bool(df.iloc[3]["_signal_tail_game"]))

    def test_low_open_缺OHLC不崩(self):
        spot = _spot([
            ("600001", "A", 10.3, 3.0, 5e8, 4.0, 10.5, 0.0, 0.0, 10.0, 4.0),  # open/high/low=0
        ])
        df = compute_signal_flags(spot)
        self.assertFalse(bool(df.iloc[0]["_signal_low_open_rally"]))  # OHLC 缺失不触发
        self.assertTrue(bool(df.iloc[0]["_signal_burst"]))           # 其余信号正常

    def test_primary_signal_优先级(self):
        # 同命中 逼近封板 + 点火 → primary = 逼近封板
        spot = _spot([
            ("600001", "A", 10.9, 9.0, 6e8, 6.0, 10.95, 10.3, 10.4, 10.0, 4.0),
        ])
        df = compute_signal_flags(spot)
        row = df.iloc[0]
        self.assertIn("逼近封板", signal_labels(row))
        self.assertIn("点火异动", signal_labels(row))
        self.assertEqual(primary_signal_label(row), "逼近封板")

    def test_输出列完整(self):
        spot = _spot([("600001", "A", 10.3, 3.0, 5e8, 4.0, 10.5, 10.1, 10.2, 10.0, 4.0)])
        df = compute_signal_flags(spot)
        for col in ("_signal_burst", "_signal_near_limit", "_signal_low_open_rally",
                    "_signal_amplitude", "_signal_tail_game", "_signal_hit",
                    "amt_billion", "_limit_max", "_near_limit_min", "_near_limit_max"):
            self.assertIn(col, df.columns)


if __name__ == "__main__":
    unittest.main()
