import logging
import socket
import pandas as pd
from config.settings import settings
import akshare as ak
logger = logging.getLogger(__name__)
import time as _time
from data.core import multi_source_fetch, _FETCH_SOCKET_TIMEOUT


# 东财 clist 全市场行情多主机轮换池：东财按后端主机/IP 频率限流（实测连续 ~8 次请求即
# RemoteDisconnected），akshare 硬编码 82.push2 且分页需 ~59 次请求必被断。
# 这里轮换多个 push2 主机分摊请求，规避单个后端主机被限流。
_EM_CLIST_HOSTS = ["82.push2", "92.push2", "push2", "7.push2", "30.push2"]


def _spot_fetch(func):
    """给数据源抓取函数包 socket 超时：akshare 内部 requests 未传 timeout，
    数据源挂起会永久阻塞主循环（曾多次卡死盘中监控，含 _fill_ohlc_from_sina 直接调用路径）。
    超时抛 socket.timeout，由调用方/降级链处理。"""
    def wrapper(*args, **kwargs):
        _old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_FETCH_SOCKET_TIMEOUT)
        try:
            return func(*args, **kwargs)
        finally:
            socket.setdefaulttimeout(_old)
    return wrapper


def _parallel_fetch_pages(url: str, make_params, parse_rows, indices, workers: int = 10) -> list:
    """并行分页抓取：make_params(idx)->params；parse_rows(json)->rows。返回按 idx 排序合并的 rows。
    任一页失败抛异常（由调用方回退串行/akshare）。每页 socket 超时用 _FETCH_SOCKET_TIMEOUT 兜底。"""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(idx):
        r = requests.get(url, params=make_params(idx), timeout=_FETCH_SOCKET_TIMEOUT)
        r.raise_for_status()
        return idx, parse_rows(r.json())

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, i): i for i in indices}
        for f in as_completed(futs):
            results.append(f.result())
    results.sort(key=lambda x: x[0])
    rows = []
    for _, rs in results:
        rows.extend(rs)
    return rows


# ---- 并行抓取反爬熔断 ----
# 并行分页请求频率高（腾讯28页/新浪59页并发），新浪/腾讯反爬可能封 IP。连续失败达阈值后
# 自动切回串行（akshare），冷却期结束再重试并行——避免被限流时每轮仍白试并行。
_SPOT_PARALLEL_FAIL_LIMIT = 2        # 连续失败次数 → 熔断该源并行
_SPOT_PARALLEL_BLOCK_SECONDS = 600   # 熔断冷却(秒)，10 分钟后重试并行
_parallel_fail_count: dict = {}
_parallel_blocked_until: dict = {}


def _parallel_allowed(source: str) -> bool:
    """该源并行抓取是否允许（未处于反爬熔断冷却期）"""
    return _time.time() >= _parallel_blocked_until.get(source, 0)


def _parallel_ok(source: str):
    """并行抓取成功，清零连续失败计数"""
    _parallel_fail_count.pop(source, None)


def _parallel_fail(source: str):
    """并行抓取失败/返回空(疑似反爬限流)：连续失败达阈值则熔断该源并行，冷却后重试"""
    _parallel_fail_count[source] = _parallel_fail_count.get(source, 0) + 1
    if _parallel_fail_count[source] >= _SPOT_PARALLEL_FAIL_LIMIT:
        _parallel_blocked_until[source] = _time.time() + _SPOT_PARALLEL_BLOCK_SECONDS
        _parallel_fail_count[source] = 0
        logger.warning(f"[{source}] 并行抓取连续失败{_SPOT_PARALLEL_FAIL_LIMIT}次疑似触发反爬/限流，"
                       f"自动切换串行，{_SPOT_PARALLEL_BLOCK_SECONDS}s 后重试并行")


_cached_total_amount: float = 0.0

# 新浪 OHLC 补齐缓存（主源为腾讯时，避免每 15 秒轮询都全量拉新浪）
_sina_ohlc_cache: pd.DataFrame = None
_sina_ohlc_cache_time: float = 0.0
_OHLC_CACHE_SECONDS: float = 60.0

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
    def _compute_open_premium(codes) -> tuple:
        """从当日 spot 快照的 开盘价/昨收 计算昨日涨停股的真实开盘溢价与高开>3%占比。
        开盘价 09:30 后固定，盘中任何时刻启动都能算准（不再依赖 09:00-09:30 启动窗口）。"""
        try:
            spot = _SpotMixin.get_realtime_spot()
            if spot is None or spot.empty:
                return None, None
            opens = []
            for code in codes:
                m = spot[spot["code"].astype(str) == str(code)]
                if not m.empty:
                    r = m.iloc[0]
                    o = float(r.get("open", 0)); pre = float(r.get("pre_close", 0))
                    if o > 0 and pre > 0:
                        opens.append((o - pre) / pre * 100)
            if not opens:
                return None, None
            opening_premium = round(sum(opens) / len(opens), 2)
            high_open = round(sum(1 for x in opens if x > 3) / len(opens) * 100, 2)
            return opening_premium, high_open
        except Exception as e:
            logger.warning(f"计算真实开盘溢价失败: {e}")
            return None, None

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
            # 真实开盘溢价：从当日快照 open/昨收 计算（盘中任何时刻都准，不依赖 09:30 前启动）
            cache_key = f"_open_premium_{today}"
            if not hasattr(_SpotMixin, cache_key) and total > 0:
                true_open, true_high = _SpotMixin._compute_open_premium(df["代码"].astype(str).tolist())
                if true_open is not None:
                    setattr(_SpotMixin, cache_key, ("open", true_open, true_high))
                else:
                    # 快照不可用时退回即时值（标记为非开盘口径）
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
            premium_tag = "开盘" if tag == "open" else "盘中快照(非开盘口径)"
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
        # 用交易日而非自然日（周一应取周五），避免取到周末无数据
        from core.trade_calendar import get_previous_trading_day
        yesterday = get_previous_trading_day()
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
    def _normalize_sina_spot(df: pd.DataFrame) -> pd.DataFrame:
        """新浪 raw 中文列 → 统一英文列（fetch 后公共归一化）"""
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
    def _fetch_spot_sina_parallel(workers: int = 10) -> pd.DataFrame:
        """新浪全市场行情并行分页抓取（num=100 达 API 上限，串行 ~74 页 → 并行）。
        按字段名映射成与 akshare stock_zh_a_spot 同构的中文列（比位置映射稳健）。
        注意：新浪反爬严格，并行高频请求可能被临时封 IP——失败由调用方回退 akshare 串行。"""
        import re
        import requests
        url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        count_url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                     "Market_Center.getHQNodeStockCount?node=hs_a")
        page_size = 100  # 新浪 API 上限 100（akshare 默认 80，加大省 ~20% 请求）
        base = {"sort": "symbol", "asc": "1", "node": "hs_a", "symbol": "", "_s_r_a": "page",
                "num": str(page_size)}
        res = requests.get(count_url, timeout=_FETCH_SOCKET_TIMEOUT)
        res.raise_for_status()
        total_n = int(re.findall(r"\d+", res.text)[0])
        n_pages = (total_n + page_size - 1) // page_size

        def _make(idx):
            return {**base, "page": str(idx + 1)}

        def _parse(j):
            return j if isinstance(j, list) else []

        rows = _parallel_fetch_pages(url, _make, _parse, range(n_pages), workers)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        return pd.DataFrame({
            "代码": df["code"].astype(str),
            "名称": df["name"].astype(str),
            "最新价": pd.to_numeric(df["trade"], errors="coerce"),
            "涨跌额": pd.to_numeric(df["pricechange"], errors="coerce"),
            "涨跌幅": pd.to_numeric(df["changepercent"], errors="coerce"),
            "昨收": pd.to_numeric(df["settlement"], errors="coerce"),
            "今开": pd.to_numeric(df["open"], errors="coerce"),
            "最高": pd.to_numeric(df["high"], errors="coerce"),
            "最低": pd.to_numeric(df["low"], errors="coerce"),
            "成交量": pd.to_numeric(df["volume"], errors="coerce"),
            "成交额": pd.to_numeric(df["amount"], errors="coerce"),
        })

    @staticmethod
    @_spot_fetch
    def _fetch_spot_sina() -> pd.DataFrame:
        """从新浪获取全市场实时行情并归一化列名。
        SPOT_FETCH_PARALLEL=True 时并行分页抓取，False 或并行失败回退 akshare 串行。"""
        if settings.SPOT_FETCH_PARALLEL and _parallel_allowed("新浪"):
            try:
                df = _SpotMixin._fetch_spot_sina_parallel()
                if df is None or df.empty:
                    raise RuntimeError("并行抓取返回空(疑似被限流)")
                _parallel_ok("新浪")
                return _SpotMixin._normalize_sina_spot(df)
            except Exception as e:
                _parallel_fail("新浪")
                logger.warning(f"新浪并行抓取失败，切串行: {e}")
        return _SpotMixin._normalize_sina_spot(ak.stock_zh_a_spot())


    @staticmethod
    def _normalize_tencent_spot(df: pd.DataFrame) -> pd.DataFrame:
        """腾讯 raw rank_list 列 → 统一英文列（fetch 后公共归一化）"""
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
        # 腾讯返回的字段多为字符串（涨跌幅/量比等可能带 % 后缀），统一转数值，否则下游运算会崩
        for col in ["price", "change_pct", "change_amount", "amplitude", "turnover_rate",
                    "volume_ratio", "volume", "amount", "circ_market_cap", "total_market_cap", "pe_ttm"]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace("%", "", regex=False), errors="coerce")
        # 单位统一（实测验证）：腾讯 成交量=手(×100→股)、成交额=万元(×1e4→元)，
        # 流通/总市值=亿元(×1e8→元)，与新浪/东财口径一致（否则市值显示成 0 亿、市值阈值算错）
        if "volume" in df.columns:
            df["volume"] = df["volume"] * 100
        if "amount" in df.columns:
            df["amount"] = df["amount"] * 1e4
        for _col in ["circ_market_cap", "total_market_cap"]:
            if _col in df.columns:
                df[_col] = df[_col] * 1e8
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
    def _fetch_spot_tencent_parallel(workers: int = 10) -> pd.DataFrame:
        """腾讯全市场行情并行分页抓取（offset 独立寻址，串行 28 页 ~30s → 并行 ~13s）。
        返回与 akshare stock_zh_a_spot_tx 同构的原始 rank_list 列。"""
        import requests
        url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        page_size = 200  # 腾讯 API 硬上限 200（加大返回空）
        base = {"_appver": "11.17.0", "board_code": "aStock", "sort_type": "price",
                "direct": "down", "count": str(page_size)}
        r0 = requests.get(url, params={**base, "offset": "0"}, timeout=_FETCH_SOCKET_TIMEOUT)
        r0.raise_for_status()
        j0 = r0.json()
        total = int(j0["data"]["total"])
        n_pages = (total + page_size - 1) // page_size
        first_rows = j0["data"].get("rank_list", [])
        rest = []
        if n_pages > 1:
            def _make(idx):
                return {**base, "offset": str(idx * page_size)}

            def _parse(j):
                return j.get("data", {}).get("rank_list", [])

            rest = _parallel_fetch_pages(url, _make, _parse, range(1, n_pages), workers)
        return pd.DataFrame(first_rows + rest).drop_duplicates(subset=["code"], ignore_index=True)

    @staticmethod
    @_spot_fetch
    def _fetch_spot_tencent() -> pd.DataFrame:
        """从腾讯获取全市场实时行情并归一化列名。
        SPOT_FETCH_PARALLEL=True 时并行分页抓取，False 或并行失败回退 akshare 串行。"""
        if settings.SPOT_FETCH_PARALLEL and _parallel_allowed("腾讯"):
            try:
                df = _SpotMixin._fetch_spot_tencent_parallel()
                if df is None or df.empty:
                    raise RuntimeError("并行抓取返回空(疑似被限流)")
                _parallel_ok("腾讯")
                return _SpotMixin._normalize_tencent_spot(df)
            except Exception as e:
                _parallel_fail("腾讯")
                logger.warning(f"腾讯并行抓取失败，切串行: {e}")
        return _SpotMixin._normalize_tencent_spot(ak.stock_zh_a_spot_tx())


    @staticmethod
    @_spot_fetch
    def _fetch_spot_eastmoney() -> pd.DataFrame:
        """从东财获取全市场实时行情并归一化列名（原始主力源）。
        东财 clist 端点按请求频率限流（实测连续 ~8 次请求后连接被 RemoteDisconnected 重置），
        akshare 默认 pz=100 分页拉全市场需 ~59 次请求必被断 → 临时补丁 akshare 分页层：
        ① 加大 pz(2000) 减少分页次数（被截断时 fetch_paginated_data 按实际页大小自适应）；
        ② 逐页轮换 push2 主机（_EM_CLIST_HOSTS），规避单个后端主机被限流。
        解析仍交给 akshare（列口径一致，避免自解析 f-code 顺序变化的坑）。"""
        import akshare as ak
        import akshare.utils.func as _func
        _hist_em = None
        try:
            import akshare.stock_feature.stock_hist_em as _hist_em
        except Exception:
            pass
        _orig = _func.fetch_paginated_data
        _state = {"idx": 0}

        def _patched(url, params, timeout=15):
            p = dict(params or {})
            p["pz"] = "2000"  # 减少分页次数，降低触发东财频率限流概率
            host = _EM_CLIST_HOSTS[_state["idx"] % len(_EM_CLIST_HOSTS)]
            _state["idx"] += 1
            url = url.replace("82.push2", host)
            return _orig(url, p, timeout)

        _func.fetch_paginated_data = _patched
        if _hist_em is not None and hasattr(_hist_em, "fetch_paginated_data"):
            _hist_em.fetch_paginated_data = _patched
        try:
            df = ak.stock_zh_a_spot_em()
        finally:
            _func.fetch_paginated_data = _orig
            if _hist_em is not None and hasattr(_hist_em, "fetch_paginated_data"):
                _hist_em.fetch_paginated_data = _orig
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
    def _fill_ohlc_from_sina(df: pd.DataFrame) -> pd.DataFrame:
        """腾讯源不提供 开/高/低，用新浪的 OHLC 补齐（新浪有 OHLC 但缺量比振幅）。
        仅当主源 OHLC 缺失/全 0 时调用，避免低开猛拉等信号在 0 值 OHLC 上误触发（垃圾判定）。"""
        try:
            if df is None or df.empty or "open" not in df.columns:
                return df
            if pd.to_numeric(df["open"], errors="coerce").fillna(0).abs().max() > 0:
                return df  # OHLC 已有真实值，无需补
            # 新浪 OHLC 缓存（60 秒），避免每 15 秒轮询都全量拉一次新浪
            global _sina_ohlc_cache, _sina_ohlc_cache_time
            now = _time.time()
            if _sina_ohlc_cache is None or (now - _sina_ohlc_cache_time) > _OHLC_CACHE_SECONDS:
                _sina_ohlc_cache = _SpotMixin._fetch_spot_sina()
                _sina_ohlc_cache_time = now
            sina = _sina_ohlc_cache
            if sina is None or sina.empty or not {"code", "open", "high", "low"} <= set(sina.columns):
                return df
            sina_slim = sina[["code", "open", "high", "low"]].copy()
            sina_slim["code"] = sina_slim["code"].astype(str)
            out = df.copy()
            out["code"] = out["code"].astype(str)
            out = out.merge(sina_slim, on="code", how="left", suffixes=("", "_sina"))
            for col in ["open", "high", "low"]:
                c2 = f"{col}_sina"
                if c2 in out.columns:
                    cur = pd.to_numeric(out[col], errors="coerce")
                    fill = pd.to_numeric(out[c2], errors="coerce")
                    out[col] = cur.where(cur > 0, fill)
                    out = out.drop(columns=[c2])
            logger.info("腾讯源 OHLC 缺失，已用新浪补齐")
            return out
        except Exception as e:
            logger.warning(f"用新浪补齐 OHLC 失败: {e}")
            return df

    @staticmethod
    def get_realtime_spot() -> pd.DataFrame:
        """
        获取全市场 A 股实时行情快照.
        多数据源降级:东财 -> 腾讯 -> 新浪,某个源失败自动切换下一个.
        """
        # 源优先级：东财(量比/振幅/OHLC/市值全) → 腾讯(量比lb/振幅zf, 缺OHLC) → 新浪(OHLC, 缺量比振幅)。
        # 信号依赖量比/振幅：若退回新浪会导致所有信号永不触发（历史 bug：从未产生 AI 买入）。
        df = multi_source_fetch([
            ("东财", _SpotMixin._fetch_spot_eastmoney),
            ("腾讯", _SpotMixin._fetch_spot_tencent),
            ("新浪", _SpotMixin._fetch_spot_sina),
        ])
        if df.empty:
            logger.warning("获取全市场实时行情为空（所有数据源均失败）.")
            return pd.DataFrame()
        # 腾讯源缺 OHLC，用新浪补齐，避免低开猛拉/一字板等在 0 值上误触发
        df = _SpotMixin._fill_ohlc_from_sina(df)
        # 过滤前缓存全量总成交额（含科创板/北交所/ST,对齐券商软件）
        global _cached_total_amount
        if "amount" in df.columns:
            _cached_total_amount = float(df["amount"].sum())
        return _SpotMixin.filter_stocks(df)


