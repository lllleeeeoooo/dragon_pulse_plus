"""
HoldingMonitor 卖出信号全矩阵测试
覆盖 5 条风控规则：绝对止损、断板必卖、破位止损、止盈、时间止损
"""
import datetime
import unittest
from unittest.mock import patch
from core.holding_monitor import HoldingMonitor


class TestHoldingMonitor(unittest.TestCase):
    """持仓监控卖出信号全覆盖"""

    def test_absolute_stop_loss_triggers(self):
        """绝对止损：亏损 >= 7% 时触发 CRITICAL，且不检查其他规则"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="测试股",
            current_price=9.30,       # 买入 10.00 → -7%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
        )
        self.assertTrue(any(s["type"] == "绝对止损" and s["level"] == "CRITICAL" for s in signals))
        # 绝对止损应直接返回，不再检查断板规则
        self.assertEqual(len(signals), 1)

    def test_stop_loss_not_triggered_when_above_threshold(self):
        """亏损未到 -7% 时不触发绝对止损"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="测试股",
            current_price=9.50,       # -5%，未触发
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
        )
        self.assertFalse(any(s["type"] == "绝对止损" for s in signals))

    def test_board_break_must_sell(self):
        """断板必卖：曾封板 + 当前未封板 + 跌破分时均线"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="炸板股",
            current_price=9.80,        # < VWAP 10.0
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=True,
        )
        self.assertTrue(any(s["type"] == "断板必卖" and s["level"] == "CRITICAL" for s in signals))

    def test_no_board_break_when_still_sealed(self):
        """仍在涨停板上不触发断板"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="封板股",
            current_price=11.0,
            cost_price=10.0,
            avg_vwap_price=10.5, ma5_price=10.0,
            is_limit_up=True, was_limit_up_today=True,
        )
        self.assertFalse(any(s["type"] == "断板必卖" for s in signals))

    def test_take_profit_critical(self):
        """强止盈：盈利 >= 20% 触发 HIGH"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="大赚股",
            current_price=12.0,       # +20%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
        )
        self.assertTrue(any(s["type"] == "逢高止盈" and s["level"] == "HIGH" for s in signals))

    def test_take_profit_warning(self):
        """止盈提醒：盈利 >= 15% 触发 WARNING"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="盈利股",
            current_price=11.5,       # +15%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
        )
        self.assertTrue(any(s["type"] == "止盈提醒" and s["level"] == "WARNING" for s in signals))

    def test_take_profit_critical_no_pullback_downgrades(self):
        """强止盈+高点回落：盈利≥20% 但现价=当日最高（未回落）→ 不触发强止盈，退化为止盈提醒(防打断主升浪)"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="主升浪",
            current_price=12.0,        # +20%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            day_high_price=12.0,       # 现价=最高，回落 0%
        )
        self.assertFalse(any(s["type"] == "逢高止盈" for s in signals))
        self.assertTrue(any(s["type"] == "止盈提醒" and s["level"] == "WARNING" for s in signals))

    def test_take_profit_critical_with_pullback(self):
        """盈利≥20% 且自当日高点回落≥5% → 触发强止盈 HIGH"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="回落股",
            current_price=12.0,        # +20%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            day_high_price=12.8,       # 回落 (12.8-12)/12.8=6.25% ≥ 5%
        )
        self.assertTrue(any(s["type"] == "逢高止盈" and s["level"] == "HIGH" for s in signals))

    def test_take_profit_critical_pullback_below_threshold(self):
        """盈利≥20% 但回落<5% → 仍不触发强止盈，只给止盈提醒"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="小回落",
            current_price=12.0,        # +20%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            day_high_price=12.3,       # 回落 (12.3-12)/12.3=2.4% < 5%
        )
        self.assertFalse(any(s["type"] == "逢高止盈" for s in signals))
        self.assertTrue(any(s["type"] == "止盈提醒" and s["level"] == "WARNING" for s in signals))

    def test_time_stop_loss(self):
        """时间止损：持仓 >= 3 天且未盈利触发 WARNING"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="死扛股",
            current_price=9.90,        # -1%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            holding_days=3,
        )
        self.assertTrue(any(s["type"] == "时间止损" for s in signals))

    def test_no_time_stop_when_profitable(self):
        """持仓 >= 3 天但盈利中不触发时间止损"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="耐心股",
            current_price=10.5,        # +5%
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            holding_days=5,
        )
        self.assertFalse(any(s["type"] == "时间止损" for s in signals))

    def test_emotion_top_warning(self):
        """情绪到顶：连板极高 + 炸板率高时触发 WARNING"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="高潮股",
            current_price=11.0,
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            market_max_lbc=8, market_zhaban_rate=40.0,
        )
        self.assertTrue(any(s["type"] == "情绪到顶预警" for s in signals))

    def test_relay_strategy_vwap_stop(self):
        """打板/接力策略：跌破 VWAP 即触发破位止损"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="打板股",
            current_price=9.80,        # < VWAP
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=10.0,
            is_limit_up=False, was_limit_up_today=False,
            buy_strategy="打板接力-3连板",
        )
        self.assertTrue(any(s["type"] == "破位止损" for s in signals))

    def test_non_relay_strategy_ma5_stop(self):
        """低吸策略：需跌破 MA5 才触发破位止损（VWAP 不管）"""
        signals = HoldingMonitor.check_sell_signals(
            stock_code="000001", stock_name="低吸股",
            current_price=9.80,        # < MA5(9.9), < VWAP(10.0)
            cost_price=10.0,
            avg_vwap_price=10.0, ma5_price=9.9,
            is_limit_up=False, was_limit_up_today=False,
            buy_strategy="低吸战法",
        )
        self.assertTrue(any(s["type"] == "破位止损" for s in signals))


class TestCountTradingDays(unittest.TestCase):
    """时间止损改交易日计算：count_trading_days 日历精确计数 + 工作日回退"""

    def test_日历精确计数(self):
        from core.trade_calendar import count_trading_days
        with patch("core.trade_calendar._ensure_synced"), \
             patch("database.services.TradeCalendarManager.count_trading_days", return_value=2):
            n = count_trading_days(datetime.date(2026, 8, 3), datetime.date(2026, 8, 7))
        self.assertEqual(n, 2)

    def test_日历不可用时工作日回退(self):
        """周五买入→周一 = 1 个交易日（回退按工作日）——消除自然日跨周末误触发"""
        from core.trade_calendar import count_trading_days
        fri = datetime.date(2026, 8, 7)   # 周五
        mon = datetime.date(2026, 8, 10)  # 周一
        with patch("core.trade_calendar._ensure_synced"), \
             patch("database.services.TradeCalendarManager.count_trading_days", return_value=None):
            # 同 monitor_core 口径：count(buy+1, today]
            n = count_trading_days(fri + datetime.timedelta(days=1), mon)
        self.assertEqual(n, 1)  # 只数到周一

    def test_当天买入0交易日(self):
        from core.trade_calendar import count_trading_days
        day = datetime.date(2026, 8, 7)
        with patch("core.trade_calendar._ensure_synced"), \
             patch("database.services.TradeCalendarManager.count_trading_days", return_value=None):
            n = count_trading_days(day + datetime.timedelta(days=1), day)
        self.assertEqual(n, 0)  # 区间空 → 0
