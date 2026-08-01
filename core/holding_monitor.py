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
        holding_days: int = 0
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
        if was_limit_up_today and not is_limit_up:
            signals.append({
                "type": "断板必卖",
                "level": "CRITICAL",
                "reason": f"标的 {stock_name}({stock_code}) 今日曾封涨停但当前炸板断板，符合龙头断板必卖原则，建议规避跌停风险立刻减仓/清仓。"
            })

        # 规则 2：破位止损 (跌破分时均线且跌破5日线)
        if current_price < avg_vwap_price and ma5_price > 0 and current_price < ma5_price:
            signals.append({
                "type": "破位止损",
                "level": "HIGH",
                "reason": f"标的 {stock_name}({stock_code}) 当前现价({current_price}) 已破分时均线({avg_vwap_price}) 且跌破5日均线({ma5_price})，建议及时止损/止盈。"
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

        return signals
