import logging
from typing import List, Dict, Optional, Any
from database.models import TradeCalendar
from database.connection import db_manager
logger = logging.getLogger(__name__)

class TradeCalendarManager:
    """交易日历管理服务。维护过去120天+未来30天的交易日数据。
    （历史窗口 120 天：供『龙头过期 30 个交易日』等按交易日回溯——30 交易日≈42 自然日+长假余量；
    原 ±30 天覆盖不够，30 个交易日会超出表范围导致查不到。）"""

    @staticmethod
    def is_trading_day(date_str: str) -> bool:
        """判断指定日期 (YYYY-MM-DD) 是否为交易日"""
        session = db_manager.get_session()
        try:
            return session.query(TradeCalendar).filter(
                TradeCalendar.trade_date == date_str
            ).count() > 0
        finally:
            session.close()

    @staticmethod
    def count_trading_days(start_date: str, end_date: str) -> Optional[int]:
        """start~end(含)之间的交易日数；区间超出日历覆盖范围(过去120天~未来30天)返回 None，由调用方回退估算。"""
        session = db_manager.get_session()
        try:
            earliest = session.query(TradeCalendar).order_by(TradeCalendar.trade_date.asc()).first()
            latest = session.query(TradeCalendar).order_by(TradeCalendar.trade_date.desc()).first()
            if earliest is None or latest is None:
                return None
            if start_date < earliest.trade_date or end_date > latest.trade_date:
                return None
            return session.query(TradeCalendar).filter(
                TradeCalendar.trade_date >= start_date,
                TradeCalendar.trade_date <= end_date
            ).count()
        finally:
            session.close()

    @staticmethod
    def sync_calendar(force: bool = False):
        """
        从 akshare 同步交易日历，保留 过去120天+未来30天 数据，清理过期记录。
        每周强制全量覆盖（应对调休/假期安排变动）。
        历史窗口 120 天：30 个交易日≈42 自然日，原 ±30 天覆盖不够按交易日回溯的场景。
        :param force: True 强制刷新，忽略缓存
        """
        import datetime
        today = datetime.date.today()
        start = today - datetime.timedelta(days=120)
        end = today + datetime.timedelta(days=30)

        session = db_manager.get_session()
        try:
            today_str = today.isoformat()

            # 非强制模式：今天已存在且距上次同步不到 7 天，跳过
            if not force:
                existing = session.query(TradeCalendar).filter(
                    TradeCalendar.trade_date == today_str
                ).count()
                if existing > 0:
                    # 未来数据充足 且 历史覆盖到 120 天前 → 跳过；
                    # 历史不足（如升级前的旧 ±30 天数据）→ 重新同步补齐历史窗口，供 30 个交易日回溯
                    last_date = session.query(TradeCalendar).order_by(
                        TradeCalendar.trade_date.desc()
                    ).first()
                    first_date = session.query(TradeCalendar).order_by(
                        TradeCalendar.trade_date.asc()
                    ).first()
                    if (last_date and last_date.trade_date >= (today + datetime.timedelta(days=25)).isoformat()
                            and first_date
                            and first_date.trade_date <= (today - datetime.timedelta(days=115)).isoformat()):
                        return  # 未来充足且历史覆盖足够，跳过

            # 从 akshare 拉取
            import akshare as ak
            import pandas as pd
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                all_dates = pd.to_datetime(df["trade_date"]).dt.date.tolist()
                window_dates = [d for d in all_dates if start <= d <= end]

                # 全量覆盖：先删后插 覆盖窗口 数据，确保调休变动被更新
                session.query(TradeCalendar).filter(
                    TradeCalendar.trade_date >= start.isoformat(),
                    TradeCalendar.trade_date <= end.isoformat()
                ).delete()

                for d in window_dates:
                    session.add(TradeCalendar(trade_date=d.isoformat()))

                # 清理过期数据
                cutoff = (today - datetime.timedelta(days=121)).isoformat()
                session.query(TradeCalendar).filter(
                    TradeCalendar.trade_date < cutoff
                ).delete()

                session.commit()
                action = "强制刷新" if force else "同步"
                logger.info(f"交易日历已{action}: {len(window_dates)} 个交易日, 范围 {start} ~ {end}")
        except Exception as e:
            session.rollback()
            logger.warning(f"交易日历同步失败: {e}")
        finally:
            session.close()