import logging
from typing import Dict, Any, Optional, List
from database.models import Holding
from database.connection import db_manager
logger = logging.getLogger(__name__)

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
                    "was_limit_up_today": h.was_limit_up_today,
                    "prev_close_price": h.prev_close_price,
                    "change_pct": h.change_pct or 0,
                    "today_change": h.change_pct or 0,  # 股票当日市场涨跌幅（监控实时更新），非成本盈亏
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
        holding_type: str = "MANUAL",
        decision_source: str = "rule"
    ) -> bool:
        """添加新持仓记录 (若未传入名称则自动匹配)。decision_source: B方案 决策来源 llm/rule。"""
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
                prev_close_price=cost_price,  # 买入日以成本为"昨收"基准，当日盈亏=(今收-成本)/成本
                profit_rate=0.0,
                quantity=quantity,
                buy_date=buy_dt,
                buy_strategy=strategy,
                holding_type=holding_type,
                decision_source=decision_source,
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
    def batch_update_profit_rates(updates: List[tuple]):
        """
        批量更新持仓当前价/当日涨跌幅/收益率（一次 session 写库）。
        盘中轮询每 15 秒调用一次，避免逐只开 session 造成的写放大。
        :param updates: [(code, current_price, holding_type, change_pct), ...]
        """
        if not updates:
            return
        session = db_manager.get_session()
        try:
            for item in updates:
                code, current_price, holding_type = item[0], item[1], item[2]
                change_pct = item[3] if len(item) > 3 else 0.0
                query = session.query(Holding).filter(
                    Holding.code == code, Holding.status == "HOLDING"
                )
                if holding_type:
                    query = query.filter(Holding.holding_type == holding_type)
                holding = query.first()
                if holding and holding.cost_price > 0:
                    holding.current_price = current_price
                    holding.change_pct = change_pct  # 股票当日市场涨跌幅
                    holding.profit_rate = round(((current_price - holding.cost_price) / holding.cost_price) * 100, 2)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"批量更新持仓收益率失败: {e}")
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
    def update_current_prices(spot_map: Dict[str, float]):
        """
        盘后用当日收盘价更新所有活跃持仓的 current_price 与收益率。
        与 sync_close_prices 的区别：不改动 prev_close_price（保留"上一交易日收盘"基准，
        供当日盈亏报告计算"今日涨跌"）。报告生成后应再调用 sync_close_prices 滚存昨收。
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
                    h.current_price = close_px
                    if h.cost_price > 0:
                        h.profit_rate = round((close_px - h.cost_price) / h.cost_price * 100, 2)
                    updated += 1
            if updated:
                session.commit()
                logger.info(f"更新 {updated} 只持仓收盘价")
        except Exception as e:
            session.rollback()
            logger.error(f"更新持仓收盘价失败: {e}")
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

            # ----- 当前持仓（浮动盈亏，仅 AI 自动持仓——手动持仓只用于监控不入报告）-----
            active = session.query(Holding).filter(
                Holding.status == "HOLDING",
                Holding.holding_type.in_(["AI_AUTO", "AI_TAIL", "AI_SW"])
            ).all()
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

            # ----- 今日平仓（已实现盈亏，仅 AI 自动持仓）-----
            today_closed = session.query(Holding).filter(
                Holding.status == "CLOSED",
                Holding.holding_type.in_(["AI_AUTO", "AI_TAIL", "AI_SW"]),
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

            # ----- 全部已实现盈亏（累计，仅 AI 自动持仓）-----
            all_closed = session.query(Holding).filter(
                Holding.status == "CLOSED",
                Holding.holding_type.in_(["AI_AUTO", "AI_TAIL", "AI_SW"])
            ).all()
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

            # 收益率分母区分：今日用"当前活跃持仓成本"，累计用"累计投入成本"，避免历史平仓稀释今日收益率
            active_cost = sum(h.cost_price * h.quantity for h in active if h.cost_price > 0)
            total_cost = active_cost + sum(h.cost_price * h.quantity for h in all_closed if h.cost_price > 0)
            total_pnl_pct = round(cumulative_total_pnl / total_cost * 100, 2) if total_cost > 0 else 0.0
            today_pnl_pct = round(today_total_pnl / active_cost * 100, 2) if active_cost > 0 else 0.0

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
