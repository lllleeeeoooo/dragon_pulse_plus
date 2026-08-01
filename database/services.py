import logging
from typing import List, Dict, Any, Optional
from database.models import DatabaseManager, Holding, Recommendation, DailySentiment, HistoricDragon, MarketIndex, DailyEquitySnapshot, DailyZtPool, SectorStrength, PushLog, LLMLog, ErrorLog, TradeCalendar, SystemLog

logger = logging.getLogger(__name__)

# 全局数据库单例
db_manager = DatabaseManager()


def switch_to_test_db():
    """切换到测试数据库（测试用例 setUp 中调用）"""
    from config.settings import settings
    db_manager.reinitialize(settings.TEST_DB_PATH)


def switch_to_prod_db():
    """切换回生产数据库（测试用例 tearDown 中调用，可选）"""
    from config.settings import settings
    db_manager.reinitialize(settings.DB_PATH)


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
    def update_holding_profit_rate(code: str, current_price: float, holding_type: Optional[str] = None):
        """更新持仓最新实时价格与收益率（支持按 holding_type 区分同代码多持仓）"""
        session = db_manager.get_session()
        try:
            query = session.query(Holding).filter(Holding.code == code, Holding.status == "HOLDING")
            if holding_type:
                query = query.filter(Holding.holding_type == holding_type)
            holding = query.first()
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
    def update_was_limit_up(code: str, was_zt: bool, holding_type: Optional[str] = None):
        """更新持仓股票今日是否曾封涨停状态（支持按 holding_type 区分同代码多持仓）"""
        session = db_manager.get_session()
        try:
            query = session.query(Holding).filter(Holding.code == code, Holding.status == "HOLDING")
            if holding_type:
                query = query.filter(Holding.holding_type == holding_type)
            holding = query.first()
            if holding:
                holding.was_limit_up_today = was_zt
                session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"更新曾封涨停状态失败: {e}")
        finally:
            session.close()

    @staticmethod
    def close_holding(code: str, holding_type: Optional[str] = None, sell_price: float = 0.0) -> bool:
        """平仓指定持仓股票，记录卖出价"""
        session = db_manager.get_session()
        try:
            query = session.query(Holding).filter(Holding.code == code, Holding.status == "HOLDING")
            if holding_type:
                query = query.filter(Holding.holding_type == holding_type)
            holding = query.first()
            if holding:
                holding.status = "CLOSED"
                holding.sell_price = sell_price if sell_price > 0 else holding.current_price
                session.commit()
                logger.info(f"成功平仓 [{holding.holding_type}] 股票 {holding.name}({code}), 卖出价:{holding.sell_price}")
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error(f"平仓失败: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def reset_all_limit_up_flags():
        """新交易日重置所有活跃持仓的 was_limit_up_today 标志"""
        session = db_manager.get_session()
        try:
            updated = session.query(Holding).filter(
                Holding.status == "HOLDING",
                Holding.was_limit_up_today == True
            ).update({"was_limit_up_today": False}, synchronize_session="fetch")
            if updated:
                session.commit()
                logger.info(f"新交易日重置 {updated} 只持仓的 was_limit_up_today 标志")
        except Exception as e:
            session.rollback()
            logger.warning(f"重置 was_limit_up_today 失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_trade_statistics(holding_type: Optional[str] = None) -> Dict[str, Any]:
        """
        统计已平仓交易的盈亏，按策略/持仓类型分组。
        这是 AI 真实买卖的成交报告，不是模拟回测。

        :param holding_type: 可选，筛选持仓类型 (AI_AUTO / MANUAL)，不传则统计全部
        """
        session = db_manager.get_session()
        try:
            query = session.query(Holding).filter(Holding.status == "CLOSED")
            if holding_type:
                query = query.filter(Holding.holding_type == holding_type)

            trades = query.all()
            if not trades:
                return {"total_trades": 0, "message": "暂无已平仓交易记录"}

            records = []
            for h in trades:
                # 计算实际盈亏：用 cost_price 和 sell_price
                if h.cost_price > 0 and h.sell_price > 0:
                    realized_pnl = round((h.sell_price - h.cost_price) / h.cost_price * 100, 2)
                else:
                    realized_pnl = 0.0

                records.append({
                    "code": h.code,
                    "name": h.name,
                    "buy_date": h.buy_date,
                    "cost_price": h.cost_price,
                    "sell_price": h.sell_price,
                    "return_pct": realized_pnl,
                    "quantity": h.quantity,
                    "strategy": h.buy_strategy or "",
                    "type": h.holding_type or "",
                })

            total = len(records)
            wins = sum(1 for r in records if r["return_pct"] > 0)
            returns = [r["return_pct"] for r in records]

            # 按策略分组
            by_strategy: dict = {}
            for r in records:
                s = r["strategy"] or "未知"
                by_strategy.setdefault(s, []).append(r["return_pct"])

            strategy_stats = {}
            for s, rets in by_strategy.items():
                strategy_stats[s] = {
                    "trades": len(rets),
                    "win_rate_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
                    "avg_return_pct": round(sum(rets) / len(rets), 2),
                    "total_return_pct": round(sum(rets), 2),
                }

            # 按类型分组
            by_type: dict = {}
            for r in records:
                t = r["type"] or "未知"
                by_type.setdefault(t, []).append(r["return_pct"])

            type_stats = {}
            for t, rets in by_type.items():
                type_stats[t] = {
                    "trades": len(rets),
                    "win_rate_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
                    "avg_return_pct": round(sum(rets) / len(rets), 2),
                }

            # 最近交易
            recent = sorted(records, key=lambda x: x["buy_date"], reverse=True)[:20]

            logger.info(
                f"成交统计: {total}笔 胜率{round(wins/total*100,1)}% "
                f"均收益{round(sum(returns)/len(returns),2)}%"
            )

            return {
                "total_trades": total,
                "win_rate_pct": round(wins / total * 100, 1),
                "avg_return_pct": round(sum(returns) / len(returns), 2),
                "max_return_pct": round(max(returns), 2),
                "min_return_pct": round(min(returns), 2),
                "total_return_pct": round(sum(returns), 2),
                "by_strategy": strategy_stats,
                "by_type": type_stats,
                "recent_trades": recent,
            }
        except Exception as e:
            logger.error(f"获取成交统计失败: {e}")
            return {"error": str(e)}
        finally:
            session.close()

    @staticmethod
    def sync_close_prices(spot_map: Dict[str, float]):
        """
        盘后用当日收盘价更新所有活跃持仓的 current_price。
        spot_map: {code: close_price} 来自当日快照或历史数据。
        同时将旧的 current_price 保存为 prev_close_price（用于次日计算今日涨跌）。
        """
        if not spot_map:
            return
        session = db_manager.get_session()
        try:
            holdings = session.query(Holding).filter(Holding.status == "HOLDING").all()
            updated = 0
            for h in holdings:
                close_px = spot_map.get(h.code, 0)
                if close_px > 0:
                    h.prev_close_price = h.current_price if h.current_price > 0 else close_px
                    h.current_price = close_px
                    if h.cost_price > 0:
                        h.profit_rate = round((close_px - h.cost_price) / h.cost_price * 100, 2)
                    updated += 1
            if updated:
                session.commit()
                logger.info(f"盘后同步 {updated} 只持仓收盘价，prev_close 已保存")
        except Exception as e:
            session.rollback()
            logger.error(f"同步收盘价失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_daily_pnl_report() -> Dict[str, Any]:
        """
        生成每日盈亏报告，包含：
        - 今日盈亏（浮动变化 + 今日平仓已实现）
        - 累计总盈亏（全部已实现 + 当前浮动）
        - 当前持仓明细
        用于盘后 15:30 推送。
        """
        import datetime as _dt
        session = db_manager.get_session()
        try:
            today_str = _dt.datetime.now().strftime("%Y-%m-%d")

            # ----- 当前持仓（浮动盈亏）-----
            active = session.query(Holding).filter(Holding.status == "HOLDING").all()
            holdings_detail = []
            total_unrealized_pnl = 0.0
            total_today_pnl = 0.0

            for h in active:
                if h.cost_price > 0:
                    unrealized = (h.current_price - h.cost_price) / h.cost_price * 100 if h.current_price > 0 else 0.0
                    # 今日涨跌 = (当前价 - 昨收) / 昨收
                    today_change = 0.0
                    if h.prev_close_price > 0 and h.current_price > 0:
                        today_change = (h.current_price - h.prev_close_price) / h.prev_close_price * 100
                else:
                    unrealized = 0.0
                    today_change = 0.0

                pnl_amount = (h.current_price - h.cost_price) * h.quantity if h.current_price > 0 and h.cost_price > 0 else 0.0
                today_amount = (h.current_price - h.prev_close_price) * h.quantity if h.current_price > 0 and h.prev_close_price > 0 else 0.0

                total_unrealized_pnl += pnl_amount
                total_today_pnl += today_amount

                holdings_detail.append({
                    "code": h.code,
                    "name": h.name,
                    "cost_price": h.cost_price,
                    "current_price": h.current_price,
                    "prev_close": h.prev_close_price,
                    "profit_pct": round(unrealized, 2),
                    "today_change_pct": round(today_change, 2),
                    "pnl_amount": round(pnl_amount, 2),
                    "today_amount": round(today_amount, 2),
                    "quantity": h.quantity,
                    "buy_date": h.buy_date,
                    "strategy": h.buy_strategy or "",
                    "type": h.holding_type or "",
                })

            # 按浮动盈亏排序
            holdings_detail.sort(key=lambda x: x["profit_pct"], reverse=True)

            # ----- 今日平仓（已实现盈亏）-----
            today_closed = session.query(Holding).filter(
                Holding.status == "CLOSED",
                Holding.updated_at >= today_str
            ).all()

            today_realized_pnl = 0.0
            today_closed_detail = []
            for h in today_closed:
                if h.cost_price > 0 and h.sell_price > 0:
                    realized = (h.sell_price - h.cost_price) / h.cost_price * 100
                    amount = (h.sell_price - h.cost_price) * h.quantity
                else:
                    realized = 0.0
                    amount = 0.0
                today_realized_pnl += amount
                today_closed_detail.append({
                    "code": h.code,
                    "name": h.name,
                    "return_pct": round(realized, 2),
                    "amount": round(amount, 2),
                    "strategy": h.buy_strategy or "",
                })

            # ----- 全部已实现盈亏（累计）-----
            all_closed = session.query(Holding).filter(Holding.status == "CLOSED").all()
            total_realized_pnl = 0.0
            total_closed_count = len(all_closed)
            closed_wins = 0
            for h in all_closed:
                if h.cost_price > 0 and h.sell_price > 0:
                    amount = (h.sell_price - h.cost_price) * h.quantity
                    total_realized_pnl += amount
                    if h.sell_price > h.cost_price:
                        closed_wins += 1

            # ----- 汇总 -----
            today_total_pnl = today_realized_pnl + total_today_pnl
            cumulative_total_pnl = total_realized_pnl + total_unrealized_pnl
            active_count = len(active)
            today_closed_count = len(today_closed)

            # 计算增减百分比（相对总成本）
            total_cost = sum(h.cost_price * h.quantity for h in active if h.cost_price > 0)
            total_cost += sum(h.cost_price * h.quantity for h in all_closed if h.cost_price > 0)
            total_pnl_pct = round(cumulative_total_pnl / total_cost * 100, 2) if total_cost > 0 else 0.0
            today_pnl_pct = round(today_total_pnl / total_cost * 100, 2) if total_cost > 0 else 0.0

            # 活跃持仓中盈利/亏损数量
            profit_count = sum(1 for h in holdings_detail if h["profit_pct"] > 0)
            loss_count = sum(1 for h in holdings_detail if h["profit_pct"] < 0)
            flat_count = active_count - profit_count - loss_count

            return {
                "date": today_str,
                "active_positions": active_count,
                "profit_count": profit_count,
                "loss_count": loss_count,
                "flat_count": flat_count,
                # 今日
                "today_total_pnl": round(today_total_pnl, 2),
                "today_total_pnl_pct": today_pnl_pct,
                "today_unrealized_pnl": round(total_today_pnl, 2),
                "today_realized_pnl": round(today_realized_pnl, 2),
                "today_closed_count": today_closed_count,
                "today_closed_trades": today_closed_detail,
                # 累计
                "cumulative_total_pnl": round(cumulative_total_pnl, 2),
                "cumulative_total_pnl_pct": total_pnl_pct,
                "total_realized_pnl": round(total_realized_pnl, 2),
                "total_unrealized_pnl": round(total_unrealized_pnl, 2),
                "total_closed_count": total_closed_count,
                "total_closed_win_rate": round(closed_wins / total_closed_count * 100, 1) if total_closed_count > 0 else 0,
                # 明细
                "holdings": holdings_detail,
            }
        except Exception as e:
            logger.error(f"生成每日盈亏报告失败: {e}")
            return {"error": str(e)}
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
                    "buy_condition": r.buy_condition,
                    "sell_condition": r.sell_condition
                }
                for r in recs
            ]
        finally:
            session.close()

    @staticmethod
    def expire_old_recommendations(before_date: str):
        """将指定日期之前的 PENDING 推荐标记为 EXPIRED"""
        session = db_manager.get_session()
        try:
            updated = session.query(Recommendation).filter(
                Recommendation.status == "PENDING",
                Recommendation.trade_date < before_date
            ).update({"status": "EXPIRED"}, synchronize_session="fetch")
            if updated:
                session.commit()
                logger.info(f"已过期 {updated} 条旧推荐标的 (早于 {before_date})")
        except Exception as e:
            session.rollback()
            logger.warning(f"过期旧推荐失败: {e}")
        finally:
            session.close()


class SentimentManager:
    """
    每日情绪历史向量数据库管理服务
    """

    @staticmethod
    def save_daily_sentiment(trade_date: str, sentiment_data: Dict[str, Any], cycle_stage: str = "", summary: str = "", total_amount: float = 0.0):
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
            record.total_amount = total_amount

            session.commit()
            logger.info(f"已保存 {trade_date} 每日情绪向量与周期结论")
        except Exception as e:
            session.rollback()
            logger.error(f"保存每日情绪数据失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_recent_sentiments(days_lookback: int = 5) -> List[Dict[str, Any]]:
        """查询最近N个交易日的情绪记录，按日期降序排列（最新的在前）"""
        import datetime
        session = db_manager.get_session()
        try:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days_lookback + 10)).strftime("%Y%m%d")
            records = session.query(DailySentiment).filter(
                DailySentiment.trade_date >= cutoff
            ).order_by(DailySentiment.trade_date.desc()).limit(days_lookback).all()
            return [
                {
                    "trade_date": r.trade_date,
                    "height": r.height,
                    "breadth": r.breadth,
                    "zt_count": r.zt_count,
                    "dt_count": r.dt_count,
                    "zhaban_count": r.zhaban_count,
                    "yield_rate": r.yield_rate,
                    "seal_force_ratio": r.seal_force_ratio,
                    "zhaban_rate": r.zhaban_rate,
                    "sentiment_index": r.sentiment_index,
                    "cycle_stage": r.cycle_stage or "",
                    "total_amount": r.total_amount,
                }
                for r in records
            ]
        finally:
            session.close()


class DragonManager:
    """
    历史龙头数据服务 (用于二波战法溯源)
    """

    @staticmethod
    def get_recent_dragons(days_lookback: int = 30) -> List[Dict[str, Any]]:
        """获取近 N 天内的人气总龙头"""
        import datetime
        session = db_manager.get_session()
        try:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days_lookback)).strftime("%Y%m%d")
            dragons = session.query(HistoricDragon).filter(
                HistoricDragon.is_active == True,
                HistoricDragon.peak_date >= cutoff
            ).all()
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


class MarketIndexManager:
    """大盘指数日线数据管理"""

    @staticmethod
    def save_daily_index(trade_date: str, spot_df=None):
        """
        保存当日大盘指数数据。
        优先从 akshare 获取真实指数，失败则从全市场快照估算。
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

            sh_close, sh_change = MarketIndexManager._fetch_index("sh000001")
            sz_close, sz_change = MarketIndexManager._fetch_index("sz399001")
            gem_close, gem_change = MarketIndexManager._fetch_index("sz399006")

            total_amt = 0.0
            if spot_df is not None and not spot_df.empty and "amount" in spot_df.columns:
                total_amt = round(float(spot_df["amount"].sum()) / 1e8, 2)

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
    def _fetch_index(symbol: str) -> tuple:
        """获取单个指数最新收盘价和涨跌幅，失败返回 (0, 0)"""
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
        return 0.0, 0.0

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

            total_equity = 1_000_000 + pnl_report.get("cumulative_total_pnl", 0)
            snapshot = DailyEquitySnapshot(
                trade_date=trade_date,
                total_equity=round(total_equity, 2),
                available_cash=round(total_equity - pnl_report.get("total_unrealized_pnl", 0), 2),
                position_value=round(total_equity - (total_equity - pnl_report.get("total_unrealized_pnl", 0)), 2),
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
            records = session.query(DailyEquitySnapshot).order_by(
                DailyEquitySnapshot.trade_date.asc()
            ).limit(days).all() if days <= 60 else session.query(DailyEquitySnapshot).order_by(
                DailyEquitySnapshot.trade_date.asc()
            ).all()
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


class SystemLogManager:
    """系统运行日志管理服务"""

    @staticmethod
    def add_log(log_date: str, category: str, title: str, detail: str = ""):
        """新增一条系统运行日志"""
        session = db_manager.get_session()
        try:
            log = SystemLog(log_date=log_date, category=category, title=title, detail=detail)
            session.add(log)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"保存系统日志失败: {e}")
        finally:
            session.close()

    @staticmethod
    def get_logs(log_date: Optional[str] = None, category: Optional[str] = None,
                 limit: int = 50) -> List[Dict[str, Any]]:
        """查询系统运行日志"""
        session = db_manager.get_session()
        try:
            query = session.query(SystemLog).order_by(SystemLog.created_at.desc())
            if log_date:
                query = query.filter(SystemLog.log_date == log_date)
            if category:
                query = query.filter(SystemLog.category == category)
            logs = query.limit(limit).all()
            return [
                {"id": l.id, "log_date": l.log_date, "category": l.category,
                 "title": l.title, "detail": l.detail[:500] if l.detail else "",
                 "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else ""}
                for l in logs
            ]
        finally:
            session.close()


class LogRetentionCleaner:
    """
    日志保留策略清理器
    - system_logs / error_logs / llm_logs: 保留 15 天
    - push_logs: 保留 30 天
    """

    @staticmethod
    def cleanup():
        """清理所有过期日志，每日调用一次"""
        import datetime
        today = datetime.date.today()
        cutoff_15 = (today - datetime.timedelta(days=15)).isoformat()
        cutoff_30 = (today - datetime.timedelta(days=30)).isoformat()

        session = db_manager.get_session()
        try:
            from sqlalchemy import func
            # 15 天保留的表：用 func.date() 做日期级别比较，避免字符串拼接
            for model, label in [(SystemLog, "system_logs"),
                                  (ErrorLog, "error_logs"),
                                  (LLMLog, "llm_logs")]:
                deleted = session.query(model).filter(
                    func.date(model.created_at) < cutoff_15
                ).delete(synchronize_session="fetch")
                if deleted:
                    logger.info(f"日志清理: {label} 删除 {deleted} 条 (>15天)")

            # 30 天保留
            deleted_push = session.query(PushLog).filter(
                func.date(PushLog.created_at) < cutoff_30
            ).delete(synchronize_session="fetch")
            if deleted_push:
                logger.info(f"日志清理: push_logs 删除 {deleted_push} 条 (>30天)")

            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"日志清理失败: {e}")
        finally:
            session.close()
