import logging
import pandas as pd
from config.settings import settings
import akshare as ak
logger = logging.getLogger(__name__)
import time as _time
from data.core import multi_source_fetch

_cached_total_amount: float = 0.0

# 立案调查黑名单缓存（每小时刷新一次，避免高频查库）
_investigation_blacklist_cache: set = set()
_investigation_cache_time: float = 0.0

# 昨日涨停溢价兜底值（首次 API 调用失败时使用）
_last_premium_fallback: float = 1.5


def _get_investigation_blacklist() -> set:
    """获取立案调查股票代码黑名单，带 1 小时缓存"""
    global _investigation_blacklist_cache, _investigation_cache_time
    now = _time.time()
    if now - _investigation_cache_time > settings.INVESTIGATION_CACHE_SECONDS:
        try:
            from database import InvestigationManager
            _investigation_blacklist_cache = InvestigationManager.get_blacklist_codes()
            _investigation_cache_time = now
        except Exception:
            pass
    return _investigation_blacklist_cache


class _SpotMixin:
    @staticmethod
    def filter_stocks(df: pd.DataFrame) -> pd.DataFrame:
        """
        根据全局配置过滤科创板、北交所、ST 股票及立案调查标的
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

        # 4. 过滤立案调查/违规处罚标的
        blacklist = _get_investigation_blacklist()
        if blacklist and "code" in filtered_df.columns:
            filtered_df = filtered_df[~filtered_df["code"].astype(str).isin(blacklist)]

        return filtered_df


    @staticmethod
    def get_yesterday_zt_premium() -> dict:
        """
        获取昨日涨停股今日表现,计算竞价溢价和即时溢价.
        API 失败时用上次成功值兜底,默认 1.5%（中性溢价）.
        """
        global _last_premium_fallback
        import datetime
        try:
            today = datetime.datetime.now().strftime("%Y%m%d")
            df = ak.stock_zt_pool_previous_em(date=today)
            if df is None or df.empty:
                return {"opening_premium": _last_premium_fallback,
                        "intraday_premium": _last_premium_fallback,
                        "high_open_ratio": 0, "positive_ratio": 0,
                        "total_count": 0, "source": f"无数据(兜底{_last_premium_fallback}%)"}
            changes = pd.to_numeric(df["涨跌幅"], errors="coerce").dropna()
            total = len(changes)
            if total == 0:
                return {"opening_premium": _last_premium_fallback,
                        "intraday_premium": _last_premium_fallback,
                        "high_open_ratio": 0, "positive_ratio": 0,
                        "total_count": 0, "source": f"数据为空(兜底{_last_premium_fallback}%)"}
            # 即时溢价:当前涨跌幅均值（盘中会变）,成功后缓存
            intraday_premium = round(float(changes.mean()), 2)
            _last_premium_fallback = intraday_premium
            # 红盘率:当前涨幅>0的比例（盘中会变）
            positive_ratio = round((changes > 0).sum() / total * 100, 2)
            # 开盘溢价:9:35 前首次有效调用时缓存（此时涨跌幅≈开盘涨幅）
            cache_key = f"_open_premium_{today}"
            now = datetime.datetime.now()
            is_early = now.hour == 9 and now.minute <= 30  # 9:30 前算开盘窗口
            if not hasattr(_SpotMixin, cache_key) and total > 0:
                if is_early:
                    setattr(_SpotMixin, cache_key, ("open", intraday_premium,
                        round((changes > 3).sum() / total * 100, 2)))
                else:
                    setattr(_SpotMixin, cache_key, ("snapshot", intraday_premium,
                        round((changes > 3).sum() / total * 100, 2)))
            if hasattr(_SpotMixin, cache_key):
                tag, opening_premium, high_open_ratio = getattr(_SpotMixin, cache_key)
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
            logger.warning(f"获取昨日涨停溢价失败: {e},使用上次缓存值{_last_premium_fallback}%")
            return {"opening_premium": _last_premium_fallback,
                    "intraday_premium": _last_premium_fallback,
                    "high_open_ratio": 0, "positive_ratio": 0,
                    "total_count": 0, "source": f"异常兜底{_last_premium_fallback}%"}


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
        lunch_start = 11 * 60 + 30  # 690 min
        lunch_end = 13 * 60  # 780 min

        if current_minutes < market_open:
            return {"estimated": 0, "ratio": 0, "now_amount": 0, "message": "未开盘"}

        # 午休期间：上午盘已结束，占全天60%
        if lunch_start <= current_minutes < lunch_end:
            now_amount = spot_total_amount / 1e8
            ratio = 0.60
            estimated = now_amount / ratio
            if estimated > 30000:
                estimated = 30000
            return {
                "estimated": round(estimated, 0),
                "ratio": round(ratio, 2),
                "now_amount": round(now_amount, 0),
            }

        # 计算有效交易分钟数（扣除午休90分钟）
        if current_minutes >= lunch_end:
            minutes_elapsed = current_minutes - market_open - 90
        else:
            minutes_elapsed = current_minutes - market_open

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
        # 东财成交量单位为"手"，统一转换为"股"（与新浪一致）
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) * 100
        return df


    @classmethod
    def get_market_total_amount(cls) -> float:
        """获取全市场实时总成交额（元）,无过滤.由 get_realtime_spot() 在过滤前写入缓存."""
        return _cached_total_amount


    @staticmethod
    def get_realtime_spot() -> pd.DataFrame:
        """
        获取全市场 A 股实时行情快照.
        多数据源降级:新浪 -> 腾讯 -> 东财,某个源失败自动切换下一个.
        """
        df = multi_source_fetch([
            ("新浪", _SpotMixin._fetch_spot_sina),
            ("腾讯", _SpotMixin._fetch_spot_tencent),
            ("东财", _SpotMixin._fetch_spot_eastmoney),
        ])
        if df.empty:
            logger.warning("获取全市场实时行情为空（所有数据源均失败）.")
            return pd.DataFrame()
        # 过滤前缓存全量总成交额（含科创板/北交所/ST,对齐券商软件）
        global _cached_total_amount
        if "amount" in df.columns:
            _cached_total_amount = float(df["amount"].sum())
        return _SpotMixin.filter_stocks(df)


