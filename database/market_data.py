import logging
from typing import List, Dict, Optional, Any
import pandas as pd
from database.models import MarketIndex, DailyEquitySnapshot, DailyZtPool, SectorStrength
from database.connection import db_manager
from config.settings import settings
logger = logging.getLogger(__name__)

class MarketIndexManager:
    """大盘指数日线数据管理"""

    @staticmethod
    def save_daily_index(trade_date: str, spot_df=None, total_amount_yuan: float = 0.0):
        """
        保存当日大盘指数数据。
        优先从 akshare 获取真实指数，失败则从全市场快照估算。
        total_amount_yuan: 全市场未过滤成交额（元），对齐券商软件口径。
        """
        import numpy as np

        session = db_manager.get_session()
        try:
            # 检查是否已存在
            existing = session.query(MarketIndex).filter(
                MarketIndex.trade_date == trade_date
            ).first()
            if existing:
                return

            sh = MarketIndexManager._fetch_index("sh000001")
            sz = MarketIndexManager._fetch_index("sz399001")
            gem = MarketIndexManager._fetch_index("sz399006")
            if sh is None and sz is None and gem is None:
                logger.warning("三个指数全部获取失败，跳过当日指数落库（避免落 0 值污染看板）")
                return
            sh_close, sh_change = sh if sh else (0.0, 0.0)
            sz_close, sz_change = sz if sz else (0.0, 0.0)
            gem_close, gem_change = gem if gem else (0.0, 0.0)

            # 优先使用传入的全市场未过滤成交额，兜底从快照估算
            if total_amount_yuan > 0:
                total_amt = round(total_amount_yuan / 1e8, 2)
            elif spot_df is not None and not spot_df.empty and "amount" in spot_df.columns:
                total_amt = round(float(spot_df["amount"].sum()) / 1e8, 2)
            else:
                total_amt = 0.0

            record = MarketIndex(
                trade_date=trade_date,
                sh_close=sh_close,
                sh_change_pct=sh_change,
                sz_close=sz_close,
                sz_change_pct=sz_change,
                gem_close=gem_close,
                gem_change_pct=gem_change,
                total_amount=total_amt,
            )
            session.add(record)
            session.commit()
            logger.info(
                f"大盘指数已保存: 上证{sh_close}({sh_change:+.2f}%) "
                f"深证{sz_close}({sz_change:+.2f}%) 创业板{gem_close}({gem_change:+.2f}%)"
            )
        except Exception as e:
            session.rollback()
            logger.warning(f"保存大盘指数失败: {e}")
        finally:
            session.close()

    @staticmethod
    def _fetch_index(symbol: str):
        """获取单个指数最新收盘价和涨跌幅，失败返回 None（避免调用方落 0 值污染看板）"""
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is not None and not df.empty:
                close = float(pd.to_numeric(df["close"].iloc[-1]))
                # 计算涨跌幅：相对于前一日收盘
                if len(df) >= 2:
                    prev = float(pd.to_numeric(df["close"].iloc[-2]))
                    change = round((close - prev) / prev * 100, 2) if prev > 0 else 0.0
                else:
                    change = 0.0
                return round(close, 2), change
        except Exception:
            pass
        return None

    @staticmethod
    def get_latest() -> Optional[Dict[str, Any]]:
        """获取最新一条指数数据"""
        from database.models import MarketIndex
        session = db_manager.get_session()
        try:
            record = session.query(MarketIndex).order_by(
                MarketIndex.trade_date.desc()
            ).first()
            if record:
                return {
                    "trade_date": record.trade_date,
                    "sh_close": record.sh_close,
                    "sh_change_pct": record.sh_change_pct,
                    "sz_close": record.sz_close,
                    "sz_change_pct": record.sz_change_pct,
                    "gem_close": record.gem_close,
                    "gem_change_pct": record.gem_change_pct,
                    "total_amount": record.total_amount,
                }
            return None
        finally:
            session.close()

    @staticmethod
    def get_recent(days: int = 5) -> List[Dict[str, Any]]:
        """获取最近 N 个交易日的大盘指数"""
        from database.models import MarketIndex
        session = db_manager.get_session()
        try:
            records = session.query(MarketIndex).order_by(
                MarketIndex.trade_date.desc()
            ).limit(days).all()
            return [{
                "trade_date": r.trade_date,
                "sh_close": r.sh_close,
                "sh_change_pct": r.sh_change_pct,
                "sz_close": r.sz_close,
                "sz_change_pct": r.sz_change_pct,
                "gem_close": r.gem_close,
                "gem_change_pct": r.gem_change_pct,
                "total_amount": r.total_amount,
            } for r in records]
        finally:
            session.close()

class DailySnapshotManager:
    """每日净值快照与绩效跟踪"""

    @staticmethod
    def save_snapshot(trade_date: str, pnl_report: dict, sh_change_pct: float = 0.0):
        """从每日盈亏报告提取关键指标落库"""
        from database.models import DailyEquitySnapshot
        session = db_manager.get_session()
        try:
            existing = session.query(DailyEquitySnapshot).filter(
                DailyEquitySnapshot.trade_date == trade_date
            ).first()
            if existing:
                return

            total_equity = settings.BACKTEST_INITIAL_CAPITAL + pnl_report.get("cumulative_total_pnl", 0)
            # 持仓市值 = Σ(现价 × 数量)；可用资金 = 总权益 - 持仓市值
            position_value = 0.0
            for _h in pnl_report.get("holdings", []) or []:
                _qty = _h.get("quantity", 0) or 0
                _cur = _h.get("current_price", 0) or _h.get("cost_price", 0) or 0
                position_value += _cur * _qty
            available_cash = total_equity - position_value
            snapshot = DailyEquitySnapshot(
                trade_date=trade_date,
                total_equity=round(total_equity, 2),
                available_cash=round(max(available_cash, 0.0), 2),
                position_value=round(position_value, 2),
                unrealized_pnl=pnl_report.get("total_unrealized_pnl", 0),
                today_realized_pnl=pnl_report.get("today_realized_pnl", 0),
                total_realized_pnl=pnl_report.get("total_realized_pnl", 0),
                position_count=pnl_report.get("active_positions", 0),
                today_pnl_pct=pnl_report.get("today_total_pnl_pct", 0),
                cumulative_pnl_pct=pnl_report.get("cumulative_total_pnl_pct", 0),
                sh_change_pct=sh_change_pct,
            )
            session.add(snapshot)
            session.commit()
            logger.info(f"净值快照已保存: 权益={total_equity:.0f} 持仓{snapshot.position_count}只")
        except Exception as e:
            session.rollback()
            logger.warning(f"保存净值快照失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_equity_curve(days: int = 60) -> List[Dict[str, Any]]:
        """获取净值曲线（最近 N 天）"""
        from database.models import DailyEquitySnapshot
        session = db_manager.get_session()
        try:
            # 取最近 N 条（倒序取 limit 再反转，返回升序序列便于前端画图）
            records = session.query(DailyEquitySnapshot).order_by(
                DailyEquitySnapshot.trade_date.desc()
            ).limit(days).all()
            records = list(reversed(records))
            return [{
                "date": r.trade_date,
                "equity": r.total_equity,
                "pnl_pct": r.today_pnl_pct,
                "cumulative_pct": r.cumulative_pnl_pct,
                "sh_pct": r.sh_change_pct,
                "positions": r.position_count,
            } for r in records]
        finally:
            session.close()

class ZtPoolManager:
    """涨停池明细管理"""

    @staticmethod
    def save_daily_zt_pool(trade_date: str, zt_df):
        """保存当日涨停池明细，幂等（已有当天数据则跳过）"""
        from database.models import DailyZtPool
        if zt_df is None or zt_df.empty:
            return
        session = db_manager.get_session()
        try:
            existing = session.query(DailyZtPool).filter(
                DailyZtPool.trade_date == trade_date
            ).first()
            if existing:
                return

            count = 0
            for _, row in zt_df.iterrows():
                zt = DailyZtPool(
                    trade_date=trade_date,
                    code=str(row.get("code", "")),
                    name=str(row.get("name", "")),
                    price=float(row.get("price", 0)),
                    change_pct=float(row.get("change_pct", 0)),
                    lbc=int(row.get("lbc", 1)) if "lbc" in row.index else 1,
                    seal_amount=float(row.get("seal_amount", 0)),
                    first_seal_time=str(row.get("first_seal_time", "")) if "first_seal_time" in row.index else "",
                    open_count=int(row.get("open_count", 0)) if "open_count" in row.index else 0,
                    industry=str(row.get("industry", "")) if "industry" in row.index else "",
                    amount=float(row.get("amount", 0)),
                    turnover_rate=float(row.get("turnover_rate", 0)) if "turnover_rate" in row.index else 0,
                    circ_market_cap=float(row.get("circ_market_cap", 0)) if "circ_market_cap" in row.index else 0,
                )
                session.add(zt)
                count += 1

            session.commit()
            logger.info(f"涨停池明细已保存: {trade_date} {count}只涨停")
        except Exception as e:
            session.rollback()
            logger.warning(f"保存涨停池明细失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_industry_zt_trend(industry: str, days: int = 20) -> List[Dict[str, Any]]:
        """查询某行业最近 N 天的涨停数量趋势"""
        from database.models import DailyZtPool
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            records = session.query(
                DailyZtPool.trade_date,
                func.count(DailyZtPool.id).label("zt_count"),
                func.max(DailyZtPool.lbc).label("max_lbc"),
            ).filter(
                DailyZtPool.industry == industry
            ).group_by(DailyZtPool.trade_date).order_by(
                DailyZtPool.trade_date.desc()
            ).limit(days).all()
            return [{"date": r.trade_date, "zt_count": r.zt_count, "max_lbc": r.max_lbc} for r in records]
        finally:
            session.close()

    @staticmethod
    def get_top_dragons(limit: int = 10) -> List[Dict[str, Any]]:
        """获取最新交易日的涨停龙头（按连板数降序）"""
        from database.models import DailyZtPool
        session = db_manager.get_session()
        try:
            latest_date = session.query(DailyZtPool.trade_date).order_by(
                DailyZtPool.trade_date.desc()
            ).first()
            if not latest_date:
                return []
            records = session.query(DailyZtPool).filter(
                DailyZtPool.trade_date == latest_date[0]
            ).order_by(DailyZtPool.lbc.desc()).limit(limit).all()
            return [{"code": r.code, "name": r.name, "lbc": r.lbc,
                     "industry": r.industry or "", "price": r.price,
                     "change_pct": r.change_pct,
                     "first_seal_time": r.first_seal_time or "",
                     "open_count": r.open_count or 0,
                     "_date": latest_date[0]} for r in records]
        finally:
            session.close()

class SectorStrengthManager:
    """板块强度管理"""

    @staticmethod
    def save_daily_sectors(trade_date: str, zt_df):
        """从涨停池按行业聚合，计算板块强度落库"""
        from database.models import SectorStrength
        if zt_df is None or zt_df.empty or "industry" not in zt_df.columns:
            return
        session = db_manager.get_session()
        try:
            existing = session.query(SectorStrength).filter(
                SectorStrength.trade_date == trade_date
            ).first()
            if existing:
                return

            # 按行业分组统计
            industry_groups = zt_df.groupby(zt_df["industry"].astype(str))
            count = 0
            for sector, group in industry_groups:
                if not sector or sector == "nan":
                    continue
                zt_count = len(group)
                if zt_count < 2:  # 少于 2 只涨停的板块不存
                    continue

                # 领涨标的
                top_codes = group.sort_values("lbc", ascending=False).head(5) if "lbc" in group.columns else group.head(3)
                top_list = [f"{str(r['code'])}:{str(r['name'])}" for _, r in top_codes.iterrows()]

                # 上日同板块涨停数
                prev_count = SectorStrengthManager._get_prev_zt_count(
                    session, trade_date, sector
                )

                ss = SectorStrength(
                    trade_date=trade_date,
                    sector_name=sector,
                    zt_count=zt_count,
                    prev_zt_count=prev_count,
                    acceleration=zt_count - prev_count,
                    total_stocks=0,
                    zt_ratio_pct=0.0,
                    top_stocks=",".join(top_list[:5]),
                )
                session.add(ss)
                count += 1

            session.commit()
            logger.info(f"板块强度已保存: {trade_date} {count}个活跃板块")
        except Exception as e:
            session.rollback()
            logger.warning(f"保存板块强度失败: {e}")
        finally:
            session.close()

    @staticmethod
    def _get_prev_zt_count(session, trade_date: str, sector: str) -> int:
        """查询同板块上日涨停数"""
        from database.models import SectorStrength, DailyZtPool
        # 先从 sector_strength 表查
        from sqlalchemy import desc
        prev = session.query(SectorStrength).filter(
            SectorStrength.sector_name == sector,
            SectorStrength.trade_date < trade_date,
        ).order_by(desc(SectorStrength.trade_date)).first()
        if prev:
            return prev.zt_count
        return 0

    @staticmethod
    def get_hot_sectors(date_str: str = None, top_n: int = 10) -> List[Dict[str, Any]]:
        """查询某日热门板块，默认最新交易日"""
        from database.models import SectorStrength
        session = db_manager.get_session()
        try:
            if date_str is None:
                latest = session.query(SectorStrength.trade_date).order_by(
                    SectorStrength.trade_date.desc()
                ).first()
                date_str = latest[0] if latest else ""
            records = session.query(SectorStrength).filter(
                SectorStrength.trade_date == date_str
            ).order_by(SectorStrength.zt_count.desc()).limit(top_n).all()
            return [{
                "sector": r.sector_name,
                "zt_count": r.zt_count,
                "prev_count": r.prev_zt_count,
                "accel": r.acceleration,
                "top_stocks": r.top_stocks,
                "_date": date_str,
            } for r in records]
        finally:
            session.close()