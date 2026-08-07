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
from core.signal_flags import compute_signal_flags, signal_labels
from data.core import socket_timeout

logger = logging.getLogger(__name__)

# 信号回测策略桶：四类抢筹信号 + 尾盘博弈（独立子回测，互不干扰，胜率横向对比）
SIGNAL_STRATEGIES = ["点火异动", "逼近封板", "低开猛拉", "振幅放量", "尾盘博弈"]
SIGNAL_TO_COL = {
    "点火异动": "_signal_burst",
    # 逼近封板用盘中最高涨幅近似（收盘口径会漏掉盘中逼近后封板/回落的股票，样本严重偏少）
    "逼近封板": "_signal_near_limit_intraday",
    "低开猛拉": "_signal_low_open_rally",
    "振幅放量": "_signal_amplitude",
    "尾盘博弈": "_signal_tail_game",
}


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
        mode: str = "zt",
    ) -> dict:
        """
        :param start_date:   回测起始日期 YYYYMMDD
        :param end_date:     回测结束日期 YYYYMMDD
        :param max_positions: 最大持仓数（默认取 settings.MAX_AI_POSITIONS；mode=signals 时不限）
        :param max_daily_buys:每日最大买入笔数（默认取 settings.MAX_DAILY_BUYS；mode=signals 时不限）
        :param slippage:      买卖滑点 (%)
        :param mode:          "zt"=涨停池买连板龙头（旧行为）；"signals"=四类信号+尾盘博弈胜率对比
        """
        if mode == "signals":
            return AIBacktestEngine._run_signals(start_date, end_date, slippage)
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

    # ------------------------------------------------------------------
    # 信号回测模式（mode="signals"）：四类信号 + 尾盘博弈 胜率对比
    # ------------------------------------------------------------------

    @staticmethod
    def _run_signals(start_date: str, end_date: str, slippage: float = 0.3) -> dict:
        """
        信号模式入口：5 个独立子回测（四类信号 + 尾盘博弈）各跑一遍，
        去掉预算（无上限买入，只看胜率/收益率%），合并已平仓交易统计对比。
        """
        date_list = AIBacktestEngine._build_trade_date_list(start_date, end_date)
        if not date_list:
            return {"total_trades": 0,
                    "message": f"回测区间 {start_date}~{end_date} 无交易日（日历获取失败或区间为空）",
                    "trading_days": 0}
        from data.kline_etl import KlineEtl
        kline_cache = KlineEtl.load_cache(start_date, end_date)
        if not kline_cache:
            return {"total_trades": 0,
                    "message": ("日线缓存缺失，请先执行 ETL: "
                                "python -m data.kline_etl --start "
                                f"{start_date} --end {end_date}"),
                    "trading_days": len(date_list)}
        all_trades = []
        for strategy in SIGNAL_STRATEGIES:
            all_trades.extend(
                AIBacktestEngine._run_signal_strategy(date_list, kline_cache, strategy, slippage))
        return AIBacktestEngine._summarize(
            all_trades, [], date_list, start_date, end_date,
            10 ** 6, 10 ** 6, set(), signal_mode=True)

    @staticmethod
    def _run_signal_strategy(date_list: list, kline_cache: dict, strategy: str,
                             slippage: float) -> list:
        """单策略逐日回测：买入该策略信号候选，卖出（尾盘博弈次日兑现 / 其余现有规则），返回已平仓交易。"""
        positions: list = []
        closed: list = []
        for i, date_str in enumerate(date_list):
            day_data = AIBacktestEngine._get_signal_day_data(
                date_str, date_list, i, positions, kline_cache)
            if day_data is None:
                continue
            if strategy == "尾盘博弈":
                positions, c2, _ = AIBacktestEngine._process_tail_game_sells(
                    positions, date_str, day_data, 1e12, slippage)
            else:
                positions, c2, _ = AIBacktestEngine._process_sells(
                    positions, date_str, day_data, 1e12, slippage)
            closed.extend(c2)
            positions.extend(
                AIBacktestEngine._process_signal_buys(date_str, strategy, day_data, slippage))

        # 末日后强制平仓（沿用旧口径，收盘价卖出）
        last_date = date_list[-1]
        last_data = AIBacktestEngine._get_signal_day_data(
            last_date, date_list, len(date_list) - 1, positions, kline_cache)
        for pos in positions:
            close_px = AIBacktestEngine._get_close_from_data(
                pos["code"], last_date, last_data) if last_data else None
            sell_price = (close_px or pos["cost_price"]) * (1 - slippage / 100)
            closed.append({
                "code": pos["code"], "name": pos.get("name", pos["code"]),
                "buy_date": pos["buy_date"], "sell_date": last_date,
                "buy_price": pos["cost_price"], "sell_price": round(sell_price, 2),
                "return_pct": AIBacktestEngine._calc_return(pos["cost_price"], sell_price),
                "hold_days": pos.get("hold_days", 1), "strategy": strategy,
                "reason": "强制平仓",
            })
        return closed

    @staticmethod
    def _get_signal_day_data(date_str: str, date_list: list, idx: int,
                             positions: list, kline_cache: dict) -> Optional[dict]:
        """信号模式当日数据：日线组装 spot → 计算信号列 → 排除 688/BSE/ST → 补 close/MA5 缓存。"""
        from data.kline_etl import KlineEtl
        spot_df = KlineEtl.build_day_spot(kline_cache, date_str)
        if spot_df is None or spot_df.empty:
            return None
        spot_df = compute_signal_flags(spot_df)
        # 盘中逼近封板：用当日最高涨幅 high_chg 近似（收盘口径漏掉盘中逼近后封板/回落的股票）
        if "high_chg" in spot_df.columns:
            # 非一字板过滤：开盘涨幅 < 涨停线（一字板开盘即涨停买不进，逼近封板只针对能买进的强势票）
            _main = spot_df["code"].astype(str).str.match(r"^(60|00)")
            _open_chg = (spot_df["open"].astype(float) - spot_df["pre_close"].astype(float)) \
                        / spot_df["pre_close"].astype(float) * 100
            _oneseal = ((_main & (_open_chg >= settings.MAIN_BOARD_LIMIT_PCT)) |
                        (~_main & (_open_chg >= settings.GEM_STAR_LIMIT_PCT)))
            spot_df["_signal_near_limit_intraday"] = (
                (spot_df["high_chg"] >= spot_df["_near_limit_min"]) &
                (spot_df["high_chg"] <= spot_df["_near_limit_max"]) &
                (spot_df["volume_ratio"] > settings.NEAR_LIMIT_VOL_RATIO) &
                (~_oneseal))
        else:
            spot_df["_signal_near_limit_intraday"] = spot_df["_signal_near_limit"]
        # 排除科创板/北交所/ST（对齐实盘源头过滤，不用 _filter_zt_pool 以免其量比过滤干扰信号）
        if settings.EXCLUDE_STAR_MARKET:
            spot_df = spot_df[~spot_df["code"].astype(str).str.startswith("688")]
        if settings.EXCLUDE_BSE:
            bse = ("82", "83", "87", "88", "43", "920")
            spot_df = spot_df[~spot_df["code"].astype(str).str.startswith(bse)]
        if settings.EXCLUDE_ST:
            spot_df = spot_df[~spot_df["name"].astype(str).str.contains("ST", case=False, na=False)]

        close_cache: Dict[str, float] = {}
        ohlc_cache: Dict[str, dict] = {}
        for _, r in spot_df.iterrows():
            code = str(r["code"])
            close_cache[code] = float(r.get("close", r.get("price")))
            ohlc_cache[code] = {k: float(r.get(k, r.get("price")))
                                for k in ("open", "high", "low", "close", "pre_close",
                                          "change_pct", "amplitude", "volume_ratio")}
        ma5_cache: Dict[str, float] = {}
        for pos in positions:
            bars = kline_cache.get(pos["code"])
            if bars is not None and len(bars["close"].loc[:date_str]) >= 5:
                ma5_cache[pos["code"]] = float(bars["close"].loc[:date_str].tail(5).mean())
        return {"date_str": date_str, "spot_df": spot_df, "ohlc_cache": ohlc_cache,
                "close_cache": close_cache, "ma5_cache": ma5_cache, "zt_df": pd.DataFrame()}

    @staticmethod
    def _process_signal_buys(date_str: str, strategy: str, day_data: dict,
                             slippage: float) -> list:
        """按策略信号列选候选，买入价=当日收盘价（≈尾盘价），无预算上限。"""
        spot = day_data.get("spot_df")
        if spot is None or spot.empty:
            return []
        col = SIGNAL_TO_COL[strategy]
        cand = spot[spot[col]].sort_values(
            by=["change_pct", "volume_ratio"], ascending=[False, False])
        out = []
        for _, row in cand.iterrows():
            code = str(row["code"])
            close_px = float(row.get("close", row.get("price")))
            if close_px <= 0 or not code:
                continue
            # 买入价口径：
            # 尾盘博弈用收盘价(14:30≈收盘)；
            # 逼近封板用当日最高价(盘中冲高触发时价格接近 high，追涨成本)；
            # 其余盘中信号用当日 VWAP(成交均价，避免收盘价追高)
            if strategy == "尾盘博弈":
                buy_px = close_px
            elif strategy == "逼近封板":
                buy_px = float(row.get("high", close_px) or close_px)
            else:
                vol = float(row.get("volume", 0) or 0)
                amt = float(row.get("amount", 0) or 0)
                buy_px = amt / vol if vol > 0 and amt > 0 else close_px
            cost = buy_px * (1 + slippage / 100)
            out.append({
                "code": code, "name": str(row.get("name", code)), "buy_date": date_str,
                "cost_price": round(cost, 2), "current_price": close_px,
                "hold_days": 0, "profit_pct": 0.0, "strategy": strategy,
                "sell_mode": "tail_game" if strategy == "尾盘博弈" else "regular",
                "signals": signal_labels(row),
            })
        return out

    @staticmethod
    def _process_tail_game_sells(positions: list, date_str: str, day_data: dict,
                                 available_cash: float, slippage: float) -> tuple:
        """
        尾盘博弈卖出：次日早盘兑现（不过 10:30）。
        次日 open ≥ 成本×(1+TAIL_GAME_OPEN_GAP_PCT) → 视为高开，在 open~window_high 之间按
        TAIL_GAME_TAKE_RATIO 兑现；否则按 open 卖出（开盘兑现/止损）。尾盘博弈持仓次日必清，绝不过夜第 2 天。

        回测口径（消除 look-ahead）：日线只有全天最高价，而实盘兑现窗口是 09:30-10:30。
        这里把窗口最高价按 开盘×(1+TAIL_GAME_MORNING_HIGH_CAP_PCT%) 封顶——即使全天最高出现在
        10:30 之后，也不虚高回测收益（原用全天 high 是轻度未来函数，已在评审中指出）。
        """
        remaining, closed = [], []
        for pos in positions:
            if pos.get("sell_mode") != "tail_game":
                remaining.append(pos)
                continue
            row = day_data.get("ohlc_cache", {}).get(pos["code"])
            if row is None:
                remaining.append(pos)
                continue
            open_px = float(row.get("open") or 0)
            high_px = float(row.get("high") or 0)
            if open_px <= 0:
                open_px = high_px = float(row.get("close") or 0)
            cost = pos["cost_price"]
            if open_px >= cost * (1 + settings.TAIL_GAME_OPEN_GAP_PCT / 100):
                # 高开：按兑现比例在 open~window_high 之间卖（默认 0.5=冲高一半），
                # window_high 用 09:30-10:30 窗口封顶值，不用全天最高价（消除 10:30 后高点的 look-ahead）
                _window_high = min(high_px, open_px * (1 + settings.TAIL_GAME_MORNING_HIGH_CAP_PCT / 100))
                take = settings.TAIL_GAME_TAKE_RATIO
                sell_price = (open_px + (_window_high - open_px) * take) * (1 - slippage / 100)
                reason = "尾盘博弈-次日高开冲高兑现"
            else:
                sell_price = open_px * (1 - slippage / 100)
                reason = "尾盘博弈-次日未高开按开盘兑现"
            closed.append({
                "code": pos["code"], "name": pos.get("name", pos["code"]),
                "buy_date": pos["buy_date"], "sell_date": date_str,
                "buy_price": cost, "sell_price": round(sell_price, 2),
                "return_pct": AIBacktestEngine._calc_return(cost, sell_price),
                "hold_days": pos.get("hold_days", 1), "strategy": "尾盘博弈",
                "reason": reason,
            })
        return remaining, closed, available_cash

    @staticmethod
    @socket_timeout()
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
            closes = None
            # 自适应放宽窗口：长假（春节/国庆≈8-9个连续休市日）后 10 个自然日
            # 可能不足 5 个交易日 → MA5 缺失 → 破位止损在回测中静默失效。
            # 逐档放宽直到凑够 5 根日线（仅长假邻近日多一次请求，日常取第一档即中）。
            for days in (10, 25, 60):
                start_dt = end_dt - datetime.timedelta(days=days)
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
                    if len(closes) >= 5:
                        break
            if closes is None or closes.empty:
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
        signal_mode: bool = False,
        disclaimer: str = "",
    ) -> dict:
        """生成回测汇总报告。signal_mode=True 时输出 signal_compare 策略桶胜率对比。"""
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

        # 总收益：有净值用净值（zt 模式）；signal 模式无净值，用逐笔收益简单加总（只看收益率%）
        if daily_equity:
            total_return = round(
                (daily_equity[-1]["total_equity"] - 1_000_000) / 1_000_000 * 100, 2)
        elif signal_mode:
            total_return = round(sum(returns), 2)
        else:
            total_return = 0.0

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

        # 信号模式胜率对比：按策略桶（四类信号 + 尾盘博弈 + 全部）独立统计
        signal_compare = None
        if signal_mode:
            signal_compare = {}
            groups: Dict[str, list] = {}
            for t in closed_trades:
                groups.setdefault(t.get("strategy", "其他"), []).append(t["return_pct"])
            for s, rets in groups.items():
                w = sum(1 for r in rets if r > 0)
                signal_compare[s] = {
                    "trades": len(rets),
                    "win_rate_pct": round(w / len(rets) * 100, 2),
                    "avg_return_pct": round(sum(rets) / len(rets), 2),
                    "total_return_pct": round(sum(rets), 2),
                }
            all_rets = [t["return_pct"] for t in closed_trades]
            if all_rets:
                w2 = sum(1 for r in all_rets if r > 0)
                signal_compare["全部信号"] = {
                    "trades": len(all_rets),
                    "win_rate_pct": round(w2 / len(all_rets) * 100, 2),
                    "avg_return_pct": round(sum(all_rets) / len(all_rets), 2),
                    "total_return_pct": round(sum(all_rets), 2),
                }

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
            "max_positions": "不限" if signal_mode else max_positions,
            "max_daily_buys": "不限" if signal_mode else max_daily_buys,
            "period": f"{start_date}~{end_date}",
            "trading_days": len(date_list),
            "circuit_breaker_days": len(circuit_breaker_dates),
            "strategy_breakdown": strategy_summary,
            "sell_reason_breakdown": reason_summary,
            "profit_distribution": profit_ranges,
            "signal_compare": signal_compare,
            "disclaimer": (disclaimer or (
                "基于全市场日线收盘口径近似，非真实盘中信号；尾盘博弈买入价=收盘价近似14:30；"
                "同一标的可计入多个独立策略桶；high/涨停附近价可能不可成交，存在乐观偏差。"
            ) if signal_mode else ""),
            "top5": [{"code": t["code"], "name": t["name"], "return_pct": t["return_pct"],
                      "hold_days": t["hold_days"], "reason": t.get("reason", ""),
                      "strategy": t.get("strategy", "")} for t in top5],
            "bottom5": [{"code": t["code"], "name": t["name"], "return_pct": t["return_pct"],
                         "hold_days": t["hold_days"], "reason": t.get("reason", ""),
                         "strategy": t.get("strategy", "")} for t in bottom5],
            "daily_equity": daily_equity[-20:],  # 最近 20 天净值
            "trades_sample": sorted_trades[:30],  # 前 30 笔明细
        }
