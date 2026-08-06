"""
全市场日线缓存管理（回测信号模式用）
================================
一次性 ETL 拉取全市场历史日线存 daily_kline，(code, trade_date) 唯一索引，
供回测用日线近似四类信号。幂等 upsert + 断点续传（complete_codes 跳过已完整覆盖的 code）。
"""
import logging

import pandas as pd
from sqlalchemy import text

from database.connection import db_manager

logger = logging.getLogger(__name__)

_INSERT_SQL = text(
    "INSERT OR REPLACE INTO daily_kline "
    "(code, trade_date, open, high, low, close, volume, amount, pre_close, change_pct, amplitude) "
    "VALUES (:code, :trade_date, :open, :high, :low, :close, :volume, :amount, "
    ":pre_close, :change_pct, :amplitude)"
)


class DailyKlineManager:
    """全市场日线缓存管理"""

    @staticmethod
    def upsert_batch(rows: list):
        """批量幂等写入：同 (code, trade_date) 重复覆盖"""
        if not rows:
            return
        with db_manager.engine.begin() as conn:
            conn.execute(_INSERT_SQL, rows)

    @staticmethod
    def complete_codes(start: str, end: str, expected_days: int) -> set:
        """返回在 [start,end] 区间内交易日覆盖数 >= expected_days 的 code（断点续传跳过用）"""
        if expected_days <= 0:
            return set()
        with db_manager.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT code FROM daily_kline "
                "WHERE trade_date >= :start AND trade_date <= :end "
                "GROUP BY code HAVING COUNT(DISTINCT trade_date) >= :days"),
                {"start": start, "end": end, "days": expected_days}).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def load_range(start: str, end: str) -> pd.DataFrame:
        """读取区间内全部日线（含前 warmup 窗口由调用方扩展 start）"""
        with db_manager.engine.connect() as conn:
            df = pd.read_sql(text(
                "SELECT code, trade_date, open, high, low, close, volume, amount, "
                "pre_close, change_pct, amplitude "
                "FROM daily_kline WHERE trade_date >= :start AND trade_date <= :end"),
                conn, params={"start": start, "end": end})
        return df

    @staticmethod
    def count_rows() -> int:
        with db_manager.engine.connect() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM daily_kline")).scalar() or 0

    @staticmethod
    def max_trade_date() -> str:
        """已同步的最大交易日期 YYYYMMDD，无数据返回空串（盘后增量同步起点用）"""
        with db_manager.engine.connect() as conn:
            return conn.execute(text("SELECT MAX(trade_date) FROM daily_kline")).scalar() or ""
