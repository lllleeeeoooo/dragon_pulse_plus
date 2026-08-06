# -*- coding: utf-8 -*-
"""
四类抢筹信号 + 尾盘博弈候选 的纯函数计算。

从 scheduler.monitor_core._scan_signals 抽取，实盘与回测共用同一套阈值，
保证回测口径与实盘一致（避免"回测信号 vs 实盘信号"分叉）。
"""
from typing import Dict

import pandas as pd

from config.settings import settings

# 信号标签 → 信号列 的映射（标签顺序即 primary 优先级）
SIGNAL_LABEL_COLS = [
    ("逼近封板", "_signal_near_limit"),
    ("低开猛拉", "_signal_low_open_rally"),
    ("点火异动", "_signal_burst"),
    ("振幅放量", "_signal_amplitude"),
]


def compute_signal_flags(spot_df: pd.DataFrame, dragons: Dict[str, float] = None) -> pd.DataFrame:
    """
    输入 spot_df 必须含列：code, name, price, change_pct, amount, volume_ratio,
    high, low, open, pre_close, amplitude（回测中 price 用当日收盘 close 填充）。

    输出：原 df 副本 + 辅助列 amt_billion/_limit_max/_near_limit_min/_near_limit_max
        + 四布尔列 _signal_burst/_signal_near_limit/_signal_low_open_rally/_signal_amplitude
        + _signal_tail_game（尾盘博弈候选，实盘不使用）+ _signal_hit（任一信号命中）
        + _signal_second_wave（龙头二波候选：dragons={code: peak_price} 传入时计算，否则 False）
    返回完整 df；调用方自行取 df[df["_signal_hit"]] 得到候选池。
    """
    df = spot_df.copy()
    # 统一 close：实盘 spot 只有 price(盘中现价)，回测 build_day_spot 有 close(收盘价)；
    # 尾盘博弈候选等需要收盘价列，缺失时用 price 兜底
    if "close" not in df.columns:
        df["close"] = df["price"]
    df["amt_billion"] = df["amount"].astype(float) / 1e8

    # 辅助列：低开猛拉的拉升强度 = (现价-开盘) / (最高-最低)
    price_range = df["high"].astype(float) - df["low"].astype(float)
    rally_strength = (df["price"].astype(float) - df["open"].astype(float)) / price_range.replace(0, 1)

    # 按板块区分涨停线：主板 10cm vs 双创 20cm（科创板已在源头过滤，此处主要区分创业板 300）
    is_main_board = df["code"].astype(str).str.match(r"^(60|00)")
    df["_limit_max"] = settings.PRICE_BURST_MAX  # 主板 10cm
    df.loc[~is_main_board, "_limit_max"] = settings.PRICE_BURST_MAX_20CM  # 双创 20cm
    # 逼近封板区间 = 涨停线的 80%~100%
    df["_near_limit_min"] = df["_limit_max"] * settings.MONITOR_NEAR_LIMIT_RATIO  # 涨停线×比值
    df["_near_limit_max"] = df["_limit_max"]

    df["_signal_burst"] = (
        (df["volume_ratio"] >= settings.VOL_BURST_THRESHOLD) &
        (df["change_pct"] >= settings.PRICE_BURST_THRESHOLD) &
        (df["change_pct"] < df["_limit_max"])
    )
    df["_signal_near_limit"] = (
        (df["change_pct"] >= df["_near_limit_min"]) &
        (df["change_pct"] <= df["_near_limit_max"]) &
        (df["volume_ratio"] > settings.NEAR_LIMIT_VOL_RATIO)
    )
    df["_signal_low_open_rally"] = (
        (df["open"].astype(float) > 0) &  # OHLC 数据缺失时（0值）不触发，避免垃圾判定
        (df["open"].astype(float) < df["pre_close"].astype(float) * settings.LOW_OPEN_DEV) &
        (df["volume_ratio"] > settings.RALLY_VOL_RATIO) &
        (rally_strength > settings.RALLY_STRENGTH_MIN) &
        (df["change_pct"] > 0)
    )
    df["_signal_amplitude"] = (
        (df["amplitude"] > settings.AMPLITUDE_SIGNAL_MIN) &
        (df["volume_ratio"] > settings.RALLY_VOL_RATIO) &
        (df["change_pct"] > settings.AMPLITUDE_CHANGE_MIN)
    )

    # 尾盘博弈候选（回测专用，实盘 _scan_signals 不消费此列）——指南「低吸强势股博次日高开」：
    # 涨幅 2%~5%（不追高）+ 放量 + 收阳 + 短上影线(收盘卖压小) + 收盘≥全天均价(尾盘强势)
    if "volume" in df.columns:
        df["vwap"] = df["amount"].astype(float) / df["volume"].astype(float).replace(0, float("nan"))
    else:
        df["vwap"] = df["close"].astype(float)  # 无 volume 列时 vwap≈close，close≥vwap 恒成立
    _upper_ratio = (df["high"].astype(float) - df["close"].astype(float)) / \
                   (df["high"].astype(float) - df["low"].astype(float)).replace(0, 1)
    df["_signal_tail_game"] = (
        (df["change_pct"] >= settings.TAIL_GAME_CHANGE_MIN) &
        (df["change_pct"] <= settings.TAIL_GAME_CHANGE_MAX) &
        (df["volume_ratio"] >= settings.TAIL_GAME_VOL_RATIO) &
        (df["close"].astype(float) > df["open"].astype(float)) &  # 收阳
        (_upper_ratio <= settings.TAIL_GAME_SHORT_UPPER_RATIO) &   # 短上影线
        (df["close"].astype(float) >= df["vwap"].fillna(df["close"].astype(float)))  # 收盘≥均价
    )

    # 任一抢筹信号命中
    df["_signal_hit"] = (df["_signal_burst"] | df["_signal_near_limit"] |
                         df["_signal_low_open_rally"] | df["_signal_amplitude"])

    # 龙头二波候选（dragons={code: peak_price} 传入时计算；实盘 _scan_second_wave 传入，回测/白天信号默认 False）：
    # 近30天历史龙头 + 现价相对第一波最高价回撤 30%~50% + 当日涨幅>3%（止跌反包）
    if dragons:
        df["_peak_price"] = df["code"].astype(str).map(dragons)
        _retreat = (df["_peak_price"].astype(float) - df["price"].astype(float)) / df["_peak_price"].astype(float)
        df["_signal_second_wave"] = (
            df["_peak_price"].notna() &
            (_retreat >= settings.SECOND_WAVE_RETREAT_MIN) &
            (_retreat <= settings.SECOND_WAVE_RETREAT_MAX) &
            (df["change_pct"] > settings.SECOND_WAVE_CHANGE_MIN)
        )
    else:
        df["_signal_second_wave"] = False
    return df


def signal_labels(row: pd.Series) -> list:
    """返回该行命中的所有信号标签列表（按 SIGNAL_LABEL_COLS 顺序）"""
    return [label for label, col in SIGNAL_LABEL_COLS if bool(row.get(col))]


def primary_signal_label(row: pd.Series) -> str:
    """返回主要信号标签（SIGNAL_LABEL_COLS 顺序第一个命中的），未命中返回"其他" """
    labels = signal_labels(row)
    return labels[0] if labels else "其他"
