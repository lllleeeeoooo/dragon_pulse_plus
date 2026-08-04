import logging
import pandas as pd
from config.settings import settings
import akshare as ak
logger = logging.getLogger(__name__)
from typing import Dict, Any, List
from data.core import multi_source_fetch
from data.fetcher_spot import _SpotMixin
import datetime

class _HistoryMixin:
    @staticmethod
    def get_stock_daily_closes(code: str, lookback: int = 30) -> list:
        """
        获取个股近 N 日收盘价序列（多源降级:新浪 -> 东财），用于 Beta 相关性计算。
        :return: 收盘价 list（升序，最近在末尾），失败返回空列表
        """
        def _from_sina():
            prefix = _HistoryMixin._market_prefix(code)
            df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
            if df is not None and not df.empty:
                df = df.tail(lookback)
                closes = pd.to_numeric(df["close"], errors="coerce").dropna()
                if len(closes) >= 5:
                    return pd.DataFrame({"close": closes})
            return pd.DataFrame()

        def _from_eastmoney():
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="", end_date="", adjust="qfq")
            if df is not None and not df.empty:
                df = df.tail(lookback)
                close_col = "收盘" if "收盘" in df.columns else (df.columns[2] if len(df.columns) > 2 else df.columns[0])
                closes = pd.to_numeric(df[close_col], errors="coerce").dropna()
                if len(closes) >= 5:
                    return pd.DataFrame({"close": closes})
            return pd.DataFrame()

        try:
            df = multi_source_fetch([
                ("新浪", _from_sina),
                ("东财", _from_eastmoney),
            ])
            if not df.empty:
                return pd.to_numeric(df["close"], errors="coerce").dropna().tolist()
        except Exception as e:
            logger.warning(f"获取 {code} 历史收盘价失败: {e}")
        return []

    @staticmethod
    def get_stock_ma_prices(code: str, lookback: int = 30) -> dict:
        """
        获取个股历史日K线数据并计算关键均线 (MA5/MA10/MA20).
        多源降级:新浪 -> 东财.
        :param code: 股票代码 (6位)
        :param lookback: 回溯天数
        """
        closes = _HistoryMixin.get_stock_daily_closes(code, lookback)
        if len(closes) >= 5:
            return {
                "ma5": round(sum(closes[-5:]) / 5, 2),
                "ma10": round(sum(closes[-10:]) / min(10, len(closes)), 2) if len(closes) >= 10 else None,
                "ma20": round(sum(closes[-20:]) / min(20, len(closes)), 2) if len(closes) >= 20 else None,
            }
        return {"ma5": None, "ma10": None, "ma20": None}


    @staticmethod
    def get_stock_name(code: str) -> str:
        """
        根据股票代码自动匹配股票名称
        :param code: 股票代码,如 000001
        """
        try:
            spot_df = _SpotMixin.get_realtime_spot()
            if not spot_df.empty:
                match = spot_df[spot_df["code"] == str(code)]
                if not match.empty:
                    return str(match.iloc[0]["name"])
        except Exception as e:
            logger.warning(f"根据代码 {code} 自动匹配股票名称失败: {e}")
        return f"股票{code}"


    @staticmethod
    def _market_prefix(code: str) -> str:
        """根据 6 位股票代码返回市场前缀:sh(沪市) / sz(深市)"""
        code = str(code)
        if code.startswith(("6", "5", "9")):
            return "sh"
        return "sz"

    @staticmethod
    def _fetch_intraday_5min_sina(code: str) -> pd.DataFrame:
        """新浪 5 分钟 K 线 -> 归一化为 [time, open, high, low, close, volume, change_pct]"""
        # 新浪分钟线需要市场前缀 sh/sz
        symbol = f"{_HistoryMixin._market_prefix(code)}{code}"
        df = ak.stock_zh_a_minute(symbol=symbol, period="5")
        if df is None or df.empty:
            return pd.DataFrame()
        # 新浪分钟线列名:day, open, high, low, close, volume
        col_map = {}
        for raw, target in [("day", "time"), ("open", "open"), ("high", "high"),
                            ("low", "low"), ("close", "close"), ("volume", "volume")]:
            if raw in df.columns:
                col_map[raw] = target
        if col_map:
            df = df.rename(columns=col_map)
        if "change_pct" not in df.columns and "close" in df.columns:
            closes = pd.to_numeric(df["close"], errors="coerce")
            df["change_pct"] = closes.pct_change().fillna(0) * 100
        return df


    @staticmethod
    def _fetch_intraday_5min_tencent(code: str) -> pd.DataFrame:
        """腾讯历史 K 线 -> 归一化（腾讯无独立分时接口,用日线兜底返回空）"""
        # 腾讯目前没有通过 akshare 提供的 5 分钟线接口,返回空让下一个源接管
        return pd.DataFrame()


    @staticmethod
    def _fetch_intraday_5min_eastmoney(code: str) -> pd.DataFrame:
        """东财 5 分钟 K 线 -> 归一化为 [time, open, high, low, close, volume, change_pct]"""
        df = ak.stock_zh_a_hist_min_em(symbol=code, period="5", adjust="")
        if df is None or df.empty:
            return pd.DataFrame()
        col_map = {}
        for raw, target in [("时间", "time"), ("开盘", "open"), ("收盘", "close"),
                            ("最高", "high"), ("最低", "low"), ("成交量", "volume"),
                            ("涨跌幅", "change_pct")]:
            if raw in df.columns:
                col_map[raw] = target
        if col_map:
            df = df.rename(columns=col_map)
        return df


    @classmethod
    def _fetch_intraday_5min(cls, code: str) -> pd.DataFrame:
        """多源获取个股 5 分钟 K 线,优先 新浪 -> 腾讯 -> 东财.只保留今日数据,按时间升序."""
        df = multi_source_fetch([
            ("新浪", lambda: cls._fetch_intraday_5min_sina(code)),
            ("腾讯", lambda: cls._fetch_intraday_5min_tencent(code)),
            ("东财", lambda: cls._fetch_intraday_5min_eastmoney(code)),
        ])
        if df.empty:
            return df
        # 只取今日
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        time_col = "time" if "time" in df.columns else df.columns[0]
        df = df[df[time_col].astype(str).str.startswith(today_str)]
        # 按时间升序
        df = df.sort_values(by=time_col).reset_index(drop=True)
        return df


    @classmethod
    def get_intraday_vwap(cls, code: str) -> float:
        """
        获取个股今日分时成交量加权均价 (VWAP).
        基于多源 5 分钟 K 线计算:∑(close × volume) / ∑volume
        """
        try:
            df = cls._fetch_intraday_5min(code)
            if df.empty:
                return 0.0
            close_col = "close" if "close" in df.columns else df.columns[2]
            vol_col = "volume" if "volume" in df.columns else df.columns[5]
            closes = pd.to_numeric(df[close_col], errors="coerce")
            vols = pd.to_numeric(df[vol_col], errors="coerce").fillna(0)
            total_value = (closes * vols).sum()
            total_volume = vols.sum()
            if total_volume > 0:
                return round(float(total_value / total_volume), 2)
        except Exception as e:
            logger.warning(f"计算 {code} 分时 VWAP 失败: {e}")
        return 0.0


    @classmethod
    def get_intraday_pattern(cls, code: str) -> str:
        # 获取个股今日分时走势形态描述,基于多源 5 分钟 K 线
        try:
            df = cls._fetch_intraday_5min(code)
            if df is None or df.empty:
                return "无分时数据"

            time_col = "time" if "time" in df.columns else df.columns[0]
            close_col = "close" if "close" in df.columns else df.columns[2]
            high_col = "high" if "high" in df.columns else df.columns[3]
            change_col = "change_pct" if "change_pct" in df.columns else df.columns[5]
            vol_col = "volume" if "volume" in df.columns else df.columns[7]

            # 筛选今日交易时段
            today_rows = df[df[time_col].astype(str).str.contains("09:|10:|11:|13:|14:|15:")]
            if today_rows.empty:
                today_rows = df.tail(50)

            closes = pd.to_numeric(today_rows[close_col], errors="coerce").dropna()
            changes = pd.to_numeric(today_rows[change_col], errors="coerce").dropna()
            vols = pd.to_numeric(today_rows[vol_col], errors="coerce").dropna()
            highs = pd.to_numeric(today_rows[high_col], errors="coerce").dropna()
            times = today_rows[time_col].tolist()

            if closes.empty:
                return "分时数据为空"

            open_price = float(closes.iloc[0])
            latest_price = float(closes.iloc[-1])
            day_high = float(highs.max())
            total_change = round(float(changes.iloc[-1]), 2)

            parts = []

            # 涨停检测（按板块区分涨停线）
            limit_line = 19.5 if str(code).startswith(("30", "688")) else 9.5
            if total_change >= limit_line:
                limit_idx = None
                for i, chg in enumerate(changes):
                    if float(chg) >= limit_line:
                        limit_idx = i
                        break
                if limit_idx is not None and limit_idx < len(times):
                    t = str(times[limit_idx])
                    seal_time = t[-8:-3] if len(t) >= 8 else t
                    parts.append(f"封板{seal_time}")
                else:
                    parts.append("涨停")

                broke = any(float(c) < 7.0 for c in changes)
                parts.append("炸板后回封" if broke else "未炸板")

                if len(vols) >= 6:
                    mid = len(vols) // 2
                    early_v = float(vols.iloc[:mid].mean())
                    late_v = float(vols.iloc[mid:].mean())
                    if late_v < early_v * 0.5:
                        parts.append("尾盘缩量封稳")
                    elif late_v > early_v * 2:
                        parts.append("尾盘放量分歧")
            elif total_change >= 5.0:
                parts.append("中阳拉升")
            elif total_change >= 0:
                parts.append("横盘震荡")
            elif total_change < -3.0:
                parts.append(f"下跌{total_change}%")
            else:
                parts.append("小幅收跌")

            if day_high > open_price * 1.05 and latest_price < day_high * 0.97:
                parts.append("冲高回落")

            if len(changes) >= 6:
                tail = float(changes.iloc[-1]) - float(changes.iloc[-6])
                if tail > 3:
                    parts.append("尾盘急拉")
                elif tail < -3:
                    parts.append("尾盘跳水")

            return " ".join(parts) if parts else "平稳走势"
        except Exception as e:
            logger.warning(f"提取 {code} 分时形态失败: {e}")
            return "分时数据异常"


    @classmethod
    def detect_intraday_patterns(cls, code: str) -> List[str]:
        """
        基于 5 分钟 K 线识别经典分时形态,返回形态标签列表.
        不需要 LLM,纯规则判断.
        """
        df = cls._fetch_intraday_5min(code)
        if df.empty:
            return []
        patterns = []

        close_col = "close" if "close" in df.columns else df.columns[2]
        high_col = "high" if "high" in df.columns else df.columns[3]
        low_col = "low" if "low" in df.columns else df.columns[4]
        vol_col = "volume" if "volume" in df.columns else df.columns[5]
        time_col = "time" if "time" in df.columns else df.columns[0]

        closes = pd.to_numeric(df[close_col], errors="coerce")
        highs = pd.to_numeric(df[high_col], errors="coerce")
        lows = pd.to_numeric(df[low_col], errors="coerce")
        vols = pd.to_numeric(df[vol_col], errors="coerce").fillna(0)

        if len(closes) < 6:
            return patterns

        n = len(closes)
        open_p = closes.iloc[0]
        curr_p = closes.iloc[-1]
        day_high = highs.max()
        day_low = lows.min()
        day_change = round((curr_p - open_p) / open_p * 100, 2) if open_p > 0 else 0

        # 1. 冲高回落:日内最高涨超 5%,收盘回落至涨幅 2% 以内
        high_pct = (day_high - open_p) / open_p * 100 if open_p > 0 else 0
        if high_pct >= 5 and day_change <= 2 and curr_p < day_high * 0.97:
            patterns.append("冲高回落")

        # 2. 尾盘偷袭:最后 6 根 K 线（30分钟）成交量是前段 2 倍以上,且价格拉升 >1%
        if n >= 12:
            early_vol = vols.iloc[:n - 6].mean()
            late_vol = vols.iloc[-6:].mean()
            late_change = (closes.iloc[-1] - closes.iloc[-7]) / closes.iloc[-7] * 100
            if late_vol > early_vol * 2 and late_change > 1:
                patterns.append("尾盘偷袭")
            elif late_vol > early_vol * 2 and late_change < -1:
                patterns.append("尾盘砸盘")

        # 3. 早盘抢筹:前 6 根 K 线成交量占全天 50% 以上,涨幅 >3%
        if n >= 12:
            early_vol_sum = vols.iloc[:6].sum()
            total_vol = vols.sum()
            early_change = (closes.iloc[5] - open_p) / open_p * 100
            if total_vol > 0 and early_vol_sum / total_vol > 0.5 and early_change > 3:
                patterns.append("早盘抢筹")

        # 4. 横盘蓄力:中间 60% 的 K 线价格波动 <1%,然后突破
        if n >= 15:
            mid_start = n // 5
            mid_end = n * 4 // 5
            mid_close = closes.iloc[mid_start:mid_end]
            mid_high = highs.iloc[mid_start:mid_end]
            mid_low = lows.iloc[mid_start:mid_end]
            mid_range = (mid_high.max() - mid_low.min()) / mid_close.mean() * 100
            breakout = (closes.iloc[-1] - closes.iloc[mid_end]) / closes.iloc[mid_end] * 100
            if mid_range < 1.5 and breakout > 2:
                patterns.append("横盘突破")

        # 5. 出货放量:价格横盘或微跌,成交量持续放大
        if n >= 8:
            last_half_vol = vols.iloc[-n // 2:].mean()
            first_half_vol = vols.iloc[:n // 2].mean()
            if last_half_vol > first_half_vol * 1.5 and abs(day_change) < 1.5:
                patterns.append("放量滞涨")
            elif last_half_vol > first_half_vol * 1.5 and day_change < -2:
                patterns.append("放量杀跌")

        # 6. 地天板/天地板:日内振幅超 15%
        amplitude = (day_high - day_low) / open_p * 100 if open_p > 0 else 0
        if amplitude > 15 and curr_p > open_p * 1.05:
            patterns.append("地天板")
        elif amplitude > 15 and curr_p < open_p * 0.95:
            patterns.append("天地板")

        return patterns if patterns else ["平稳走势"]

# Add trailing newline to avoid encoding issues

