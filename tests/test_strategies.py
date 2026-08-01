"""
策略战法标签与板块区分逻辑测试
覆盖：打板接力 10cm/20cm 涨停线、涨停检测板块区分
"""
import unittest
from core.strategies import StrategyAnalyzer


class TestStrategyTags(unittest.TestCase):
    """战法标签分类"""

    def test_board_play_tag_main_board(self):
        """主板 10cm：涨幅 >= 9.5% 打板接力标签"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="600519", stock_name="茅台",
            change_pct=9.8, turnover_rate=5.0,
        )
        self.assertIn("打板接力", tags)

    def test_board_play_tag_20cm_not_triggered_at_10pct(self):
        """创业板 20cm：涨幅 10% 不应触发打板接力（涨停线是 19.5%）"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="300750", stock_name="宁德",
            change_pct=10.0, turnover_rate=5.0,
        )
        self.assertNotIn("打板接力", tags)

    def test_board_play_tag_20cm_triggers_at_limit(self):
        """创业板 20cm：涨幅达到涨停线触发"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="300750", stock_name="宁德",
            change_pct=19.8, turnover_rate=5.0,
        )
        self.assertIn("打板接力", tags)

    def test_board_play_tag_star_market(self):
        """科创板 20cm：涨幅达到涨停线触发"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="688001", stock_name="华兴",
            change_pct=19.7, turnover_rate=5.0,
        )
        self.assertIn("打板接力", tags)

    def test_second_wave_tag(self):
        """二波预警：前期龙头，回撤在 30%-50%"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="000001", stock_name="二波标的",
            change_pct=3.5, turnover_rate=5.0,
            is_past_dragon=True, retreat_ratio_from_high=0.40,
        )
        self.assertIn("二波预警", tags)

    def test_core_pool_pullback_tag(self):
        """中军回踩：核心池成员，-2%~3% 区间"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="000001", stock_name="中军A",
            change_pct=1.5, turnover_rate=3.0,
            is_in_core_pool=True,
        )
        self.assertIn("中军回踩", tags)

    def test_sector_resonance_tag(self):
        """板块共振：大盘强 + 板块活跃 + 个股大涨"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="000001", stock_name="共振股",
            change_pct=6.0, turnover_rate=8.0,
            index_change_pct=1.5, sector_active_count=4,
        )
        self.assertIn("板块共振", tags)

    def test_hedge_hug_tag(self):
        """避险抱团：大盘跌 + 缩量 + 个股微涨"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="000001", stock_name="抱团股",
            change_pct=1.0, turnover_rate=2.0,
            index_change_pct=-1.0, market_total_amount=5e11,
        )
        self.assertIn("避险抱团", tags)

    def test_default_watch_tag(self):
        """无特别信号时返回观望/跟随"""
        tags = StrategyAnalyzer.identify_tags(
            stock_code="000001", stock_name="平淡股",
            change_pct=1.0, turnover_rate=2.0,
        )
        self.assertIn("观望/跟随", tags)
