"""
看板模块
- data.py: 数据聚合，从各服务收集看板所需数据
- templates.py: HTML 渲染，每个板块独立函数
"""

from dashboard.data import build_dashboard_data
from dashboard.templates import render_html

__all__ = ["build_dashboard_data", "render_html"]
