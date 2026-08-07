# -*- coding: utf-8 -*-
"""
pytest 全局夹具：任何测试执行前先切到独立测试库（dragon_pulse_test.db）。

背景：部分测试类（如 test_kline_etl.py::TestKlineEtl）的 setUp 直接做破坏性操作
（`session.query(DailyKline).delete()` / `Base.metadata.drop_all`），但自身没有
`switch_to_test_db()`（依赖同文件靠前的类先切换）。当单独跑该类/单个用例时，
db_manager 仍指向生产库 dragon_pulse.db → 会误删生产数据（2026-08-07 曾因此
清空生产 daily_kline）。

本会话级 autouse 夹具在「任何 setUpClass/setUp 之前」先切到测试库，全局兜底，
即使某测试类漏写 switch_to_test_db 也绝不会碰到生产库。
"""
import pytest

from database.connection import db_manager, switch_to_test_db


@pytest.fixture(scope="session", autouse=True)
def _force_test_db():
    """会话开始即切到独立测试库；结束后释放引擎连接。"""
    switch_to_test_db()
    yield
    db_manager.engine.dispose()
