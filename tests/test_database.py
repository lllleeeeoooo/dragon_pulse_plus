import os

# ---------------------------------------------------------------------------
# 必须在任何项目模块导入之前设置 DB_PATH 环境变量。
# 但注意：unittest 按字母序执行，test_core.py 可能先于本文件触发 database 包导入，
# 此时 DB_PATH 尚未设置、db_manager 单例已按生产库初始化。
# 因此真正的保险在 setUpClass 里显式 switch_to_test_db() 重新指向测试库，
# 否则 Base.metadata.drop_all() 会清空生产库。此处设置环境变量仅作第一道保险。
# ---------------------------------------------------------------------------
os.environ["DB_PATH"] = "dragon_pulse_test.db"

import unittest
from database.services import HoldingManager, RecommendationManager, SentimentManager, db_manager, switch_to_test_db
from database.models import Holding, Recommendation, DailySentiment, Base
from config.settings import settings


class TestDatabaseServices(unittest.TestCase):
    """
    数据库 CRUD 与持久化功能单元测试
    使用独立的测试数据库（dragon_pulse_test.db），不污染生产库
    """

    @classmethod
    def setUpClass(cls):
        """
        确保测试数据库处于干净状态。
        必须先 switch_to_test_db()：其他测试文件可能在字母序上先导入 database 包，
        使 db_manager 单例绑定生产库；这里显式重指向测试库后 drop_all 才安全。
        """
        switch_to_test_db()
        Base.metadata.drop_all(bind=db_manager.engine)
        Base.metadata.create_all(bind=db_manager.engine)

    @classmethod
    def tearDownClass(cls):
        """测试类结束后：关闭引擎连接（测试库文件保留，方便排查）"""
        db_manager.engine.dispose()

    def setUp(self):
        """每个测试方法运行前：清空所有表数据，确保用例间相互隔离"""
        session = db_manager.get_session()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
        finally:
            session.close()

    def test_holding_crud(self):
        """测试持仓表的增删改查、持仓类型与收益率计算"""
        code = "000001"
        name = "平安银行"
        cost_price = 10.0

        # 1. 添加手动持仓与 AI 自动持仓
        res_m = HoldingManager.add_holding(code=code, name=name, cost_price=cost_price, holding_type="MANUAL")
        res_a = HoldingManager.add_holding(code="600519", name="贵州茅台", cost_price=1700.0, holding_type="AI_AUTO")
        self.assertTrue(res_m)
        self.assertTrue(res_a)

        # 2. 按类型筛选持仓
        manual_holdings = HoldingManager.get_active_holdings(holding_type="MANUAL")
        ai_holdings = HoldingManager.get_active_holdings(holding_type="AI_AUTO")
        self.assertTrue(any(h["code"] == code for h in manual_holdings))
        self.assertTrue(any(h["code"] == "600519" for h in ai_holdings))

        # 3. 更新收益率
        test_code = "600519"
        HoldingManager.update_holding_profit_rate(code=test_code, current_price=1870.0)  # 上涨 10%
        updated = HoldingManager.get_active_holdings(holding_type="AI_AUTO")
        target = next((h for h in updated if h["code"] == test_code), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["profit_rate"], 10.0)

    def test_recommendation_crud(self):
        """测试推荐标的表落库与检索"""
        import time
        trade_date = f"202607{int(time.time()) % 10000}"
        items = [
            {
                "code": "600519",
                "name": "贵州茅台",
                "strategy_type": "中军低吸",
                "open_requirement": "平开或轻微低开",
                "buy_condition": "回踩均线止跌跟进"
            }
        ]

        RecommendationManager.add_recommendations(trade_date, items)
        pending = RecommendationManager.get_pending_recommendations(trade_date)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["code"], "600519")

    def test_sentiment_save(self):
        """测试每日情绪向量落库"""
        trade_date = "20260729"
        data = {
            "height": 6,
            "breadth": 25,
            "zt_count": 35,
            "dt_count": 2,
            "sentiment_index": 72.5
        }
        SentimentManager.save_daily_sentiment(trade_date, data, cycle_stage="发酵期", summary="整体赚钱效应良好")


if __name__ == "__main__":
    unittest.main()
