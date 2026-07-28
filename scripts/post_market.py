import sys
import os
import asyncio
from datetime import datetime
import pandas as pd
import akshare as ak

# 确保脚本可以从根目录导入模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database
from push import send_bark

class PostMarketReviewer:
    """盘后深度复盘与次日接力选股引擎"""
    def __init__(self):
        self.db = Database()

    async def fetch_market_sentiment(self, loop):
        """抓取全市场盘后数据"""
        date_str = datetime.now().strftime('%Y%m%d')
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在抓取全市场盘后数据与涨停池...")

        zt_task = loop.run_in_executor(None, lambda: ak.stock_zt_pool_em(date=date_str))
        zb_task = loop.run_in_executor(None, lambda: ak.stock_zt_pool_zbgc_em(date=date_str))
        dt_task = loop.run_in_executor(None, lambda: ak.stock_zt_pool_dtgc_em(date=date_str))
        spot_task = loop.run_in_executor(None, ak.stock_zh_a_spot_em)

        zt_df, zb_df, dt_df, spot_df = await asyncio.gather(zt_task, zb_task, dt_task, spot_task)
        return zt_df, zb_df, dt_df, spot_df

    def format_ladder(self, zt_df):
        """格式化连板梯队"""
        if zt_df is None or zt_df.empty or '连板天数' not in zt_df.columns:
            return "  • 暂无连板梯队数据"

        ladder_str_list = []
        grouped = zt_df.groupby('连板天数')
        sorted_heights = sorted(grouped.groups.keys(), reverse=True)

        for height in sorted_heights:
            stocks = grouped.get_group(height)['名称'].tolist()
            stocks_formatted = "、".join(stocks[:5]) + ("..." if len(stocks) > 5 else "")
            ladder_str_list.append(f"  • {height} 连板 ({len(stocks)}只): {stocks_formatted}")

        return "\n".join(ladder_str_list)

    def select_next_day_targets(self, zt_df):
        """从当天封板标的中精选次日可追接力目标，并生成精准开盘买入条件"""
        if zt_df is None or zt_df.empty:
            return "  • 今日无涨停标的，次日观望为主。"

        # 检查所需字段
        ladder_col = '连板天数' if '连板天数' in zt_df.columns else ('连板' if '连板' in zt_df.columns else None)
        if not ladder_col:
            zt_df['连板天数'] = 1
            ladder_col = '连板天数'

        targets_output = []
        # 过滤并排序：成交额 > 1.5 亿（保证流动性），按连板数和成交额排序
        candidate_df = zt_df[zt_df['成交额'] >= 150_000_000].sort_values(
            by=[ladder_col, '成交额'], ascending=[False, False]
        ).head(5)

        if candidate_df.empty:
            candidate_df = zt_df.sort_values(by=['成交额'], ascending=False).head(5)

        for idx, row in candidate_df.reset_index().iterrows():
            code = str(row['代码'])
            name = str(row['名称'])
            height = int(row[ladder_col])
            amount_e = round(float(row['成交额']) / 100_000_000, 2)
            sector = str(row.get('所属行业', '主线题材'))
            first_time = str(row.get('首次封板时间', '未知'))
            last_time = str(row.get('最后封板时间', '未知'))

            # 根据封板特征划分次日策略类型与精准买入条件
            if height >= 2:
                strategy_type = "🚀 龙头强接力 (高位晋级)"
                req_open = "超预期高开 +3.0% ~ +6.0%"
                req_amount = round(amount_e * 0.1, 2)
                condition_desc = (
                    f"09:25 竞价金额需 > {req_amount} 亿；\n"
                    f"      └─ 触发买点 : 开盘 1 分钟快速放量突破 09:25 竞价高点 ➔ 顺势半路或扫板切入"
                )

            elif first_time == last_time and first_time != '未知':
                strategy_type = "🔥 弱转强接力 (一字/烂板弱转强)"
                req_open = "超预期高开 +2.0% ~ +4.5%"
                req_amount = round(amount_e * 0.08, 2)
                condition_desc = (
                    f"09:25 竞价金额需 > {req_amount} 亿；\n"
                    f"      └─ 触发买点 : 09:25 弱转强挂单介入，或开盘回踩不破均线反抽放量时半路切入"
                )

            else:
                strategy_type = "⚖️ 分歧承接 (1进2分歧换手龙头)"
                req_open = "平开或轻微低开 -1.5% ~ +1.0%"
                req_amount = round(amount_e * 0.05, 2)
                condition_desc = (
                    f"09:25 竞价成交需 > {req_amount} 亿；\n"
                    f"      └─ 触发买点 : 低开/平开后，开盘 10 分钟内放量翻红突破分时均线 ➔ 半路低吸吸筹"
                )

            targets_output.append(
                f"  [{idx+1}] {name} ({code}) | {height}板 | 行业: {sector} | 成交: {amount_e}亿\n"
                f"      ├─ 策略类型     : {strategy_type}\n"
                f"      ├─ 09:25开盘要求: {req_open}\n"
                f"      ├─ 09:25竞价量能: 竞价金额必须 > {req_amount} 亿\n"
                f"      └─ 盘中买入动作 : {condition_desc}\n"
            )

        return "\n".join(targets_output)

    async def run(self):
        loop = asyncio.get_event_loop()
        zt_df, zb_df, dt_df, spot_df = await self.fetch_market_sentiment(loop)

        zt_count = len(zt_df) if zt_df is not None and not zt_df.empty else 0
        zb_count = len(zb_df) if zb_df is not None and not zb_df.empty else 0
        dt_count = len(dt_df) if dt_df is not None and not dt_df.empty else 0

        up_count = len(spot_df[spot_df['涨跌幅'] > 0]) if spot_df is not None and not spot_df.empty else 0
        down_count = len(spot_df[spot_df['涨跌幅'] < 0]) if spot_df is not None and not spot_df.empty else 0

        total_zt_attempts = zt_count + zb_count
        zb_rate = round((zb_count / total_zt_attempts * 100), 1) if total_zt_attempts > 0 else 0.0

        ladder_col = '连板天数' if zt_df is not None and '连板天数' in zt_df.columns else ('连板' if zt_df is not None and '连板' in zt_df.columns else None)
        max_height = int(zt_df[ladder_col].max()) if zt_df is not None and not zt_df.empty and ladder_col else 1

        top_sectors_str = "暂无数据"
        if zt_df is not None and not zt_df.empty and '所属行业' in zt_df.columns:
            sector_counts = zt_df['所属行业'].value_counts().head(3)
            top_sectors_str = "、".join([f"{sec}({cnt}只)" for sec, cnt in sector_counts.items()])

        today_prefix = datetime.now().strftime('%Y-%m-%d')
        signals_summary = []

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT stock_code, stock_name, signal_type, trigger_price, trigger_pct
                FROM signal_history
                WHERE push_time LIKE ?
            ''', (f"{today_prefix}%",))
            signals = cursor.fetchall()

        buy_signals_count = 0
        buy_limit_up_count = 0

        if signals and spot_df is not None:
            for code, name, sig_type, price, trigger_pct in signals:
                stock_row = spot_df[spot_df['代码'] == code]
                if not stock_row.empty:
                    close_price = float(stock_row.iloc[0]['最新价'])
                    close_pct = float(stock_row.iloc[0]['涨跌幅'])
                    is_zt = 1 if close_pct >= 9.8 else 0

                    if "BUY" in sig_type:
                        buy_signals_count += 1
                        if is_zt:
                            buy_limit_up_count += 1

                    self.db.update_daily_close(today_prefix, code, close_price, is_zt)
                    status_flag = "🔴 封板" if is_zt else f"⚪ {close_pct}%"
                    signals_summary.append(f"  • [{sig_type}] {name}({code}) 触发价:{price}元({trigger_pct}%) ➔ 收盘价:{close_price}元 ({status_flag})")

        win_rate = round((buy_limit_up_count / buy_signals_count * 100), 1) if buy_signals_count > 0 else 0.0

        # 次日接力精选目标提取
        next_day_targets_str = self.select_next_day_targets(zt_df)

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        ladder_details = self.format_ladder(zt_df)
        signals_details = "\n".join(signals_summary) if signals_summary else "  • 今日无触发信号"

        report_md = f"""
====================================================
🐉 【龙魂智策】每日复盘与 AI 次日接力买入条件报告 ({now_str})
====================================================

一、 市场整体温度与情绪
----------------------------------------------------
• 上涨/下跌家数 : 上涨 {up_count} 家 | 下跌 {down_count} 家
• 涨停/跌停家数 : 涨停 {zt_count} 家 | 跌停 {dt_count} 家
• 炸板率        : {zb_rate}% ({zb_count} 只炸板)
• 市场空间高度   : 最高 {max_height} 连板
• 最强领涨主线   : {top_sectors_str}

二、 连板梯队分布
----------------------------------------------------
{ladder_details}

三、 今日 AI 推送信号与胜率结算
----------------------------------------------------
• 推送买入信号数 : {buy_signals_count} 条
• 成功封板数     : {buy_limit_up_count} 条
• 今日打板封板率 : {win_rate}%

详细信号明细:
{signals_details}

四、 🎯 明日重点关注【今日封板标的之次日接力精选与开盘买入条件】
----------------------------------------------------
{next_day_targets_str}

五、 明日环境风险提示与开盘策略
----------------------------------------------------
"""
        if zb_rate > 50 or dt_count > 15:
            report_md += "⚠️ 提示：今日炸板率偏高，市场分歧剧烈，明日接力务必严格校验 09:25 竞价量能，防范开盘跳水！\n"
        elif max_height >= 3 and win_rate >= 50:
            report_md += "🚀 提示：龙头连板效应良好，次日弱转强标的成功率极高，符合竞价条件可果断切入！\n"
        else:
            report_md += "⚖️ 提示：市场处于存量震荡期，接力尽量选择领涨主线的首板或 1 进 2 换手龙头。\n"

        report_md += "===================================================="

        print(report_md)

        bark_summary = f"今日涨停{zt_count}家 | 次日接力精选目标与09:25买入条件已生成，详见终端复盘报告！"
        send_bark("🌙 龙魂智策：明日次日接力买入条件已生成", bark_summary, sound="minuet")

if __name__ == "__main__":
    reviewer = PostMarketReviewer()
    asyncio.run(reviewer.run())
