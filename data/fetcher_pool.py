import logging
import pandas as pd
from config.settings import settings
import akshare as ak
logger = logging.getLogger(__name__)
from data.core import retry_on_exception
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


