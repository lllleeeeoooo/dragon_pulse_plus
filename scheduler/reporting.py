import datetime
import logging
from typing import Dict, Any, List, Optional
import pandas as pd

from config.settings import settings
from data.fetcher import DataFetcher
from data.news_fetcher import NewsFetcher
from llm.client import llm_client
from notifier.bark import bark_notifier
from core.trade_calendar import is_trading_day, is_last_non_trading_day

logger = logging.getLogger(__name__)
from database import HoldingManager, MarketIndexManager, DailySnapshotManager
def _push_daily_pnl_report(trade_date: str, spot_df=None):
    """盘后每日盈亏推送：同步收盘价 + 生成盈亏报告 + Bark 推送。"""

    # 1. 用今日收盘价刷新持仓当前价（保留 prev_close 昨收基准，供"今日涨跌"计算）
    spot_map = {}
    if spot_df is not None and not spot_df.empty:
        for _, row in spot_df.iterrows():
            code = str(row.get("code", ""))
            price = float(row.get("price", 0))
            if code and price > 0:
                spot_map[code] = price
    HoldingManager.update_current_prices(spot_map)

    # 2. 生成报告（今日涨跌 = 今日收盘价 - 昨收；必须先于滚存昨收，否则恒≈0）
    report = HoldingManager.get_daily_pnl_report()

    # 3. 报告完成后，将今日收盘价滚存为 prev_close（供次日"今日涨跌"基准）
    HoldingManager.sync_close_prices(spot_map)

    if "error" in report:
        logger.warning(f"每日盈亏报告生成失败: {report['error']}")
        return

    active_count = report.get("active_positions", 0)
    if active_count == 0 and report.get("today_closed_count", 0) == 0:
        return

    def _s(v):
        return f"+{v}" if v > 0 else str(v)

    # 大盘对比
    idx = MarketIndexManager.get_latest()

    lines = [
        f"📊 DragonPulse 每日盈亏报告 ({trade_date})",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📈 今日盈亏: {_s(report['today_total_pnl'])} 元 ({_s(report['today_total_pnl_pct'])}%)",
        f"   浮动: {_s(report['today_unrealized_pnl'])} 元",
    ]
    if idx and idx.get("sh_change_pct", 0) != 0:
        vs_market = round(report['today_total_pnl_pct'] - idx['sh_change_pct'], 2)
        lines.append(f"   上证: {_s(idx['sh_change_pct'])}% | 深证: {_s(idx.get('sz_change_pct', 0))}% | 创业板: {_s(idx.get('gem_change_pct', 0))}%")
        lines.append(f"   vs大盘: {_s(vs_market)}% {'🏆 跑赢' if vs_market > 0 else '📉 跑输'}")
    if report["today_closed_count"] > 0:
        lines.append(f"   已实现: {_s(report['today_realized_pnl'])} 元 (平仓{report['today_closed_count']}笔)")

    lines += [
        "",
        f"💰 累计总盈亏: {_s(report['cumulative_total_pnl'])} 元 ({_s(report['cumulative_total_pnl_pct'])}%)",
        f"   已实现: {_s(report['total_realized_pnl'])} 元 ({report['total_closed_count']}笔, 胜率{report['total_closed_win_rate']}%)",
        f"   浮动: {_s(report['total_unrealized_pnl'])} 元",
        "",
        f"📋 当前持仓 ({active_count}只) 盈{report['profit_count']}/亏{report['loss_count']}/平{report['flat_count']}:",
    ]

    for h in report.get("holdings", [])[:15]:
        emoji = "🟢" if h["profit_pct"] > 0 else ("🔴" if h["profit_pct"] < 0 else "⚪")
        tag = h["strategy"][:4] if h["strategy"] else ""
        lines.append(
            f"  {emoji} {h['name']}({h['code']}) "
            f"{_s(h['profit_pct'])}% | 今日{_s(h['today_change_pct'])}% | {tag}"
        )

    if active_count > 15:
        lines.append(f"  ... 还有 {active_count - 15} 只持仓")

    if report["today_closed_count"] > 0:
        lines.append("")
        lines.append(f"🔚 今日平仓 ({report['today_closed_count']}笔):")
        for t in report.get("today_closed_trades", []):
            emoji = "✅" if t["return_pct"] > 0 else "❌"
            lines.append(f"  {emoji} {t['name']}({t['code']}) {_s(t['return_pct'])}% | {t.get('strategy', '')}")

    body = "\n".join(lines)
    logger.info(f"每日盈亏报告:\n{body}")

    level = "passive"
    if report["today_total_pnl"] < 0:
        level = "active"
    if report.get("cumulative_total_pnl_pct", 0) < -5:
        level = "timeSensitive"

    bark_notifier.send(
        title=f"📊 每日盈亏 | {_s(report['today_total_pnl'])}元 ({_s(report['today_total_pnl_pct'])}%)",
        body=body,
        group="盈亏报告",
        level=level
    )

    # 净值快照落库
    sh_pct = idx.get("sh_change_pct", 0) if idx else 0.0
    DailySnapshotManager.save_snapshot(trade_date, report, sh_change_pct=sh_pct)


