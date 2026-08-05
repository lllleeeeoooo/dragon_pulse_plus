import logging
import pandas as pd
from config.settings import settings
import akshare as ak
logger = logging.getLogger(__name__)
from data.core import retry_on_exception, multi_source_fetch
from data.fetcher_spot import _SpotMixin

class _PoolMixin:
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
        return _SpotMixin.filter_stocks(df) if df is not None else pd.DataFrame()


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
        return _SpotMixin.filter_stocks(df) if df is not None else pd.DataFrame()


    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_concept_boards() -> pd.DataFrame:
        """
        获取概念板块列表（东财 → 新浪 双源降级）。
        东财: stock_board_concept_name_em（约300+概念，板块代码 BKxxxx）
        新浪: stock_sector_spot(indicator='概念')（约175概念，板块代码 gn_xxx）
        ※ 概念代码体系因源而异：东财 BKxxxx 需用「概念名」查成分股，新浪 gn_xxx 直接作参数。
        统一输出: code(概念代码), name(概念名), stock_count, change_pct, amount, leading_stock(领涨股)
        """
        def _from_em() -> pd.DataFrame:
            df = ak.stock_board_concept_name_em()
            if df is None or df.empty:
                return pd.DataFrame()
            # 东财概念列表列名以 akshare 文档为准（当前网络被拒，字段未经实测，get() 兜底）
            out = pd.DataFrame({
                "code": df.get("板块代码", pd.Series(dtype=str)),
                "name": df.get("板块名称", pd.Series(dtype=str)),
                "change_pct": df.get("涨跌幅", 0.0),
                "amount": df.get("成交额", df.get("总市值", 0.0)),
                "leading_stock": df.get("领涨股票", ""),
            })
            up = df.get("上涨家数", 0)
            down = df.get("下跌家数", 0)
            out["stock_count"] = up.fillna(0) + down.fillna(0) if isinstance(up, pd.Series) else 0
            out["code"] = out["code"].astype(str)
            return out[["code", "name", "stock_count", "change_pct", "amount", "leading_stock"]]

        def _from_sina() -> pd.DataFrame:
            df = ak.stock_sector_spot(indicator="概念")
            if df is None or df.empty:
                return pd.DataFrame()
            out = pd.DataFrame({
                "code": df["label"],
                "name": df["板块"],
                "stock_count": df["公司家数"],
                "change_pct": df["涨跌幅"],
                "amount": df["总成交额"],
                "leading_stock": df["股票名称"],
            })
            out["code"] = out["code"].astype(str)
            return out[["code", "name", "stock_count", "change_pct", "amount", "leading_stock"]]

        # 熔断键用 "东财概念板块" 而非 "东财"：概念接口被拒不能误伤主行情源(东财)的槽位
        # （主行情东财若被熔断，信号会退回腾讯/新浪导致信号永不触发）
        return multi_source_fetch([("东财概念板块", _from_em), ("新浪", _from_sina)])


    @staticmethod
    def get_concept_cons(concept: str) -> pd.DataFrame:
        """
        获取概念板块成分股。
        concept 传参规则:
          - 'gn_xxx'（新浪概念代码）→ 直接走新浪 stock_sector_detail
          - 概念名 / 'BKxxxx'（东财）→ 先东财 stock_board_concept_cons_em(按名)，失败后回退新浪（经概念表反查 gn 代码）
        统一输出: code(纯6位), name, price, change_pct, amount, turnover_rate
        异常时返回空 DataFrame,不抛异常.
        """
        concept = str(concept).strip()

        def _cons_from_sina(gn_code: str) -> pd.DataFrame:
            df = ak.stock_sector_detail(sector=gn_code)
            if df is None or df.empty:
                return pd.DataFrame()
            out = pd.DataFrame({
                "code": df["code"].astype(str).str.zfill(6),
                "name": df["name"],
                "price": df.get("trade", 0.0),
                "change_pct": df.get("changepercent", 0.0),
                "amount": df.get("amount", 0.0),
                "turnover_rate": df.get("turnoverratio", 0.0),
            })
            return out[["code", "name", "price", "change_pct", "amount", "turnover_rate"]]

        def _cons_from_em(name: str) -> pd.DataFrame:
            df = ak.stock_board_concept_cons_em(symbol=name)
            if df is None or df.empty:
                return pd.DataFrame()
            out = pd.DataFrame({
                "code": df.get("代码", pd.Series(dtype=str)).astype(str).str.zfill(6),
                "name": df.get("名称", pd.Series(dtype=str)),
                "price": df.get("最新价", 0.0),
                "change_pct": df.get("涨跌幅", 0.0),
                "amount": df.get("成交额", 0.0),
                "turnover_rate": df.get("换手率", 0.0),
            })
            return out[["code", "name", "price", "change_pct", "amount", "turnover_rate"]]

        # 新浪代码直接走新浪
        if concept.startswith("gn_"):
            try:
                return _cons_from_sina(concept)
            except Exception as e:
                logger.warning(f"获取概念[{concept}]成分股失败(新浪): {e}")
                return pd.DataFrame()

        # 东财体系：按名取成分股，失败回退新浪（经概念表反查 gn 代码）
        em_df = pd.DataFrame()
        if not concept.startswith("BK"):
            try:
                em_df = _cons_from_em(concept)
            except Exception as e:
                logger.warning(f"获取概念[{concept}]成分股失败(东财): {e}")
        if not em_df.empty:
            return em_df
        try:
            boards = _PoolMixin.get_concept_boards()
            match = boards[boards["name"].astype(str) == concept]
            if match.empty:
                match = boards[boards["name"].astype(str).str.contains(concept, na=False)]
            if not match.empty:
                gn_code = match.iloc[0]["code"]
                if str(gn_code).startswith("gn_"):
                    return _cons_from_sina(str(gn_code))
        except Exception as e:
            logger.warning(f"概念[{concept}]回退新浪反查失败: {e}")
        return pd.DataFrame()


    @staticmethod
    @retry_on_exception(retries=settings.FETCH_RETRY_COUNT, delay=settings.FETCH_RETRY_DELAY)
    def get_fund_flow_instant() -> pd.DataFrame:
        """
        全市场即时资金流快照（同花顺，东财 push2 限流时的替代源）。
        stock_fund_flow_individual(symbol='即时') 一次返回全市场主力净额，无需逐只查询。
        统一输出: code(6位), name, net_amount(元), inflow(元), outflow(元), amount(元)
        """
        def _to_yuan(v):
            s = str(v).strip()
            try:
                if s.endswith("亿"):
                    return float(s[:-1]) * 1e8
                if s.endswith("万"):
                    return float(s[:-1]) * 1e4
                return float(s)
            except Exception:
                return 0.0

        df = ak.stock_fund_flow_individual(symbol="即时")
        if df is None or df.empty:
            return pd.DataFrame()
        out = pd.DataFrame({
            "code": df["股票代码"].astype(str).str.zfill(6),
            "name": df["股票简称"].astype(str),
            "net_amount": df["净额"].map(_to_yuan),
            "inflow": df["流入资金"].map(_to_yuan),
            "outflow": df["流出资金"].map(_to_yuan),
            "amount": df["成交额"].map(_to_yuan),
        })
        return out


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
            for raw, target in [("名称", "name"), ("股票简称", "name"),
                                ("代码", "code"), ("股票代码", "code"), ("日期", "date"),
                                ("主力净流入-净额", "main_net_inflow"),
                                ("主力净流入-净占比", "main_net_ratio"),
                                ("超大单净流入-净额", "super_large_net"),
                                ("收盘价", "close"), ("涨跌幅", "change_pct")]:
                if raw in df.columns:
                    rename_map[raw] = target
            if rename_map:
                df = df.rename(columns=rename_map)
        return df if df is not None else pd.DataFrame()


