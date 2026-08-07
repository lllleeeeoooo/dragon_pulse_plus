# -*- coding: utf-8 -*-
"""compute_signal_flags 纯函数单元测试（实盘 _scan_signals 与回测共用的信号口径）"""
import unittest
from unittest.mock import patch

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
            # 低吸强势: 涨幅4% 温和放量(量比2) 收阳 短上影 → 命中
            ("600001", "A", 10.4, 4.0, 6e8, 2.0, 10.45, 10.0, 10.1, 10.0, 4.0),
            # 追高: 涨幅9%(>5) → 不命中
            ("600002", "B", 10.9, 9.0, 6e8, 2.0, 10.95, 10.3, 10.4, 10.0, 4.0),
            # 量比过低(1.0<1.2) → 不命中（未放量）
            ("600003", "C", 10.4, 4.0, 6e8, 1.0, 10.45, 10.0, 10.1, 10.0, 4.0),
            # 量比过高(3.0>2.5) → 不命中（放量滞涨/抛压重）
            ("600004", "D", 10.4, 4.0, 6e8, 3.0, 10.45, 10.0, 10.1, 10.0, 4.0),
            # 收阴(close<open) → 不命中
            ("600005", "E", 10.2, 2.0, 6e8, 2.0, 10.5, 10.0, 10.5, 10.0, 5.0),
        ])
        df = compute_signal_flags(spot)
        self.assertTrue(bool(df.iloc[0]["_signal_tail_game"]))
        self.assertFalse(bool(df.iloc[1]["_signal_tail_game"]))  # 涨幅9%追高
        self.assertFalse(bool(df.iloc[2]["_signal_tail_game"]))  # 量比1.0<1.2未放量
        self.assertFalse(bool(df.iloc[3]["_signal_tail_game"]))  # 量比3.0>2.5放量滞涨
        self.assertFalse(bool(df.iloc[4]["_signal_tail_game"]))  # 收阴

    def test_low_open_缺OHLC不崩(self):
        spot = _spot([
            ("600001", "A", 10.3, 3.0, 5e8, 4.0, 10.5, 0.0, 0.0, 10.0, 4.0),  # open/high/low=0
        ])
        df = compute_signal_flags(spot)
        self.assertFalse(bool(df.iloc[0]["_signal_low_open_rally"]))  # OHLC 缺失不触发
        self.assertTrue(bool(df.iloc[0]["_signal_burst"]))           # 其余信号正常

    def test_低开猛拉默认配置仍触发(self):
        """加固后（分母保护+拉升幅度下限）默认口径下正常低开猛拉仍触发"""
        spot = _spot([
            # 低开<98% + 拉回昨收上方 + 放量：拉升强度 0.875>0.8，拉升幅度 7%≥2%
            ("600003", "C", 10.4, 4.0, 4e8, 4.0, 10.5, 9.7, 9.7, 10.0, 7.0),
        ])
        df = compute_signal_flags(spot)
        self.assertTrue(bool(df.iloc[0]["_signal_low_open_rally"]))

    def test_低开猛拉拉升幅度下限过滤(self):
        """仅小幅度拉升(拉升幅度<2%)虽强度高也不触发（防单笔拉高假信号）"""
        from config.settings import settings
        with patch.object(settings, "LOW_OPEN_DEV", 1.0):  # 放宽低开判定，隔离测试拉升幅度条件
            spot = _spot([
                # 强度 (10.1-10.0)/(10.1-10.0)=1.0>0.8，但拉升幅度 1%<2% → 不触发
                ("600001", "A", 10.1, 1.0, 4e8, 4.0, 10.1, 10.0, 10.0, 10.0, 1.0),
            ])
            df = compute_signal_flags(spot)
        self.assertFalse(bool(df.iloc[0]["_signal_low_open_rally"]))

    def test_低开猛拉分母保护防微小区间刷强度(self):
        """价差低于昨收×RALLY_DENOM_MIN_RATIO 时用昨收×比例作分母，避免早盘微小区间刷到 1.0"""
        from config.settings import settings
        with patch.object(settings, "LOW_OPEN_DEV", 1.0), \
             patch.object(settings, "RALLY_MIN_PCT", 0.1):  # 关闭拉升幅度过滤，隔离测试分母保护
            spot = _spot([
                # 价差 0.02(<昨收×1%=0.1)，强度老算法 (10.0-9.98)/0.02=1.0 → 新算法 0.02/0.1=0.2<0.8 不触发
                ("600001", "A", 10.0, 0.2, 4e8, 4.0, 10.0, 9.98, 9.98, 10.0, 0.2),
            ])
            df = compute_signal_flags(spot)
        self.assertFalse(bool(df.iloc[0]["_signal_low_open_rally"]))

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
