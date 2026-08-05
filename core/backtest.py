"""
AI 自动买卖策略回测引擎 v2

模拟 AI 的完整决策链：
  买入：涨停池 → AI 过滤规则(排除板块/量比/中军) → 仓位管理 → 买入
  卖出：HoldingMonitor 规则(止损/止盈/时间止损/断板) → 卖出

与 v1 的区别：
  - v1: 无脑买涨停池 + N 天后卖，和 AI 实际逻辑完全无关
  - v2: 走 AI 实际使用的过滤链和风控规则，每日逐笔跟踪持仓
"""

import logging
import datetime
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np

from data.fetcher import DataFetcher
from config.settings import settings

logger = logging.getLogger(__name__)


class AIBacktestEngine:
    """
    AI 自动买卖策略回测引擎

    模拟流程（每个交易日）：
      1. 卖出检查：遍历持仓，用 HoldingMonitor 规则判定是否离场
      2. 熔断检查：检查当日持仓平均亏损是否触发熔断
      3. 买入检查：从涨停池筛选标的，应用 AI 过滤链，模拟买入
      4. 记录每日持仓浮动盈亏
    """

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    @staticmethod
    def run(
        start_date: str = "20260701",
        end_date: str = "20260730",
        max_positions: int = None,
        max_daily_buys: int = None,
        slippage: float = 0.3,
    ) -> dict:
        """
        :param start_date:   回测起始日期 YYYYMMDD
        :param end_date:     回测结束日期 YYYYMMDD
        :param max_positions: 最大持仓数（默认取 settings.MAX_AI_POSITIONS）
        :param max_daily_buys:每日最大买入笔数（默认取 settings.MAX_DAILY_BUYS）
        :param slippage:      买卖滑点 (%)
        """
        if max_positions is None:
            max_positions = settings.MAX_AI_POSITIONS
        if max_daily_buys is None:
            max_daily_buys = settings.MAX_DAILY_BUYS

        # 用 A 股交易日历而非工作日(freq="B")，否则长假会把非交易日计入持仓天数/净值
        date_list = AIBacktestEngine._build_trade_date_list(start_date, end_date)
        if not date_list:
            return {
                "total_trades": 0,
                "message": f"回测区间 {start_date}~{end_date} 无交易日（日历获取失败或区间为空）",
                "trading_days": 0,
            }

        positions: List[Dict[str, Any]] = []   # 当前持仓
        closed_trades: List[Dict[str, Any]] = []  # 已平仓交易
        daily_equity: List[Dict[str, Any]] = []   # 每日净值曲线
        cash = 1_000_000.0  # 初始资金 100 万（虚拟）
        available_cash = cash
        circuit_breaker_triggered_dates = set()

        for i, date_str in enumerate(date_list):
            # ------------------------------------------------------------------
            # 0. 获取当日市场数据
            # ------------------------------------------------------------------
            day_data = AIBacktestEngine._get_day_data(date_str, date_list, i, positions)
            if day_data is None:
                continue

            # ------------------------------------------------------------------
            # 1. 卖出检查：遍历当前持仓
            # ------------------------------------------------------------------
            positions, closed, available_cash = AIBacktestEngine._process_sells(
                positions, date_str, day_data, available_cash, slippage
            )
            closed_trades.extend(closed)

            # ------------------------------------------------------------------
            # 2. 熔断检查：AI 持仓当日平均亏损
            # ------------------------------------------------------------------
            if AIBacktestEngine._check_circuit_breaker(positions, date_str, day_data):
                circuit_breaker_triggered_dates.add(date_str)
                # 记录净值后跳过买入
                equity = AIBacktestEngine._calc_equity(
                    positions, available_cash, day_data, date_str
                )
                daily_equity.append(equity)
                continue

            # ------------------------------------------------------------------
            # 3. 买入检查
            # ------------------------------------------------------------------
            if len(positions) < max_positions:
                buys_today = min(max_daily_buys, max_positions - len(positions))
                new_positions, available_cash = AIBacktestEngine._process_buys(
                    date_str, buys_today, available_cash, day_data, slippage
                )
                positions.extend(new_positions)

            # ------------------------------------------------------------------
            # 4. 记录每日净值
            # ------------------------------------------------------------------
            equity = AIBacktestEngine._calc_equity(
                positions, available_cash, day_data, date_str
            )
            daily_equity.append(equity)

        # ------------------------------------------------------------------
        # 5. 强制平仓：回测最后一天清掉所有持仓
        # ------------------------------------------------------------------
        if positions:
            last_date = date_list[-1]
            last_data = AIBacktestEngine._get_day_data(last_date, date_list, len(date_list) - 1, positions)
            for pos in positions:
                close_px = AIBacktestEngine._get_close_from_data(
                    pos["code"], last_date, last_data
                )
                if close_px is None:
                    sell_price = pos["cost_price"]
                else:
                    sell_price = close_px * (1 - slippage / 100)  # 与常规卖出一致，只扣一次卖滑点
                ret = AIBacktestEngine._calc_return(pos["cost_price"], sell_price)
                closed_trades.append({
                    "code": pos["code"],
                    "name": pos["name"],
                    "buy_date": pos["buy_date"],
                    "sell_date": last_date,
                    "buy_price": pos["cost_price"],
                    "sell_price": sell_price,
                    "return_pct": ret,
                    "hold_days": pos.get("hold_days", 1),
                    "strategy": pos.get("strategy", ""),
                })
            positions = []

        # ------------------------------------------------------------------
        # 6. 汇总统计
        # ------------------------------------------------------------------
        return AIBacktestEngine._summarize(
            closed_trades, daily_equity, date_list,
            start_date, end_date, max_positions, max_daily_buys,
            circuit_breaker_triggered_dates,
        )

    @staticmethod
    def _build_trade_date_list(start_date: str, end_date: str) -> List[str]:
        """构建回测区间内的 A 股交易日列表（akshare 交易日历，失败回退工作日）"""
        import datetime
        try:
            import akshare as ak
            cal = ak.tool_trade_date_hist_sina()
            if cal is not None and not cal.empty:
                d0 = datetime.datetime.strptime(start_date, "%Y%m%d").date()
                d1 = datetime.datetime.strptime(end_date, "%Y%m%d").date()
                days = [d.date() for d in pd.to_datetime(cal["trade_date"]) if d0 <= d.date() <= d1]
                if days:
                    return [d.strftime("%Y%m%d") for d in days]
        except Exception as e:
            logger.warning(f"获取交易日历失败，回退到工作日: {e}")
        return [d.strftime("%Y%m%d") for d in pd.date_range(start=start_date, end=end_date, freq="B")]

    # ------------------------------------------------------------------
    # 卖出逻辑
    # ------------------------------------------------------------------

    @staticmethod
    def _process_sells(
        positions: list,
        date_str: str,
        day_data: dict,
        available_cash: float,
        slippage: float,
    ) -> tuple:
        """检查所有持仓，触发卖出条件的平仓"""
        remaining = []
        closed = []

        for pos in positions:
            code = pos["code"]
            close_price = AIBacktestEngine._get_close_from_data(code, date_str, day_data)
            if close_price is None:
                remaining.append(pos)
                continue

            pos["hold_days"] = pos.get("hold_days", 0) + 1
            profit_pct = round((close_price - pos["cost_price"]) / pos["cost_price"] * 100, 2)

            # 更新当日浮动
            pos["current_price"] = close_price
            pos["profit_pct"] = profit_pct

            sell_reason = None

            # 规则 0：绝对止损（最高优先级）
            if profit_pct <= settings.ABSOLUTE_STOP_LOSS_PCT:
                sell_reason = f"绝对止损({profit_pct}% <= {settings.ABSOLUTE_STOP_LOSS_PCT}%)"

            # 规则 1：断板必卖（审计③修复，对齐实盘 CRITICAL 规则）——
            # 连板(打板接力)股今日不在涨停池 = 炸板，不再等待回封
            elif ("打板接力" in pos.get("strategy", "")
                  and AIBacktestEngine._not_in_zt(code, day_data)):
                sell_reason = f"断板必卖(连板股{pos.get('strategy','')}今日未封板)"

            # 规则 2：破位止损（审计③修复，对齐实盘 HIGH 规则）——
            # 收盘跌破 5 日均线（无分时 VWAP，用 MA5 近似）
            elif (day_data.get("ma5_cache", {}).get(str(code)) is not None
                  and close_price < day_data["ma5_cache"][str(code)]):
                ma5_val = day_data["ma5_cache"][str(code)]
                sell_reason = f"破位止损(收盘{close_price:.2f} < MA5 {ma5_val:.2f})"

            # 规则 3：强止盈
            elif profit_pct >= settings.TAKE_PROFIT_CRITICAL_PCT:
                sell_reason = f"强止盈({profit_pct}% >= {settings.TAKE_PROFIT_CRITICAL_PCT}%)"

            # 规则 4：时间止损
            elif pos["hold_days"] >= settings.TIME_STOP_LOSS_DAYS and profit_pct <= 0:
                sell_reason = f"时间止损(持仓{pos['hold_days']}天仍亏损)"


            if sell_reason:
                sell_price = close_price * (1 - slippage / 100)
                ret = AIBacktestEngine._calc_return(pos["cost_price"], sell_price)
                closed.append({
                    "code": code,
                    "name": pos["name"],
                    "buy_date": pos["buy_date"],
                    "sell_date": date_str,
                    "buy_price": pos["cost_price"],
                    "sell_price": round(sell_price, 2),
                    "return_pct": ret,
                    "hold_days": pos["hold_days"],
                    "strategy": pos.get("strategy", ""),
                    "reason": sell_reason,
                })
                # 释放资金
                available_cash += sell_price * 100  # 假设每笔 100 股（简化）
                logger.debug(f"  卖出: {pos['name']}({code}) {sell_reason} 收益:{ret}%")
            else:
                remaining.append(pos)

        return remaining, closed, available_cash

    # ------------------------------------------------------------------
    # 买入逻辑
    # ------------------------------------------------------------------

    @staticmethod
    def _process_buys(
        date_str: str,
        max_buys: int,
        available_cash: float,
        day_data: dict,
        slippage: float,
    ) -> tuple:
        """从涨停池筛选标的并模拟买入"""
        new_positions = []

        # 取涨停池
        zt_df = day_data.get("zt_df")
        if zt_df is None or zt_df.empty:
            return new_positions, available_cash

        # 过滤：排除科创板/北交所/ST
        zt_df = AIBacktestEngine._filter_zt_pool(zt_df, day_data)

        if zt_df.empty:
            return new_positions, available_cash

        # 按连板数降序排列，优先买龙头
        if "lbc" in zt_df.columns:
            zt_df = zt_df.sort_values("lbc", ascending=False)

        bought = 0
        for _, row in zt_df.iterrows():
            if bought >= max_buys:
                break

            code = str(row.get("code", ""))
            name = str(row.get("name", ""))
            # 审计④修复：NaN/空值防御，坏数据行跳过而不是让整个回测崩溃
            try:
                buy_price = float(row.get("price", 0))
            except (TypeError, ValueError):
                buy_price = 0.0
            if pd.isna(buy_price):
                buy_price = 0.0
            lbc_raw = row.get("lbc", 1) if "lbc" in row.index else 1
            try:
                lbc = int(float(lbc_raw)) if pd.notna(lbc_raw) else 1
            except (TypeError, ValueError):
                lbc = 1

            if buy_price <= 0 or not code or pd.isna(buy_price):
                continue

            # 买入成本（含滑点）
            cost = buy_price * (1 + slippage / 100)

            # 简化：假设每只买 100 股
            shares = 100
            required = cost * shares
            if required > available_cash:
                continue

            available_cash -= required

            # 打标签
            strategy_tag = f"打板接力-{lbc}连板" if lbc >= 2 else "首板低吸"

            new_positions.append({
                "code": code,
                "name": name,
                "buy_date": date_str,
                "cost_price": round(cost, 2),
                "current_price": buy_price,
                "hold_days": 0,
                "profit_pct": 0.0,
                "strategy": strategy_tag,
                "lbc": lbc,
            })
            bought += 1
            logger.debug(f"  买入: {name}({code}) {strategy_tag} @ {cost:.2f}")

        return new_positions, available_cash

    # ------------------------------------------------------------------
    # 过滤规则（与 AI 实际过滤链保持一致）
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_zt_pool(zt_df: pd.DataFrame, day_data: dict) -> pd.DataFrame:
        """复现 AI 的源头过滤规则"""
        if zt_df is None or zt_df.empty:
            return pd.DataFrame()

        df = zt_df.copy()
        code_series = df["code"].astype(str)

        # 排除科创板 (688)
        if settings.EXCLUDE_STAR_MARKET:
            df = df[~code_series.str.startswith("688")]

        # 排除北交所 (8开头/43/83/87/920)
        if settings.EXCLUDE_BSE:
            df = df[~code_series.str.match(r"^(8|43|83|87|92)")]

        # 排除 ST
        if settings.EXCLUDE_ST:
            name_series = df["name"].astype(str)
            df = df[~name_series.str.contains("ST", case=False)]

        # 量比过滤：至少满足 VOL_BURST_THRESHOLD（用涨停池自带数据近似）
        spot_df = day_data.get("spot_df")
        if spot_df is not None and not spot_df.empty and "volume_ratio" in spot_df.columns:
            spot_map = {}
            for _, srow in spot_df.iterrows():
                c = str(srow.get("code", ""))
                vr = float(srow.get("volume_ratio", 1.0))
                if c:
                    spot_map[c] = vr
            if spot_map:
                df["_vr"] = df["code"].astype(str).map(spot_map).fillna(1.0)
                df = df[df["_vr"] >= settings.VOL_BURST_THRESHOLD]

        return df

    # ------------------------------------------------------------------
    # 熔断检查
    # ------------------------------------------------------------------

    @staticmethod
    def _check_circuit_breaker(
        positions: list, date_str: str, day_data: dict
    ) -> bool:
        """AI 持仓当日平均亏损是否触发熔断"""
        if not positions:
            return False

        total_profit = 0.0
        count = 0
        for pos in positions:
            code = pos["code"]
            close_price = AIBacktestEngine._get_close_from_data(code, date_str, day_data)
            if close_price is not None:
                profit = (close_price - pos["cost_price"]) / pos["cost_price"] * 100
                total_profit += profit
                count += 1

        if count == 0:
            return False

        avg_profit = total_profit / count
        return avg_profit <= settings.DAILY_LOSS_CIRCUIT_BREAKER

    # ------------------------------------------------------------------
    # 净值计算
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_equity(
        positions: list, available_cash: float, day_data: dict, date_str: str
    ) -> dict:
        """计算当日总净值"""
        position_value = 0.0
        unrealized_pnl = 0.0

        for pos in positions:
            code = pos["code"]
            close_price = AIBacktestEngine._get_close_from_data(code, date_str, day_data)
            if close_price is not None:
                shares = 100  # 与买入逻辑保持一致
                position_value += close_price * shares
                unrealized_pnl += (close_price - pos["cost_price"]) * shares

        total_equity = available_cash + position_value

        return {
            "date": date_str,
            "total_equity": round(total_equity, 2),
            "available_cash": round(available_cash, 2),
            "position_value": round(position_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "position_count": len(positions),
        }

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------

    @staticmethod
    def _get_day_data(
        date_str: str, date_list: list, idx: int, positions: list
    ) -> Optional[dict]:
        """获取回测当天所需数据并缓存"""
        # 涨停池（买入候选）
        try:
            zt_df = DataFetcher.get_zt_pool(date_str=date_str)
        except Exception:
            zt_df = pd.DataFrame()

        # 全市场快照（用于量比补充）
        # 历史快照不可得，尝试用当日涨停池自带的量比字段
        # 如果涨停池本身含 volume_ratio 则直接用
        spot_df = zt_df if (zt_df is not None and not zt_df.empty) else pd.DataFrame()

        # 需要查收盘价的股票列表：当前持仓 + 涨停池
        codes_to_fetch = set()
        for pos in positions:
            codes_to_fetch.add(pos["code"])
        if zt_df is not None and not zt_df.empty:
            for c in zt_df["code"].astype(str).head(20):
                codes_to_fetch.add(c)

        # 批量获取历史日线（收盘价 + MA5，一次请求，审计③破位止损用）
        close_cache = {}
        ma5_cache = {}
        for code in codes_to_fetch:
            close, ma5 = AIBacktestEngine._fetch_close_and_ma5(code, date_str)
            if close is not None:
                close_cache[code] = close
            if ma5 is not None:
                ma5_cache[code] = ma5

        return {
            "date_str": date_str,
            "zt_df": zt_df,
            "spot_df": spot_df,
            "close_cache": close_cache,
            "ma5_cache": ma5_cache,
        }

    @staticmethod
    def _get_close_from_data(code: str, date_str: str, day_data: dict) -> Optional[float]:
        """从当日数据中取收盘价，缓存未命中则实时查询"""
        cache = day_data.get("close_cache", {})
        code_str = str(code)
        if code_str in cache:
            return cache[code_str]
        # 缓存未命中，实时查
        price = AIBacktestEngine._fetch_close_price(code_str, date_str)
        if price is not None:
            cache[code_str] = price
        return price

    @staticmethod
    def _fetch_close_price(code: str, date_str: str) -> Optional[float]:
        """获取个股历史收盘价（日线），失败不影响主流程"""
        close, _ = AIBacktestEngine._fetch_close_and_ma5(code, date_str)
        return close

    @staticmethod
    def _fetch_close_and_ma5(code: str, date_str: str):
        """一次日线请求返回 (收盘价, MA5)。审计③修复：破位止损需要 MA5。失败返回 (None, None)。"""
        try:
            import akshare as ak
            end_dt = datetime.datetime.strptime(date_str, "%Y%m%d")
            start_dt = end_dt - datetime.timedelta(days=10)
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=date_str, adjust="qfq"
            )
            if df is not None and not df.empty:
                close_col = "收盘" if "收盘" in df.columns else (
                    df.columns[2] if len(df.columns) > 2 else df.columns[0]
                )
                closes = pd.to_numeric(df[close_col], errors="coerce").dropna()
                if closes.empty:
                    return None, None
                close = float(closes.iloc[-1])
                ma5 = float(closes.tail(5).mean()) if len(closes) >= 5 else None
                return close, ma5
        except Exception:
            pass
        return None, None

    @staticmethod
    def _not_in_zt(code: str, day_data: dict) -> bool:
        """该股今日是否不在涨停池（无涨停池数据时保守返回 False，不误断板）"""
        zt = day_data.get("zt_df")
        if zt is None or zt.empty or "code" not in zt.columns:
            return False
        return str(code) not in set(zt["code"].astype(str))

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_return(buy_price: float, sell_price: float) -> float:
        """
        计算收益率 (%)。
        buy_price 传入的是已含买滑点的成本价，sell_price 传入的是已含卖滑点的卖出价，
        因此这里直接按实际现金口径计算，不再重复应用滑点。
        """
        if buy_price <= 0:
            return 0.0
        return round((sell_price - buy_price) / buy_price * 100, 2)

    # ------------------------------------------------------------------
    # 汇总统计
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(
        closed_trades: list,
        daily_equity: list,
        date_list: list,
        start_date: str,
        end_date: str,
        max_positions: int,
        max_daily_buys: int,
        circuit_breaker_dates: set,
    ) -> dict:
        """生成回测汇总报告"""
        if not closed_trades:
            return {
                "total_trades": 0,
                "message": f"回测区间 {start_date}~{end_date} 无有效交易",
                "trading_days": len(date_list),
            }

        total_trades = len(closed_trades)
        wins = sum(1 for t in closed_trades if t["return_pct"] > 0)
        win_rate = round(wins / total_trades * 100, 2)
        returns = [t["return_pct"] for t in closed_trades]
        avg_ret = round(sum(returns) / len(returns), 2)
        max_ret = round(max(returns), 2)
        min_ret = round(min(returns), 2)

        # 最大回撤（基于每日净值）
        max_drawdown = 0.0
        if daily_equity:
            peak = daily_equity[0]["total_equity"]
            for e in daily_equity:
                peak = max(peak, e["total_equity"])
                dd = (peak - e["total_equity"]) / peak * 100 if peak > 0 else 0
                max_drawdown = max(max_drawdown, dd)

        # 按策略分组统计
        strategy_stats = {}
        for t in closed_trades:
            s = t.get("strategy", "未知")
            if s not in strategy_stats:
                strategy_stats[s] = {"trades": 0, "wins": 0, "returns": []}
            strategy_stats[s]["trades"] += 1
            if t["return_pct"] > 0:
                strategy_stats[s]["wins"] += 1
            strategy_stats[s]["returns"].append(t["return_pct"])

        strategy_summary = {}
        for s, st in strategy_stats.items():
            strategy_summary[s] = {
                "trades": st["trades"],
                "win_rate_pct": round(st["wins"] / st["trades"] * 100, 2),
                "avg_return_pct": round(sum(st["returns"]) / len(st["returns"]), 2),
            }

        # 按卖出原因分组
        reason_stats = {}
        for t in closed_trades:
            r = t.get("reason", "强制平仓")
            if r not in reason_stats:
                reason_stats[r] = {"count": 0, "returns": []}
            reason_stats[r]["count"] += 1
            reason_stats[r]["returns"].append(t["return_pct"])
        reason_summary = {
            r: {"count": st["count"], "avg_return_pct": round(sum(st["returns"]) / len(st["returns"]), 2)}
            for r, st in reason_stats.items()
        }

        # 总收益（相对初始资金 100 万）
        total_return = round(
            (daily_equity[-1]["total_equity"] - 1_000_000) / 1_000_000 * 100, 2
        ) if daily_equity else 0.0

        # 盈亏分布
        profit_ranges = {
            ">= 20%": sum(1 for r in returns if r >= 20),
            "10~20%": sum(1 for r in returns if 10 <= r < 20),
            "5~10%": sum(1 for r in returns if 5 <= r < 10),
            "0~5%": sum(1 for r in returns if 0 < r < 5),
            "-5~0%": sum(1 for r in returns if -5 < r <= 0),
            "-10~-5%": sum(1 for r in returns if -10 < r <= -5),
            "< -10%": sum(1 for r in returns if r < -10),
        }

        # Top/Bottom
        sorted_trades = sorted(closed_trades, key=lambda x: x["return_pct"], reverse=True)
        top5 = sorted_trades[:5]
        bottom5 = sorted_trades[-5:]

        logger.info(
            f"AI 回测完成: {start_date}~{end_date} "
            f"交易={total_trades}笔 胜率={win_rate}% "
            f"均收益={avg_ret}% 总收益={total_return}% 最大回撤={round(max_drawdown, 2)}%"
        )

        return {
            "total_trades": total_trades,
            "win_rate_pct": win_rate,
            "avg_return_pct": avg_ret,
            "max_return_pct": max_ret,
            "min_return_pct": min_ret,
            "total_return_pct": total_return,
            "max_drawdown_pct": round(max_drawdown, 2),
            "max_positions": max_positions,
            "max_daily_buys": max_daily_buys,
            "period": f"{start_date}~{end_date}",
            "trading_days": len(date_list),
            "circuit_breaker_days": len(circuit_breaker_dates),
            "strategy_breakdown": strategy_summary,
            "sell_reason_breakdown": reason_summary,
            "profit_distribution": profit_ranges,
            "top5": [{"code": t["code"], "name": t["name"], "return_pct": t["return_pct"],
                      "hold_days": t["hold_days"], "reason": t.get("reason", ""),
                      "strategy": t.get("strategy", "")} for t in top5],
            "bottom5": [{"code": t["code"], "name": t["name"], "return_pct": t["return_pct"],
                         "hold_days": t["hold_days"], "reason": t.get("reason", ""),
                         "strategy": t.get("strategy", "")} for t in bottom5],
            "daily_equity": daily_equity[-20:],  # 最近 20 天净值
            "trades_sample": sorted_trades[:30],  # 前 30 笔明细
        }
