import time
import datetime
import functools
import logging
from typing import Callable, Any, Optional, List, Dict, Tuple
import pandas as pd
import akshare as ak

from config.settings import settings

logger = logging.getLogger(__name__)


def retry_on_exception(retries: int = 3, delay: float = 2.0, backoff: float = 1.5):
    """
    网络请求重试装饰器,支持指数退避
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
                        logger.error(f"接口 {func.__name__} 达到最大重试次数 {retries},抛出异常.")
                        raise e
                    time.sleep(current_delay)
                    current_delay *= backoff
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# 多数据源降级工具
# 按优先级依次尝试不同数据源,某个源失败时自动切换下一个.
# 所有源都失败则返回空 DataFrame.
# ---------------------------------------------------------------------------

# 数据源优先级:新浪 -> 腾讯 -> 东财
SOURCE_PRIORITY = ["新浪", "腾讯", "东财"]


def multi_source_fetch(source_chain: List[Tuple[str, Callable[[], pd.DataFrame]]]) -> pd.DataFrame:
    """
    按优先级依次尝试多个数据源获取数据.

    :param source_chain: [(source_name, fetch_func), ...] 列表,
                         优先级从高到低排列.
    :return: 第一个成功的数据源返回的 DataFrame；全部失败则返回空 DataFrame.

    用法示例:
        df = multi_source_fetch([
            ("新浪", lambda: fetch_from_sina()),
            ("腾讯", lambda: fetch_from_tencent()),
            ("东财", lambda: fetch_from_eastmoney()),
        ])
    """
    for i, (source_name, fetch_func) in enumerate(source_chain):
        try:
            logger.info(f"尝试从 [{source_name}] 获取数据...")
            df = fetch_func()
            if df is not None and not df.empty:
                logger.info(f"[{source_name}] 数据获取成功,共 {len(df)} 条")
                return df
            else:
                logger.warning(f"[{source_name}] 返回空数据,尝试下一个数据源...")
        except Exception as e:
            logger.warning(f"[{source_name}] 获取失败: {e},尝试下一个数据源...")

    logger.error(f"所有数据源 ({', '.join(s[0] for s in source_chain)}) 均获取失败！")
    return pd.DataFrame()


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

    # ──────────────────────────────────────────────
    # 昨日涨停溢价（市场温度计）
    # ──────────────────────────────────────────────

    _last_premium: float = 1.5  # 上次成功的溢价,API失败时兜底

    @staticmethod
    def get_yesterday_zt_premium() -> dict:
        """
        获取昨日涨停股今日表现,计算竞价溢价和即时溢价.
        API 失败时用上次成功值兜底,默认 1.5%（中性溢价）.
        """
        import datetime
        try:
            today = datetime.datetime.now().strftime("%Y%m%d")
            df = ak.stock_zt_pool_previous_em(date=today)
            if df is None or df.empty:
                return {"opening_premium": DataFetcher._last_premium,
                        "intraday_premium": DataFetcher._last_premium,
                        "high_open_ratio": 0, "positive_ratio": 0,
                        "total_count": 0, "source": f"无数据(兜底{DataFetcher._last_premium}%)"}
            changes = pd.to_numeric(df["涨跌幅"], errors="coerce").dropna()
            total = len(changes)
            if total == 0:
                return {"opening_premium": DataFetcher._last_premium,
                        "intraday_premium": DataFetcher._last_premium,
                        "high_open_ratio": 0, "positive_ratio": 0,
                        "total_count": 0, "source": f"数据为空(兜底{DataFetcher._last_premium}%)"}
            # 即时溢价:当前涨跌幅均值（盘中会变）,成功后缓存
            intraday_premium = round(float(changes.mean()), 2)
            DataFetcher._last_premium = intraday_premium
            # 红盘率:当前涨幅>0的比例（盘中会变）
            positive_ratio = round((changes > 0).sum() / total * 100, 2)
            # 开盘溢价:9:35 前首次有效调用时缓存（此时涨跌幅≈开盘涨幅）
            cache_key = f"_open_premium_{today}"
            now = datetime.datetime.now()
            is_early = now.hour == 9 and now.minute <= 30  # 9:30 前算开盘窗口
            if not hasattr(DataFetcher, cache_key) and total > 0:
                if is_early:
                    setattr(DataFetcher, cache_key, ("open", intraday_premium,
                        round((changes > 3).sum() / total * 100, 2)))
                else:
                    setattr(DataFetcher, cache_key, ("snapshot", intraday_premium,
                        round((changes > 3).sum() / total * 100, 2)))
            if hasattr(DataFetcher, cache_key):
                tag, opening_premium, high_open_ratio = getattr(DataFetcher, cache_key)
            else:
                tag, opening_premium, high_open_ratio = "snapshot", intraday_premium, round((changes > 3).sum() / total * 100, 2)
            logger.info(
                f"昨日涨停溢价: 开盘{opening_premium}% | 即时{intraday_premium}% | "
                f"高开>3%占比{high_open_ratio}% | 红盘率{positive_ratio}% | 样本{total}只"
            )
            premium_tag = "开盘" if tag == "open" else "盘中快照(非开盘时段启动)"
            return {
                "opening_premium": opening_premium,
                "intraday_premium": intraday_premium,
                "high_open_ratio": high_open_ratio,
                "positive_ratio": positive_ratio,
                "total_count": total,
                "premium_tag": premium_tag,
                "source": "stock_zt_pool_previous_em",
            }
        except Exception as e:
            logger.warning(f"获取昨日涨停溢价失败: {e},使用上次缓存值{DataFetcher._last_premium}%")
            return {"opening_premium": DataFetcher._last_premium,
                    "intraday_premium": DataFetcher._last_premium,
                    "high_open_ratio": 0, "positive_ratio": 0,
                    "total_count": 0, "source": f"异常兜底{DataFetcher._last_premium}%"}

    @staticmethod
    def get_adaptive_baseline() -> dict:
        """
        获取流动性基准: 优先昨日全市场真实成交额, 不可用时回退 20 日均线.

        :return: {"ma_amount": 基准成交额(亿元), "source": "昨日全市场/指数合成/默认"}
        """
        import datetime
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        # 1. 优先从数据库取昨日全市场真实成交额
        try:
            from database.services import db_manager
            from database.models import DailySentiment
            session = db_manager.get_session()
            try:
                row = session.query(DailySentiment).filter(
                    DailySentiment.trade_date == yesterday
                ).first()
                if row and getattr(row, "total_amount", 0) > 0:
                    return {"ma_amount": row.total_amount, "source": "昨日全市场"}
            finally:
                session.close()
        except Exception:
            pass
        # 2. 回退:20日均线合成
        try:
            indices = ["sh000001", "sz399001", "sz399006"]
            all_amounts = []
            for sym in indices:
                try:
                    df = ak.stock_zh_index_daily(symbol=sym)
                    if df is not None and not df.empty:
                        vals = pd.to_numeric(df["volume"], errors="coerce").dropna().tail(20)
                        if len(vals) >= 5:
                            all_amounts.append(vals)
                except Exception:
                    pass
            if all_amounts:
                combined = sum(all_amounts)
                ma = round(float(combined.mean()) / 1e8, 0)
                if ma > 1000:
                    return {"ma_amount": ma, "source": "20日均线合成"}
        except Exception as e:
            logger.warning(f"指数合成失败: {e}")
        return {"ma_amount": 8000, "source": "默认8000亿"}

    @staticmethod
    def estimate_today_amount(spot_total_amount: float, now=None) -> dict:
        """Estimate full-day turnover using intraday volume distribution coefficients.
        9:30-9:45=15%, 10:00=30%, 10:30=45%, 11:30=60%, 14:00=80%, 15:00=100%."""

        import datetime
        if now is None:
            now = datetime.datetime.now()
        current_minutes = now.hour * 60 + now.minute
        market_open = 9 * 60 + 30  # 570 min

        if current_minutes < market_open:
            return {"estimated": 0, "ratio": 0, "now_amount": 0, "message": "未开盘"}

        minutes_elapsed = current_minutes - market_open
        # 午休扣除 11:30~13:00 (90 min)
        if current_minutes > 11 * 60 + 30:
            minutes_elapsed -= 90
        if minutes_elapsed < 0:
            minutes_elapsed = 0

        # 分时成交占比（非线性:早盘和尾盘成交密集,午盘稀疏）
        # 9:30~09:45=15%  9:30~10:00=30%  9:30~10:30=45%
        # 9:30~11:30=60%  9:30~14:00=80%  14:00~15:00=20%
        time_points = [
            (15, 0.15), (30, 0.30), (60, 0.45),
            (120, 0.60), (150, 0.66), (180, 0.72),
            (210, 0.80), (225, 0.90), (240, 1.00),
        ]
        ratio = 0.01
        for elapsed, coeff in time_points:
            if minutes_elapsed <= elapsed:
                # 在相邻节点间线性插值
                prev_elapsed = (0, 0.0) if elapsed == 15 else \
                    time_points[time_points.index((elapsed, coeff)) - 1]
                frac = (minutes_elapsed - prev_elapsed[0]) / (elapsed - prev_elapsed[0])
                ratio = prev_elapsed[1] + frac * (coeff - prev_elapsed[1])
                break
        else:
            ratio = 0.98
        if ratio < 0.01:
            ratio = 0.01

        now_amount = spot_total_amount / 1e8  # 转亿元
        estimated = now_amount / ratio
        # 合理范围:A 股日均 5000~15000 亿,极端不超过 30000 亿
        if estimated > 30000:
            estimated = 30000
        elif estimated < 500 and ratio < 0.03:
            # 开盘 2 分钟内比例太低不准,下调到合理范围
            estimated = now_amount * 20  # 用 5% 底限比率兜底
        return {
            "estimated": round(estimated, 0),
            "ratio": round(ratio, 2),
            "now_amount": round(now_amount, 0),
        }

    @staticmethod
    def get_stock_ma_prices(code: str, lookback: int = 30) -> dict:
        """
        获取个股历史日K线数据并计算关键均线 (MA5/MA10/MA20).
        多源降级:新浪 -> 东财.
        :param code: 股票代码 (6位)
        :param lookback: 回溯天数
        """
        def _from_sina():
            prefix = DataFetcher._market_prefix(code)
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
                closes = pd.to_numeric(df["close"], errors="coerce").dropna().tolist()
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
        :param code: 股票代码,如 000001
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

    # ──────────────────────────────────────────────
    # 实时行情快照（多数据源降级:新浪 -> 腾讯 -> 东财）
    # ──────────────────────────────────────────────

    @staticmethod
    def _fetch_spot_sina() -> pd.DataFrame:
        """从新浪获取全市场实时行情并归一化列名"""
        df = ak.stock_zh_a_spot()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "代码": "code", "名称": "name",
            "最新价": "price", "涨跌幅": "change_pct", "涨跌额": "change_amount",
            "昨收": "pre_close", "今开": "open", "最高": "high", "最低": "low",
            "成交量": "volume", "成交额": "amount",
        })
        # 新浪 code 带前缀 (sh600519 / sz000001 / bj920000),去掉前缀
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
        # 删除不需要的列,避免干扰下游
        for drop_col in ["买入", "卖出", "时间戳"]:
            if drop_col in df.columns:
                df.drop(columns=drop_col, inplace=True)
        # 新浪缺失字段用默认值填充
        for col, default in [("volume_ratio", 1.0), ("turnover_rate", 0.0),
                              ("amplitude", 0.0), ("total_market_cap", 0.0),
                              ("circ_market_cap", 0.0), ("pe_ttm", 0.0), ("pb", 0.0)]:
            if col not in df.columns:
                df[col] = default
        return df

    @staticmethod
    def _fetch_spot_tencent() -> pd.DataFrame:
        """从腾讯获取全市场实时行情并归一化列名"""
        df = ak.stock_zh_a_spot_tx()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "name": "name",
            "zxj": "price", "zdf": "change_pct", "zd": "change_amount",
            "zf": "amplitude", "hsl": "turnover_rate", "lb": "volume_ratio",
            "volume": "volume", "turnover": "amount",
            "ltsz": "circ_market_cap", "zsz": "total_market_cap",
            "pe_ttm": "pe_ttm",
        })
        # 腾讯 code 带前缀 (sh600519 / sz000001 / bj920000),去掉前缀
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
        # pre_close 从 最新价 - 涨跌额 反推
        df["pre_close"] = df["price"] - df["change_amount"]
        # open/high/low 腾讯不提供,默认值
        for col, default in [("open", 0.0), ("high", 0.0), ("low", 0.0), ("pb", 0.0)]:
            if col not in df.columns:
                df[col] = default
        return df

    @staticmethod
    def _fetch_spot_eastmoney() -> pd.DataFrame:
        """从东财获取全市场实时行情并归一化列名（原始主力源）"""
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "代码": "code", "名称": "name",
            "最新价": "price", "涨跌幅": "change_pct", "涨跌额": "change_amount",
            "成交量": "volume", "成交额": "amount", "振幅": "amplitude",
            "最高": "high", "最低": "low", "今开": "open", "昨收": "pre_close",
            "量比": "volume_ratio", "换手率": "turnover_rate",
            "市盈率-动态": "pe_ttm", "市净率": "pb",
            "总市值": "total_market_cap", "流通市值": "circ_market_cap",
        })
        return df

    _cached_total_amount: float = 0.0

    @classmethod
    def get_market_total_amount(cls) -> float:
        """获取全市场实时总成交额（元）,无过滤.由 get_realtime_spot() 在过滤前写入缓存."""
        return cls._cached_total_amount

    @staticmethod
    def get_realtime_spot() -> pd.DataFrame:
        """
        获取全市场 A 股实时行情快照.
        多数据源降级:新浪 -> 腾讯 -> 东财,某个源失败自动切换下一个.
        """
        df = multi_source_fetch([
            ("新浪", DataFetcher._fetch_spot_sina),
            ("腾讯", DataFetcher._fetch_spot_tencent),
            ("东财", DataFetcher._fetch_spot_eastmoney),
        ])
        if df.empty:
            logger.warning("获取全市场实时行情为空（所有数据源均失败）.")
            return pd.DataFrame()
        # 过滤前缓存全量总成交额（含科创板/北交所/ST,对齐券商软件）
        if "amount" in df.columns:
            DataFetcher._cached_total_amount = float(df["amount"].sum())
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
        return df if df is not None else pd.DataFrame()

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_zhaban_pool(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的炸板观察池 (stock_zt_pool_zbgc_em)
        注:旧版 stock_zt_pool_zhaban_em 已废弃,新版 zbgc 合并了涨停+炸板信息
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
        return df if df is not None else pd.DataFrame()

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
        return df if df is not None else pd.DataFrame()

    @staticmethod
    def get_lhb_detail(date_str: str) -> pd.DataFrame:
        """
        获取指定日期的龙虎榜个股明细 (stock_lhb_detail_em)
        注:新版 akshare 返回的是个股级聚合数据（龙虎榜净买额/买入额/卖出额）,
        不再包含营业部名称.营业部级数据请使用 get_lhb_seats().
        异常时返回空 DataFrame,不抛异常.
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
        异常时返回空 DataFrame,不抛异常.
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
        注:新版 akshare 不再返回「总市值」字段,中军池改用成交额单维度筛选.
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
            # 新版 API 不含总市值,设为 0 兜底
            if "total_market_cap" not in df.columns:
                df["total_market_cap"] = 0
        return DataFetcher.filter_stocks(df) if df is not None else pd.DataFrame()

    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_individual_fund_flow(stock_code: str = "600519", market: str = "sh") -> pd.DataFrame:
        """
        获取个股主力资金流向 (stock_individual_fund_flow)
        注:新版 akshare 此接口返回单只个股的历史资金流向,不再支持「即时」全市场排名.
        stock_code: 沪市个股代码 (如 600519)
        market: 'sh' (沪市) 或 'sz' (深市,部分版本可能不支持)
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

    # ──────────────────────────────────────────────
    # 分时数据（多数据源降级:新浪 -> 腾讯 -> 东财）
    # ──────────────────────────────────────────────

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
        symbol = f"{DataFetcher._market_prefix(code)}{code}"
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

    # ──────────────────────────────────────────────
    # 分时形态库（经典形态识别,不依赖 LLM）
    # ──────────────────────────────────────────────

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
