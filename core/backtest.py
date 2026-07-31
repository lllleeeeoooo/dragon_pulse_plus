"""
模拟回测引擎
用历史涨停池数据回测 AI 自动买卖策略的收益与胜率。

核心逻辑：
- 买入信号：每日涨停池中符合条件的标的（如连板 >= 2）
- 卖出信号：N 天后卖出，或触发断板/破位
- 统计：胜率、平均收益、最大回撤
"""

import logging
import datetime
from typing import Dict, Any, List, Optional
import pandas as pd

from data.fetcher import DataFetcher
from core.holding_monitor import HoldingMonitor

logger = logging.getLogger(__name__)


class BacktestEngine:
    """简易回测引擎"""

    @staticmethod
    def run(
        start_date: str = "20260701",
        end_date: str = "20260730",
        strategy: str = "打板接力",
        hold_days: int = 3,
        slippage: float = 0.5  # 滑点 (%)
    ) -> dict:
        """
        :param start_date: 回测起始日期 YYYYMMDD
        :param end_date: 回测结束日期 YYYYMMDD
        :param strategy: 回测策略：打板接力/低吸/全部
        :param hold_days: 持仓天数
        :param slippage: 买卖滑点 (%)
        """
        results = []
        dates = pd.date_range(start=start_date, end=end_date, freq="B")
        date_list = [d.strftime("%Y%m%d") for d in dates]
        total_trades = 0
        wins = 0

        for i, date_str in enumerate(date_list):
            try:
                zt_df = DataFetcher.get_zt_pool(date_str=date_str)
                if zt_df is None or zt_df.empty:
                    continue
            except Exception:
                continue

            # 卖出日：买入日之后第 N 个交易日
            sell_idx = i + hold_days
            if sell_idx >= len(date_list):
                continue
            sell_date_str = date_list[sell_idx]

            # 筛选买入标的
            if strategy == "打板接力":
                zt_df = zt_df[zt_df["lbc"].astype(int) >= 2]
            elif strategy == "低吸":
                zt_df = zt_df[zt_df["lbc"].astype(int) == 1]
            zt_df = zt_df.head(10)  # 每天最多买 10 只

            for _, row in zt_df.iterrows():
                code = str(row.get("code", ""))
                name = str(row.get("name", ""))
                buy_price = float(row.get("price", 0))
                if buy_price <= 0:
                    continue

                sell_price = BacktestEngine._get_close_price(code, sell_date_str)
                if sell_price is None or sell_price <= 0:
                    continue

                # 扣除滑点
                buy_cost = buy_price * (1 + slippage / 100)
                sell_revenue = sell_price * (1 - slippage / 100)
                ret = round((sell_revenue - buy_cost) / buy_cost * 100, 2)

                total_trades += 1
                if ret > 0:
                    wins += 1

                results.append({
                    "date": date_str,
                    "code": code,
                    "name": name,
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "return_pct": ret,
                    "hold_days": hold_days,
                })

        if not results:
            return {"total_trades": 0, "message": "无有效交易数据"}

        returns = [r["return_pct"] for r in results]
        avg_ret = round(sum(returns) / len(returns), 2)
        win_rate = round(wins / total_trades * 100, 2)
        max_ret = round(max(returns), 2)
        min_ret = round(min(returns), 2)

        # 个股收益排行（按代码聚合，取平均收益）
        stock_map: Dict[str, List[float]] = {}
        stock_names: Dict[str, str] = {}
        for r in results:
            c = r["code"]
            stock_map.setdefault(c, []).append(r["return_pct"])
            stock_names[c] = r.get("name", c)
        stock_avg = [(c, round(sum(v) / len(v), 2), len(v), stock_names.get(c, c))
                     for c, v in stock_map.items()]
        stock_avg.sort(key=lambda x: x[1], reverse=True)
        top5 = [{"code": c, "name": n, "avg_return": ret, "trades": cnt}
                for c, ret, cnt, n in stock_avg[:5]]
        bottom5 = [{"code": c, "name": n, "avg_return": ret, "trades": cnt}
                   for c, ret, cnt, n in stock_avg[-5:]]

        # 最大回撤
        cumulative = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for r in returns:
            cumulative *= (1 + r / 100)
            peak = max(peak, cumulative)
            drawdown = (peak - cumulative) / peak * 100
            max_drawdown = max(max_drawdown, drawdown)

        logger.info(
            f"回测完成: {start_date}~{end_date} "
            f"策略={strategy} 持仓={hold_days}天 "
            f"交易={total_trades}笔 胜率={win_rate}% "
            f"均收益={avg_ret}% 最大回撤={round(max_drawdown, 2)}%"
        )

        return {
            "total_trades": total_trades,
            "win_rate_pct": win_rate,
            "avg_return_pct": avg_ret,
            "max_return_pct": max_ret,
            "min_return_pct": min_ret,
            "max_drawdown_pct": round(max_drawdown, 2),
            "strategy": strategy,
            "hold_days": hold_days,
            "period": f"{start_date}~{end_date}",
            "top5": top5,
            "bottom5": bottom5,
            "trades": results[:20],  # 仅返回前 20 笔明细
        }

    @staticmethod
    def _get_close_price(code: str, date_str: str) -> Optional[float]:
        """获取指定日期的收盘价，失败返回 None"""
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=date_str,
                                     end_date=date_str, adjust="qfq")
            if df is not None and not df.empty:
                # 优先找 "收盘" 列，找不到用第3列
                close_col = "收盘" if "收盘" in df.columns else (df.columns[2] if len(df.columns) > 2 else df.columns[0])
                return float(pd.to_numeric(df[close_col].iloc[-1]))
        except Exception:
            pass
        return None
