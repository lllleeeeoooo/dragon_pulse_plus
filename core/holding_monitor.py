import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings

logger = logging.getLogger(__name__)


class HoldingMonitor:
    """
    持仓实时监控与卖出条件判定引擎
    风控离场规则：
    1. 情绪到顶预警：全市场最高连板达到 8 板以上且炸板率极高，提示减仓
    2. 破位止损：跌破分时均线且 30 分钟未收回，或跌破关键支撑（如5日均线）
    3. 断板必卖：短线龙头一旦不封板/炸板放量，发出强制离场信号
    """

    @staticmethod
    def check_sell_signals(
        stock_code: str,
        stock_name: str,
        current_price: float,
        cost_price: float,
        avg_vwap_price: float,
        ma5_price: float,
        is_limit_up: bool,
        was_limit_up_today: bool,
        market_max_lbc: int = 5,
        market_zhaban_rate: float = 20.0,
        holding_days: int = 0,
        buy_strategy: str = ""
    ) -> List[Dict[str, Any]]:
        """
        检查单只持仓股票是否触卖出/减仓/止损信号
        """
        signals = []

        # 变动百分比
        profit_pct = round(((current_price - cost_price) / cost_price) * 100, 2) if cost_price > 0 else 0.0

        # 规则 0：绝对止损（最高优先级，无条件触发）
        if profit_pct <= settings.ABSOLUTE_STOP_LOSS_PCT:
            signals.append({
                "type": "绝对止损",
                "level": "CRITICAL",
                "reason": f"标的 {stock_name}({stock_code}) 亏损已达 {profit_pct}%，触及绝对止损线({settings.ABSOLUTE_STOP_LOSS_PCT}%)，无条件止损离场！"
            })
            return signals  # 绝对止损直接返回，不再检查其他规则

        # 规则 1：断板必卖 (针对短线龙头/连板股)
        # 条件：曾封板 + 当前未封板 + 价格已跌破分时均线（排除正常的炸板回封过程）
        if was_limit_up_today and not is_limit_up and current_price < avg_vwap_price:
            signals.append({
                "type": "断板必卖",
                "level": "CRITICAL",
                "reason": f"标的 {stock_name}({stock_code}) 今日曾封涨停但炸板后跌破分时均线({avg_vwap_price:.2f})，回封概率低，建议立刻减仓/清仓。"
            })

        # 规则 2：破位止损 (按策略区分灵敏度)
        # 打板/接力股：跌破VWAP即触发（短线资金不容忍水下运行）
        # 低吸/中军股：需跌破MA5才触发（日内VWAP波动大，MA5更可靠）
        is_relay_strategy = any(k in buy_strategy for k in ("打板", "接力", "AI自动"))
        if is_relay_strategy:
            if current_price < avg_vwap_price:
                signals.append({
                    "type": "破位止损",
                    "level": "HIGH",
                    "reason": f"标的 {stock_name}({stock_code}) 打板/接力策略，现价({current_price})跌破分时均线({avg_vwap_price:.2f})，建议止损。"
                })
        else:
            if ma5_price > 0 and current_price < ma5_price:
                signals.append({
                    "type": "破位止损",
                    "level": "HIGH",
                    "reason": f"标的 {stock_name}({stock_code}) 低吸策略，现价({current_price})跌破5日均线({ma5_price:.2f})，建议止损。"
                })

        # 规则 3：情绪到顶预警 (市场环境风控，阈值可通过 settings 配置)
        if market_max_lbc >= settings.EMOTION_TOP_MAX_LBC and market_zhaban_rate > settings.EMOTION_TOP_ZHABAN_RATE:
            signals.append({
                "type": "情绪到顶预警",
                "level": "WARNING",
                "reason": f"全市场最高连板已达 {market_max_lbc} 板极值且炸板率高达 {market_zhaban_rate}%，市场处于高潮末端/退潮期，建议逢高落袋为安。"
            })

        # 规则 4：时间止损 (持仓超过N天且未盈利)
        if holding_days >= settings.TIME_STOP_LOSS_DAYS and profit_pct <= 0:
            signals.append({
                "type": "时间止损",
                "level": "WARNING",
                "reason": f"标的 {stock_name}({stock_code}) 已持仓 {holding_days} 天且仍亏损 {profit_pct}%，短线资金效率低下，建议换股。"
            })

        # 规则 5：止盈提醒 (盈利较大时逢高落袋)
        if profit_pct >= settings.TAKE_PROFIT_CRITICAL_PCT:
            signals.append({
                "type": "逢高止盈",
                "level": "HIGH",
                "reason": f"标的 {stock_name}({stock_code}) 盈利已达 {profit_pct}%（超过{settings.TAKE_PROFIT_CRITICAL_PCT}%强止盈线），短线应锁定利润，逢高分批卖出！"
            })
        elif profit_pct >= settings.TAKE_PROFIT_WARN_PCT:
            signals.append({
                "type": "止盈提醒",
                "level": "WARNING",
                "reason": f"标的 {stock_name}({stock_code}) 盈利已达 {profit_pct}%（超过{settings.TAKE_PROFIT_WARN_PCT}%），建议设置移动止盈或逢高减仓。"
            })

        return signals
