import logging
from typing import List, Dict, Any, Optional
from database.models import DatabaseManager, Holding, Recommendation, DailySentiment, HistoricDragon, PushLog, LLMLog, ErrorLog, TradeCalendar

logger = logging.getLogger(__name__)

# 全局数据库单例
db_manager = DatabaseManager()


class HoldingManager:
    """
    持仓股票数据库管理服务
    """

    @staticmethod
    def get_active_holdings(holding_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有处于 HOLDING 状态的持仓列表，可按 holding_type 筛选 (MANUAL / AI_AUTO)"""
        session = db_manager.get_session()
        try:
            query = session.query(Holding).filter(Holding.status == "HOLDING")
            if holding_type:
                query = query.filter(Holding.holding_type == holding_type)
            holdings = query.all()
            return [
                {
                    "id": h.id,
                    "code": h.code,
                    "name": h.name,
                    "cost_price": h.cost_price,
                    "current_price": h.current_price,
                    "profit_rate": h.profit_rate,
                    "quantity": h.quantity,
                    "buy_date": h.buy_date,
                    "buy_strategy": h.buy_strategy,
                    "holding_type": h.holding_type,
                    "was_limit_up_today": h.was_limit_up_today
                }
                for h in holdings
            ]
        finally:
            session.close()

    @staticmethod
    def add_holding(
        code: str,
        cost_price: float,
        name: str = "",
        quantity: int = 100,
        buy_date: str = "",
        strategy: str = "低吸战法",
        holding_type: str = "MANUAL"
    ) -> bool:
        """添加新持仓记录 (若未传入名称则自动匹配)"""
        session = db_manager.get_session()
        try:
            import datetime
            from data.fetcher import DataFetcher
            stock_name = name or DataFetcher.get_stock_name(code=code)
            buy_dt = buy_date or datetime.datetime.now().strftime("%Y-%m-%d")
            holding = Holding(
                code=code,
                name=stock_name,
                cost_price=cost_price,
                current_price=cost_price,
                profit_rate=0.0,
                quantity=quantity,
                buy_date=buy_dt,
                buy_strategy=strategy,
                holding_type=holding_type,
                status="HOLDING"
            )
            session.add(holding)
            session.commit()
            logger.info(f"成功添加 [{holding_type}] 持仓: {stock_name}({code}) 成本价: {cost_price}")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"添加持仓失败: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def update_holding_profit_rate(code: str, current_price: float):
        """更新持仓最新实时价格与收益率"""
        session = db_manager.get_session()
        try:
            holding = session.query(Holding).filter(Holding.code == code, Holding.status == "HOLDING").first()
            if holding and holding.cost_price > 0:
                holding.current_price = current_price
                holding.profit_rate = round(((current_price - holding.cost_price) / holding.cost_price) * 100, 2)
                session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"更新持仓收益率失败: {e}")
        finally:
            session.close()

    @staticmethod
    def update_was_limit_up(code: str, was_zt: bool):
        """更新持仓股票今日是否曾封涨停状态"""
        session = db_manager.get_session()
        try:
            holding = session.query(Holding).filter(Holding.code == code, Holding.status == "HOLDING").first()
            if holding:
                holding.was_limit_up_today = was_zt
                session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"更新曾封涨停牌状态失败: {e}")
        finally:
            session.close()

    @staticmethod
    def close_holding(code: str, holding_type: Optional[str] = None) -> bool:
        """平仓指定持仓股票"""
        session = db_manager.get_session()
        try:
            query = session.query(Holding).filter(Holding.code == code, Holding.status == "HOLDING")
            if holding_type:
                query = query.filter(Holding.holding_type == holding_type)
            holding = query.first()
            if holding:
                holding.status = "CLOSED"
                session.commit()
                logger.info(f"成功平仓 [{holding.holding_type}] 股票 {holding.name}({code})")
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"平仓失败: {e}")
            return False
        finally:
            session.close()


class RecommendationManager:
    """
    复盘/竞价推荐标的数据库管理服务
    """

    @staticmethod
    def add_recommendations(trade_date: str, items: List[Dict[str, Any]]):
        """保存盘后复盘推荐标的"""
        session = db_manager.get_session()
        try:
            for item in items:
                rec = Recommendation(
                    trade_date=trade_date,
                    code=item.get("code"),
                    name=item.get("name"),
                    strategy_type=item.get("strategy_type", "观察"),
                    open_requirement=item.get("open_requirement", ""),
                    auction_vol_ratio=item.get("auction_vol_ratio", ""),
                    buy_condition=item.get("buy_condition", ""),
                    sell_condition=item.get("sell_condition", ""),
                    status="PENDING"
                )
                session.add(rec)
            session.commit()
            logger.info(f"成功保存 {trade_date} 推荐标的 {len(items)} 个")
        except Exception as e:
            session.rollback()
            logger.error(f"保存推荐标的失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_pending_recommendations(trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取待观察的推荐标的列表"""
        session = db_manager.get_session()
        try:
            query = session.query(Recommendation).filter(Recommendation.status == "PENDING")
            if trade_date:
                query = query.filter(Recommendation.trade_date == trade_date)
            recs = query.all()
            return [
                {
                    "id": r.id,
                    "trade_date": r.trade_date,
                    "code": r.code,
                    "name": r.name,
                    "strategy_type": r.strategy_type,
                    "open_requirement": r.open_requirement,
                    "auction_vol_ratio": r.auction_vol_ratio,
                    "buy_condition": r.buy_condition
                }
                for r in recs
            ]
        finally:
            session.close()


class SentimentManager:
    """
    每日情绪历史向量数据库管理服务
    """

    @staticmethod
    def save_daily_sentiment(trade_date: str, sentiment_data: Dict[str, Any], cycle_stage: str = "", summary: str = ""):
        """保存每日情绪分值与周期定性"""
        session = db_manager.get_session()
        try:
            # 存在则更新，不存在则插入
            record = session.query(DailySentiment).filter(DailySentiment.trade_date == trade_date).first()
            if not record:
                record = DailySentiment(trade_date=trade_date)
                session.add(record)

            record.height = sentiment_data.get("height", 0)
            record.breadth = sentiment_data.get("breadth", 0)
            record.zt_count = sentiment_data.get("zt_count", 0)
            record.dt_count = sentiment_data.get("dt_count", 0)
            record.zhaban_count = sentiment_data.get("zhaban_count", 0)
            record.yield_rate = sentiment_data.get("yield_rate", 0.0)
            record.seal_force_ratio = sentiment_data.get("seal_force_ratio", 0.0)
            record.zhaban_rate = sentiment_data.get("zhaban_rate", 0.0)
            record.sentiment_index = sentiment_data.get("sentiment_index", 0.0)
            record.cycle_stage = cycle_stage
            record.summary = summary

            session.commit()
            logger.info(f"已保存 {trade_date} 每日情绪向量与周期结论")
        except Exception as e:
            session.rollback()
            logger.error(f"保存每日情绪数据失败: {e}")
        finally:
            session.close()


class DragonManager:
    """
    历史龙头数据服务 (用于二波战法溯源)
    """

    @staticmethod
    def get_recent_dragons(days_lookback: int = 30) -> List[Dict[str, Any]]:
        """获取近 30 天内的人气总龙头"""
        session = db_manager.get_session()
        try:
            dragons = session.query(HistoricDragon).filter(HistoricDragon.is_active == True).all()
            return [
                {
                    "code": d.code,
                    "name": d.name,
                    "max_lbc": d.max_lbc,
                    "peak_date": d.peak_date,
                    "peak_price": d.peak_price,
                    "board_name": d.board_name
                }
                for d in dragons
            ]
        finally:
            session.close()


class PushLogManager:
    """
    推送通知日志数据库管理服务
    """

    @staticmethod
    def add_log(
        title: str,
        body: str,
        push_group: str = "",
        level: str = "active",
        send_success: bool = False,
        error_msg: str = ""
    ):
        """新增一条推送日志"""
        session = db_manager.get_session()
        try:
            log = PushLog(
                title=title,
                body=body,
                push_group=push_group,
                level=level,
                send_success=send_success,
                error_msg=error_msg
            )
            session.add(log)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"保存推送日志失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_logs(
        date_str: Optional[str] = None,
        push_group: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        查询推送历史日志，支持按日期 (YYYY-MM-DD) 和分组筛选
        """
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            query = session.query(PushLog).order_by(PushLog.created_at.desc())
            if date_str:
                # SQLite 下用 strftime 提取日期部分做比较
                query = query.filter(func.strftime("%Y-%m-%d", PushLog.created_at) == date_str)
            if push_group:
                query = query.filter(PushLog.push_group == push_group)
            logs = query.limit(limit).all()
            return [
                {
                    "id": l.id,
                    "title": l.title,
                    "body": l.body[:200],  # 摘要截断，完整内容按需查询
                    "push_group": l.push_group,
                    "level": l.level,
                    "send_success": l.send_success,
                    "error_msg": l.error_msg,
                    "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
                }
                for l in logs
            ]
        finally:
            session.close()


class LLMLogManager:
    """LLM 调用日志管理服务"""

    @staticmethod
    def add_log(
        module: str,
        model: str = "",
        system_prompt: str = "",
        user_prompt: str = "",
        response: str = "",
        tokens_used: int = 0,
        success: bool = True,
        error_msg: str = ""
    ):
        """新增一条 LLM 调用日志"""
        session = db_manager.get_session()
        try:
            log = LLMLog(
                module=module,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=response,
                tokens_used=tokens_used,
                success=success,
                error_msg=error_msg or ""
            )
            session.add(log)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"保存 LLM 日志失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_logs(
        module: Optional[str] = None,
        date_str: Optional[str] = None,
        success_only: Optional[bool] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """查询 LLM 调用历史"""
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            query = session.query(LLMLog).order_by(LLMLog.created_at.desc())
            if module:
                query = query.filter(LLMLog.module == module)
            if date_str:
                query = query.filter(func.strftime("%Y-%m-%d", LLMLog.created_at) == date_str)
            if success_only is not None:
                query = query.filter(LLMLog.success == success_only)
            logs = query.limit(limit).all()
            return [
                {
                    "id": l.id,
                    "module": l.module,
                    "model": l.model,
                    "system_prompt": l.system_prompt[:300] if l.system_prompt else "",
                    "user_prompt": l.user_prompt[:500] if l.user_prompt else "",
                    "response": l.response[:500] if l.response else "",
                    "tokens_used": l.tokens_used,
                    "success": l.success,
                    "error_msg": l.error_msg,
                    "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
                }
                for l in logs
            ]
        finally:
            session.close()


class ErrorLogManager:
    """系统错误日志管理服务"""

    @staticmethod
    def add_log(level: str = "ERROR", module: str = "", message: str = "", traceback: str = ""):
        """新增一条错误日志"""
        session = db_manager.get_session()
        try:
            log = ErrorLog(
                level=level,
                module=module,
                message=message,
                traceback=traceback or ""
            )
            session.add(log)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    @staticmethod
    def get_logs(
        level: Optional[str] = None,
        date_str: Optional[str] = None,
        module: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """查询错误日志"""
        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            query = session.query(ErrorLog).order_by(ErrorLog.created_at.desc())
            if level:
                query = query.filter(ErrorLog.level == level.upper())
            if date_str:
                query = query.filter(func.strftime("%Y-%m-%d", ErrorLog.created_at) == date_str)
            if module:
                query = query.filter(ErrorLog.module.like(f"%{module}%"))
            logs = query.limit(limit).all()
            return [
                {
                    "id": l.id,
                    "level": l.level,
                    "module": l.module,
                    "message": l.message[:500] if l.message else "",
                    "traceback": l.traceback[:1000] if l.traceback else "",
                    "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""
                }
                for l in logs
            ]
        finally:
            session.close()


class TradeCalendarManager:
    """交易日历管理服务。维护过去30天+未来30天的交易日数据。"""

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
    def sync_calendar(force: bool = False):
        """
        从 akshare 同步交易日历，保留 ±30 天数据，清理过期记录。
        每周强制全量覆盖 ±30 天窗口（应对调休/假期安排变动）。
        :param force: True 强制刷新，忽略缓存
        """
        import datetime
        today = datetime.date.today()
        start = today - datetime.timedelta(days=30)
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
                    # 检查最新同步日期（用表中最新日期推断）
                    last_date = session.query(TradeCalendar).order_by(
                        TradeCalendar.trade_date.desc()
                    ).first()
                    if last_date and last_date.trade_date >= (today + datetime.timedelta(days=25)).isoformat():
                        return  # 未来数据充足，跳过

            # 从 akshare 拉取
            import akshare as ak
            import pandas as pd
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                all_dates = pd.to_datetime(df["trade_date"]).dt.date.tolist()
                window_dates = [d for d in all_dates if start <= d <= end]

                # 全量覆盖：先删后插 ±30 天范围，确保调休变动被更新
                session.query(TradeCalendar).filter(
                    TradeCalendar.trade_date >= start.isoformat(),
                    TradeCalendar.trade_date <= end.isoformat()
                ).delete()

                for d in window_dates:
                    session.add(TradeCalendar(trade_date=d.isoformat()))

                # 清理过期数据
                cutoff = (today - datetime.timedelta(days=31)).isoformat()
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
