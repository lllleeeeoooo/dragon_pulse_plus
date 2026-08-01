import logging
from database.models import DatabaseManager
logger = logging.getLogger(__name__)

db_manager = DatabaseManager()

def switch_to_test_db():
    """切换到测试数据库（测试用例 setUp 中调用）"""
    from config.settings import settings
    db_manager.reinitialize(settings.TEST_DB_PATH)


def switch_to_prod_db():
    """切换回生产数据库（测试用例 tearDown 中调用，可选）"""
    from config.settings import settings
    db_manager.reinitialize(settings.DB_PATH)


