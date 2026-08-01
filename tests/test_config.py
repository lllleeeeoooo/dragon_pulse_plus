"""
配置与阈值逻辑测试
覆盖 Settings 校验、FUND_INFLOW 分级阈值、默认值合法性
"""
import unittest
from config.settings import settings


class TestSettingsValidation(unittest.TestCase):
    """配置参数校验"""

    def test_emotion_weights_sum_to_one(self):
        """盘中情绪四维权重之和应接近 1.0"""
        total = (settings.PREMIUM_WEIGHT + settings.BREADTH_WEIGHT +
                 settings.HEIGHT_WEIGHT + settings.SUPPORT_WEIGHT)
        self.assertAlmostEqual(total, 1.0, delta=0.01)

    def test_stop_loss_negative(self):
        """止损线应为负数"""
        self.assertLess(settings.ABSOLUTE_STOP_LOSS_PCT, 0)
        self.assertLess(settings.DAILY_LOSS_CIRCUIT_BREAKER, 0)

    def test_take_profit_order(self):
        """止盈提醒线应低于强止盈线"""
        self.assertLess(settings.TAKE_PROFIT_WARN_PCT, settings.TAKE_PROFIT_CRITICAL_PCT)

    def test_position_limits_positive(self):
        """仓位限制应为正数"""
        self.assertGreater(settings.MAX_AI_POSITIONS, 0)
        self.assertGreater(settings.MAX_DAILY_BUYS, 0)
        self.assertLessEqual(settings.MAX_DAILY_BUYS, settings.MAX_AI_POSITIONS)

    def test_monitor_interval_reasonable(self):
        """监控间隔应在 5-120 秒之间"""
        self.assertGreaterEqual(settings.MONITOR_INTERVAL_SECONDS, 5)
        self.assertLessEqual(settings.MONITOR_INTERVAL_SECONDS, 120)

    def test_regulatory_limits_order(self):
        """主板偏离度 < 创业板 = 科创板"""
        self.assertLess(settings.MAIN_BOARD_3D_DEV_LIMIT, settings.GEM_3D_DEV_LIMIT)
        self.assertEqual(settings.GEM_3D_DEV_LIMIT, settings.STAR_3D_DEV_LIMIT)

    def test_burst_threshold_positive(self):
        """点火异动阈值为正"""
        self.assertGreater(settings.VOL_BURST_THRESHOLD, 0)
        self.assertGreater(settings.PRICE_BURST_THRESHOLD, 0)
        self.assertGreater(settings.PRICE_BURST_MAX, settings.PRICE_BURST_THRESHOLD)


class TestFundInflowThreshold(unittest.TestCase):
    """FUND_INFLOW 按市值分级阈值逻辑"""

    def test_dynamic_threshold_small_cap(self):
        """小盘股(50亿)使用绝对底线"""
        circ_cap = 50 * 1e8  # 50亿
        dynamic = max(
            settings.FUND_INFLOW_MIN * 1e4,   # 2000万 → 元
            circ_cap * settings.FUND_INFLOW_CAP_RATIO
        )
        # 50亿 * 0.0005 = 250万 → 应使用底线 2000万
        self.assertEqual(dynamic, settings.FUND_INFLOW_MIN * 1e4)

    def test_dynamic_threshold_large_cap(self):
        """大盘股(2000亿)使用比例阈值"""
        circ_cap = 2000 * 1e8
        dynamic = max(
            settings.FUND_INFLOW_MIN * 1e4,
            circ_cap * settings.FUND_INFLOW_CAP_RATIO
        )
        # 2000亿 * 0.0005 = 1亿 → 应大于底线
        self.assertGreater(dynamic, settings.FUND_INFLOW_MIN * 1e4)

    def test_dynamic_threshold_equals_old_at_1000b(self):
        """1000亿市值时阈值约等于原默认5000万"""
        circ_cap = 1000 * 1e8
        dynamic = max(
            settings.FUND_INFLOW_MIN * 1e4,
            circ_cap * settings.FUND_INFLOW_CAP_RATIO
        )
        # 1000亿 * 0.0005 = 5000万（= 原默认值）
        dynamic_wan = dynamic / 1e4
        self.assertAlmostEqual(dynamic_wan, 5000, delta=100)


class TestBoardSpecificPriceBurst(unittest.TestCase):
    """PRICE_BURST_MAX 区分板块涨跌幅"""

    def test_main_board_burst_max(self):
        """主板 10cm 涨停线为 9.5%"""
        self.assertAlmostEqual(settings.PRICE_BURST_MAX, 9.5, delta=0.1)

    def test_20cm_board_burst_max(self):
        """双创 20cm 涨停线为 19.5%"""
        self.assertAlmostEqual(settings.PRICE_BURST_MAX_20CM, 19.5, delta=0.1)

    def test_20cm_higher_than_main(self):
        """20cm 阈值应显著高于主板"""
        self.assertGreater(settings.PRICE_BURST_MAX_20CM, settings.PRICE_BURST_MAX * 1.5)
