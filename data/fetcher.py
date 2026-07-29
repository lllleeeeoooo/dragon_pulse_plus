import time
import functools
import logging
from typing import Callable, Any, Optional, List, Dict
import pandas as pd
import akshare as ak

from config.settings import settings

logger = logging.getLogger(__name__)


def retry_on_exception(retries: int = 3, delay: float = 2.0, backoff: float = 1.5):
    """
    网络请求重试装饰器，支持指数退避
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"接口 {func.__name__} 调用失败 (第 {attempt}/{retries} 次): {e}")
                    if attempt == retries:
                        logger.error(f"接口 {func.__name__} 达到最大重试次数 {retries}，抛出异常。")
                        raise e
                    time.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator


class DataFetcher:
    """
    AkShare 数据抓取二次封装类
    带自动重试、字段标准化与异常兜底
    """

    @staticmethod
    def filter_stocks(df: pd.DataFrame) -> pd.DataFrame:
        """
        根据全局配置过滤科创板、北交所及 ST 股票
        :param df: 包含 'code' 和 'name' 列的 DataFrame
        """
        if df is None or df.empty:
            return pd.DataFrame()

        filtered_df = df.copy()

        # 1. 过滤科创板 (代码 688 开头)
        if settings.EXCLUDE_STAR_MARKET and "code" in filtered_df.columns:
            filtered_df = filtered_df[~filtered_df["code"].astype(str).str.startswith("688")]

        # 2. 过滤北交所 (代码 8开头, 43开头, 83开头, 87开头)
        if settings.EXCLUDE_BSE and "code" in filtered_df.columns:
            bse_prefixes = ("82", "83", "87", "88", "43", "920")
            bse_mask = filtered_df["code"].astype(str).str.startswith(bse_prefixes)
            filtered_df = filtered_df[~bse_mask]

        # 3. 过滤 ST / *ST 股票
        if settings.EXCLUDE_ST and "name" in filtered_df.columns:
            st_mask = filtered_df["name"].astype(str).str.contains("ST", case=False, na=False)
            filtered_df = filtered_df[~st_mask]

        return filtered_df

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_stock_ma_prices(code: str, lookback: int = 30) -> dict:
        """
        获取个股历史日K线数据并计算关键均线 (MA5/MA10/MA20)
        :param code: 股票代码
        :param lookback: 回溯天数
        :return: { "ma5": float, "ma10": float, "ma20": float, "close_history": [...] }
        """
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="", end_date="", adjust="qfq")
            if df is not None and not df.empty:
                df = df.tail(lookback)
                closes = pd.to_numeric(df["收盘"], errors="coerce").dropna().tolist()
                if len(closes) >= 5:
                    return {
                        "ma5": round(sum(closes[-5:]) / 5, 2),
                        "ma10": round(sum(closes[-10:]) / min(10, len(closes)), 2) if len(closes) >= 10 else None,
                        "ma20": round(sum(closes[-20:]) / min(20, len(closes)), 2) if len(closes) >= 20 else None,
                    }
        except Exception as e:
            logger.warning(f"获取 {code} 历史K线计算均线失败: {e}")
        return {"ma5": None, "ma10": None, "ma20": None}

    @staticmethod
    def get_stock_name(code: str) -> str:
        """
        根据股票代码自动匹配股票名称
        :param code: 股票代码，如 000001
        """
        try:
            spot_df = DataFetcher.get_realtime_spot()
            if not spot_df.empty:
                match = spot_df[spot_df["code"] == str(code)]
                if not match.empty:
                    return str(match.iloc[0]["name"])
        except Exception as e:
            logger.warning(f"根据代码 {code} 自动匹配股票名称失败: {e}")
        return f"股票{code}"

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_realtime_spot() -> pd.DataFrame:
        """
        获取全市场 A 股实时行情快照 (stock_zh_a_spot_em)
        返回包含代码、名称、最新价、涨跌幅、成交量、成交额、换手率等的 DataFrame
        """
        df = ak.stock_zh_a_spot_em()
        if df.empty:
            logger.warning("获取全市场实时行情为空。")
            return pd.DataFrame()

        # 字段标准化重命名
        rename_dict = {
            "代码": "code",
            "名称": "name",
            "最新价": "price",
            "涨跌幅": "change_pct",
            "涨跌额": "change_amount",
            "成交量": "volume",
            "成交额": "amount",
            "振幅": "amplitude",
            "最高": "high",
            "最低": "low",
            "今开": "open",
            "昨收": "pre_close",
            "量比": "volume_ratio",
            "换手率": "turnover_rate",
            "市盈率-动态": "pe_ttm",
            "市净率": "pb",
            "总市值": "total_market_cap",
            "流通市值": "circ_market_cap"
        }
        df = df.rename(columns=rename_dict)
        return DataFetcher.filter_stocks(df)

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_zt_pool(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的涨停池 (stock_zt_pool_em)
        date_str 格式: "YYYYMMDD"
        """
        df = ak.stock_zt_pool_em(date=date_str)
        if df is not None and not df.empty:
            rename_dict = {
                "代码": "code",
                "名称": "name",
                "涨跌幅": "change_pct",
                "最新价": "price",
                "成交额": "amount",
                "流通市值": "circ_market_cap",
                "总市值": "total_market_cap",
                "换手率": "turnover_rate",
                "封板资金": "seal_amount",
                "首次封板时间": "first_seal_time",
                "最后封板时间": "last_seal_time",
                "炸板次数": "open_count",
                "涨停统计": "zt_stats",
                "连板数": "lbc",
                "所属行业": "industry"
            }
            df = df.rename(columns=rename_dict)
        return DataFetcher.filter_stocks(df) if df is not None else pd.DataFrame()

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_zhaban_pool(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的炸板观察池 (stock_zt_pool_zbgc_em)
        注：旧版 stock_zt_pool_zhaban_em 已废弃，新版 zbgc 合并了涨停+炸板信息
        date_str 格式: "YYYYMMDD"
        """
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
        if df is not None and not df.empty:
            rename_dict = {
                "代码": "code",
                "名称": "name",
                "涨跌幅": "change_pct",
                "最新价": "price",
                "成交额": "amount",
                "涨停价": "limit_up_price",
                "首次封板时间": "first_seal_time",
                "炸板次数": "open_count",
                "涨停统计": "zt_stats",
                "所属行业": "industry",
                "换手率": "turnover_rate",
                "流通市值": "circ_market_cap",
                "总市值": "total_market_cap",
            }
            df = df.rename(columns=rename_dict)
        return DataFetcher.filter_stocks(df) if df is not None else pd.DataFrame()

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_dt_pool(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的跌停池 (stock_zt_pool_dtgc_em)
        """
        df = ak.stock_zt_pool_dtgc_em(date=date_str)
        if df is not None and not df.empty:
            rename_dict = {
                "代码": "code",
                "名称": "name",
                "涨跌幅": "change_pct",
                "最新价": "price",
                "成交额": "amount",
                "连续跌停": "dtc",
                "所属行业": "industry"
            }
            df = df.rename(columns=rename_dict)
        return DataFetcher.filter_stocks(df) if df is not None else pd.DataFrame()

    @staticmethod
    def get_lhb_detail(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的龙虎榜个股明细 (stock_lhb_detail_em)
        注：新版 akshare 返回的是个股级聚合数据（龙虎榜净买额/买入额/卖出额），
        不再包含营业部名称。营业部级数据请使用 get_lhb_seats()。
        异常时返回空 DataFrame，不抛异常。
        """
        try:
            df = ak.stock_lhb_detail_em(start_date=date_str, end_date=date_str)
        except Exception as e:
            logger.warning(f"获取龙虎榜个股明细失败 (日期: {date_str}): {e}")
            return pd.DataFrame()

        if df is not None and not df.empty:
            rename_dict = {
                "代码": "code",
                "名称": "name",
                "解读": "explanation",
                "收盘价": "price",
                "涨跌幅": "change_pct",
                "龙虎榜净买额": "net_amount",
                "龙虎榜买入额": "buy_amount",
                "龙虎榜卖出额": "sell_amount",
                "龙虎榜成交额": "lhb_amount",
                "换手率": "turnover_rate",
                "流通市值": "circ_market_cap",
                "上榜原因": "reason",
            }
            df = df.rename(columns=rename_dict)
        return DataFetcher.filter_stocks(df) if df is not None else pd.DataFrame()

    @staticmethod
    def get_lhb_seats(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的龙虎榜活跃营业部明细 (stock_lhb_hyyyb_em)
        异常时返回空 DataFrame，不抛异常。
        """
        try:
            df = ak.stock_lhb_hyyyb_em(start_date=date_str, end_date=date_str)
        except Exception as e:
            logger.warning(f"获取龙虎榜营业部数据失败 (日期: {date_str}): {e}")
            return pd.DataFrame()

        if df is not None and not df.empty:
            rename_dict = {
                "营业部名称": "seat_name",
                "营业部代码": "seat_code",
                "上榜日": "trade_date",
                "买入个股数": "buy_stock_count",
                "卖出个股数": "sell_stock_count",
                "买入总金额": "buy_amount",
                "卖出总金额": "sell_amount",
                "总买卖净额": "net_amount",
                "买入股票": "buy_stocks",
            }
            df = df.rename(columns=rename_dict)
        return df if df is not None else pd.DataFrame()

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_board_cons(board_name: str) -> pd.DataFrame:
        """
        获取指定同花顺/东财概念板块成分股 (stock_board_industry_cons_em)
        注：新版 akshare 不再返回「总市值」字段，中军池改用成交额单维度筛选。
        """
        df = ak.stock_board_industry_cons_em(symbol=board_name)
        if df is not None and not df.empty:
            rename_dict = {
                "代码": "code",
                "名称": "name",
                "最新价": "price",
                "涨跌幅": "change_pct",
                "成交额": "amount",
                "换手率": "turnover_rate",
                "量比": "volume_ratio",
                "市盈率-动态": "pe_ttm",
            }
            df = df.rename(columns=rename_dict)
            # 新版 API 不含总市值，设为 0 兜底
            if "total_market_cap" not in df.columns:
                df["total_market_cap"] = 0
        return DataFetcher.filter_stocks(df) if df is not None else pd.DataFrame()

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_individual_fund_flow(stock_code: str = "600519", market: str = "sh") -> pd.DataFrame:
        """
        获取个股主力资金流向 (stock_individual_fund_flow)
        注：新版 akshare 此接口返回单只个股的历史资金流向，不再支持「即时」全市场排名。
        stock_code: 沪市个股代码 (如 600519)
        market: 'sh' (沪市) 或 'sz' (深市，部分版本可能不支持)
        """
        df = ak.stock_individual_fund_flow(stock=stock_code, market=market)
        if df is not None and not df.empty:
            # 兼容不同版本的字段名
            rename_map = {}
            for raw, target in [("名称", "name"), ("代码", "code"), ("日期", "date"),
                                ("主力净流入-净额", "main_net_inflow"),
                                ("主力净流入-净占比", "main_net_ratio"),
                                ("超大单净流入-净额", "super_large_net"),
                                ("收盘价", "close"), ("涨跌幅", "change_pct")]:
                if raw in df.columns:
                    rename_map[raw] = target
            if rename_map:
                df = df.rename(columns=rename_map)
        return df if df is not None else pd.DataFrame()

    @staticmethod
    def get_intraday_vwap(code: str) -> float:
        """获取个股今日真实分时均价 (VWAP)，基于逐笔成交数据计算"""
        try:
            df = ak.stock_intraday_em(symbol=code)
            if df is not None and not df.empty:
                price_col = "价格" if "价格" in df.columns else df.columns[2]
                vol_col = "成交量" if "成交量" in df.columns else df.columns[1]
                df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
                df[vol_col] = pd.to_numeric(df[vol_col], errors="coerce").fillna(0)
                total_value = (df[price_col] * df[vol_col]).sum()
                total_volume = df[vol_col].sum()
                if total_volume > 0:
                    return round(total_value / total_volume, 2)
        except Exception as e:
            logger.warning(f"计算 {code} 分时 VWAP 失败: {e}")
        return 0.0

    @staticmethod
    def get_intraday_pattern(code: str) -> str:
        """获取个股今日分时走势形态描述，基于 5 分钟 K 线"""
        try:
            df = ak.stock_zh_a_hist_min_em(symbol=code, period="5", adjust="")
            if df is None or df.empty:
                return "无分时数据"

            time_col = "时间" if "时间" in df.columns else df.columns[0]
            close_col = "收盘" if "收盘" in df.columns else df.columns[2]
            high_col = "最高" if "最高" in df.columns else df.columns[3]
            change_col = "涨跌幅" if "涨跌幅" in df.columns else df.columns[5]
            vol_col = "成交量" if "成交量" in df.columns else df.columns[7]

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

            # 涨停检测
            if total_change >= 9.5:
                limit_idx = None
                for i, chg in enumerate(changes):
                    if float(chg) >= 9.5:
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
