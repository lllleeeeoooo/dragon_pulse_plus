# -*- coding: utf-8 -*-
"""
全市场历史日线 ETL（回测信号模式数据源）
========================================
一次性拉取全市场个股日线到 daily_kline 表（(code, trade_date) 唯一，幂等 upsert），
断点续传：已完整覆盖区间的 code 跳过。回测用 build_day_spot 把日线组装成
compute_signal_flags 需要的 spot 形态（日线近似四类信号，收盘口径）。

CLI: python -m data.kline_etl --start 20260701 --end 20260731 [--workers 8] [--force]
"""
import argparse
import datetime
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

import pandas as pd

from data.core import socket_timeout

logger = logging.getLogger(__name__)

# 本进程内已确认不可用的日线源（连续失败达阈值才标记跳过，偶发失败不跳过）
_disabled_sources: set = set()
_source_fail_count: dict = {}

# 日线中文列 → 英文列 映射
_COL_MAP = {
    "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
    "振幅": "amplitude", "涨跌幅": "change_pct", "涨跌额": "change_amount",
}


class KlineEtl:
    """全市场日线 ETL + 回测 spot 组装"""

    # ------------------------------------------------------------------
    # 1) ETL：拉取落库
    # ------------------------------------------------------------------

    @staticmethod
    def fetch_universe() -> pd.DataFrame:
        """当前全市场股票列表（多源降级东财→腾讯→新浪 + 过滤 688/BSE/ST，对齐实盘口径）。
        复用 get_realtime_spot 而非单源 stock_zh_a_spot_em，避免东财限流时拿不到股票列表。"""
        from data.fetcher_spot import _SpotMixin
        df = _SpotMixin.get_realtime_spot()
        if df is None or df.empty:
            return pd.DataFrame()
        return df[["code", "name"]]

    @staticmethod
    def _normalize_ohlcv(df: pd.DataFrame, code: str) -> pd.DataFrame:
        """从 OHLCV 日线自算 pre_close/change_pct/amplitude（新浪/腾讯共用，东财已带这些字段）"""
        df = df.rename(columns={"date": "trade_date"})
        df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
        df["code"] = str(code)
        closes = pd.to_numeric(df["close"], errors="coerce")
        df["pre_close"] = closes.shift(1)
        df["change_pct"] = ((closes - df["pre_close"]) / df["pre_close"] * 100).round(2)
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        df["amplitude"] = ((high - low) / df["pre_close"] * 100).round(2)
        return df

    @staticmethod
    @socket_timeout()
    def fetch_one(code: str, start: str, end: str) -> pd.DataFrame:
        """拉单只股票区间日线并归一化列（socket 超时保护，源挂起不阻塞 ETL worker）。
        多源降级：东财 stock_zh_a_hist（首选，自带涨跌幅/振幅/涨跌额）→
        新浪 stock_zh_a_daily → 腾讯 stock_zh_a_hist_tx（后两者从 OHLC 自算）。"""
        import akshare as ak
        from data.fetcher_history import _HistoryMixin
        try:
            if "东财" not in _disabled_sources:
                df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date=start, end_date=end, adjust="qfq")
                if df is None or df.empty:
                    raise ValueError("东财返回空")
                df = df.rename(columns=_COL_MAP)
                df["trade_date"] = df["trade_date"].astype(str).str.replace("-", "")
                df["code"] = str(code)
                # 昨收 = 收盘 - 涨跌额（akshare 涨跌额为绝对额）
                df["pre_close"] = pd.to_numeric(df["close"], errors="coerce") - \
                                  pd.to_numeric(df.get("change_amount", 0), errors="coerce")
            else:
                raise RuntimeError("东财本进程已失效")
        except Exception as e:
            _disabled_sources.add("东财")
            logger.warning(f"{code} 东财日线失败，尝试备用源: {e}")
            prefix = _HistoryMixin._market_prefix(code)
            df = pd.DataFrame()
            for name, fn in [
                ("新浪", lambda: ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
                                                     start_date=start, end_date=end, adjust="qfq")),
                ("腾讯", lambda: ak.stock_zh_a_hist_tx(symbol=f"{prefix}{code}",
                                                       start_date=start, end_date=end, adjust="qfq")),
            ]:
                if name in _disabled_sources:
                    continue
                # 空结果重试（限流常静默返回空而非异常）：最多 3 次，间隔递增，避免限流丢整只
                for attempt in range(4):
                    try:
                        tmp = fn()
                        if tmp is not None and not tmp.empty:
                            _source_fail_count[name] = 0  # 成功清零
                            df = KlineEtl._normalize_ohlcv(tmp, code)
                            break
                        if attempt < 3:
                            time.sleep(1.0 * (attempt + 1))
                    except Exception as e2:
                        # 连续失败达阈值才跳过该源（偶发一次失败不永久标记，避免限流抖动丢整个源）
                        _source_fail_count[name] = _source_fail_count.get(name, 0) + 1
                        if _source_fail_count[name] >= 5:
                            _disabled_sources.add(name)
                        logger.warning(f"{code} {name}日线失败(连续{_source_fail_count[name]}次): {e2}")
                        break
                if not df.empty:
                    break
            if df is None or df.empty:
                return pd.DataFrame()
        keep = ["code", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "pre_close", "change_pct", "amplitude"]
        return df[[c for c in keep if c in df.columns]]

    @staticmethod
    def run_incremental(workers: int = None) -> dict:
        """盘后增量同步：从 daily_kline 已同步最大日期+1 拉到今天，只补新增交易日。
        首次（缓存为空）回退拉最近 KLINE_ETL_BOOTSTRAP_DAYS 天全量。"""
        from config.settings import settings
        from database.kline import DailyKlineManager
        last = DailyKlineManager.max_trade_date()
        end = datetime.date.today().strftime("%Y%m%d")
        if last:
            start = (datetime.datetime.strptime(last, "%Y%m%d") +
                     datetime.timedelta(days=1)).strftime("%Y%m%d")
        else:
            start = (datetime.date.today() -
                     datetime.timedelta(days=settings.KLINE_ETL_BOOTSTRAP_DAYS)).strftime("%Y%m%d")
        if start > end:
            return {"message": "日线缓存已是最新，无需同步", "pulled": 0, "skipped": 0}
        return KlineEtl.run(start, end, workers)

    @staticmethod
    def run(start: str, end: str, workers: int = None, force: bool = False) -> dict:
        """并行拉取全市场日线，断点续传。返回统计信息。"""
        from config.settings import settings
        from database.kline import DailyKlineManager
        from core.backtest import AIBacktestEngine
        workers = workers or settings.KLINE_ETL_WORKERS
        universe = KlineEtl.fetch_universe()
        if universe is None or universe.empty:
            return {"universe": 0, "pulled": 0, "skipped": 0, "error": "universe 为空"}
        expected_days = len(AIBacktestEngine._build_trade_date_list(start, end))
        done = set() if force else DailyKlineManager.complete_codes(start, end, expected_days)
        todo = [str(c) for c in universe["code"] if str(c) not in done]
        if todo:
            from tqdm import tqdm
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(KlineEtl.fetch_one, code, start, end): code for code in todo}
                for fut in tqdm(as_completed(futs), total=len(todo), desc="拉取日线"):
                    code = futs[fut]
                    try:
                        df = fut.result()
                        if df is not None and not df.empty:
                            DailyKlineManager.upsert_batch(df.to_dict("records"))
                    except Exception as e:
                        logger.warning(f"{code} 日线拉取失败: {e}")  # 单只失败不中断整体
        return {"universe": len(universe), "pulled": len(todo), "skipped": len(done),
                "rows": DailyKlineManager.count_rows()}

    # ------------------------------------------------------------------
    # 2) 回测读取：load_cache + build_day_spot
    # ------------------------------------------------------------------

    @staticmethod
    def load_cache(start: str, end: str, warmup_days: int = 10) -> Dict[str, pd.DataFrame]:
        """从 daily_kline 读区间（扩展 warmup 窗口供前5日均量），按 code 分组，
        返回 {code: DataFrame(index=trade_date, cols=open/high/low/close/volume/...)}"""
        from database.kline import DailyKlineManager
        ext_start = (datetime.datetime.strptime(start, "%Y%m%d") -
                     datetime.timedelta(days=warmup_days)).strftime("%Y%m%d")
        df = DailyKlineManager.load_range(ext_start, end)
        if df is None or df.empty:
            return {}
        cache: Dict[str, pd.DataFrame] = {}
        for code, g in df.groupby("code"):
            g = g.sort_values("trade_date").set_index("trade_date")
            g.index = g.index.astype(str)
            cache[str(code)] = g
        return cache

    @staticmethod
    def build_day_spot(kline_cache: Dict[str, pd.DataFrame], date_str: str) -> pd.DataFrame:
        """把某日全市场日线条组装成 compute_signal_flags 需要的 spot_df。
        price=close（回测用收盘价近似盘中价/尾盘价）；volume_ratio=当日量/前5日均量；
        同时提供 close 列，避免信号(price)与尾盘卖出/close_cache(close)读列不一致。"""
        rows = []
        for code, bars in kline_cache.items():
            if date_str not in bars.index:
                continue
            bar = bars.loc[date_str]
            prev = bars.loc[:date_str].iloc[:-1]["volume"].tail(5)
            vol_ratio = float(bar["volume"]) / prev.mean() if len(prev) >= 3 and prev.mean() > 0 else 1.0
            rows.append({
                "code": code, "name": code, "price": float(bar["close"]),
                "close": float(bar["close"]), "change_pct": float(bar["change_pct"]),
                "amount": float(bar["amount"]), "volume": float(bar["volume"]),
                "volume_ratio": round(vol_ratio, 2),
                "high": float(bar["high"]), "low": float(bar["low"]),
                "open": float(bar["open"]), "pre_close": float(bar["pre_close"]),
                "amplitude": float(bar["amplitude"]),
                # 盘中最高涨幅（近似盘中逼近封板/追涨触发时点，回测逼近封板用）
                "high_chg": round((float(bar["high"]) - float(bar["pre_close"])) / float(bar["pre_close"]) * 100, 2)
                if float(bar["pre_close"]) > 0 else 0.0,
            })
        return pd.DataFrame(rows)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="全市场日线 ETL")
    ap.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    ap.add_argument("--workers", type=int, default=None, help="并行线程数")
    ap.add_argument("--force", action="store_true", help="忽略断点续传，全量重拉")
    args = ap.parse_args()
    r = KlineEtl.run(args.start, args.end, args.workers, args.force)
    print(r)
