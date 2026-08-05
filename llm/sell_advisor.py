import logging
from typing import Dict, Any
import pandas as pd

from llm.client import llm_client
from config.prompt_templates import DYNAMICS_SYSTEM_PROMPT, DYNAMICS_USER_TEMPLATE

logger = logging.getLogger(__name__)


class DynamicSellAdvisor:
    """
    盘中异动归因与卖出风控提示润色器。
    拉取个股实时分时数据喂给 LLM，让其基于真实量价做判断。
    """

    @classmethod
    def _fetch_intraday(cls, code: str) -> str:
        """拉取个股今日 5 分钟 OHLCV（多源降级），压缩为文本供 LLM 分析"""
        try:
            from data.fetcher import DataFetcher
            df = DataFetcher._fetch_intraday_5min(code)
            if df is None or df.empty:
                return "分时数据暂不可用"

            points = []
            for _, r in df.iterrows():
                t = str(r.get("time", r.iloc[0]) if "time" in df.columns else r.iloc[0])
                if any(m in str(t) for m in ["09:", "10:", "11:", "13:", "14:", "15:"]):
                    o_val = float(r.get("open", r.iloc[1] if len(df.columns) > 1 else 0))
                    c_val = float(r.get("close", r.iloc[2] if len(df.columns) > 2 else 0))
                    h_val = float(r.get("high", r.iloc[3] if len(df.columns) > 3 else 0))
                    l_val = float(r.get("low", r.iloc[4] if len(df.columns) > 4 else 0))
                    v_val = float(r.get("volume", 0)) / 1e4
                    points.append(f"{str(t)[-8:-3]} O{o_val:.2f} H{h_val:.2f} L{l_val:.2f} C{c_val:.2f} V{v_val:.0f}万")

            return "\n".join(points) if points else "分时数据为空"
        except Exception as e:
            logger.warning(f"拉取 {code} 分时数据失败: {e}")
            return f"分时数据获取失败: {e}"

    @classmethod
    def _fetch_context(cls, code: str, current_price: float) -> str:
        """
        补充 LLM 判断上下文（数据不足修复）：
        1. 个股位置：近5日累计涨跌 + MA5/MA10/MA20 + 现价 vs MA5
        2. 大盘环境：上证指数今日涨跌（新浪日线兜底）
        3. 主力资金：同花顺全市场净流入（东财限流时的替代源）
        任一项失败则省略该项，不影响主流程。
        """
        parts = []
        try:
            from data.fetcher import DataFetcher
            ma = DataFetcher.get_stock_ma_prices(code, lookback=30)
            closes = DataFetcher.get_stock_daily_closes(code, lookback=6)
            if len(closes) >= 2 and closes[0]:
                recent_5d = round((closes[-1] - closes[0]) / closes[0] * 100, 2)
                parts.append(f"近5日累计{recent_5d:+.1f}%")
            if ma.get("ma5"):
                pos = "高于" if current_price >= ma["ma5"] else "低于"
                parts.append(f"现价{pos}MA5({ma['ma5']:.2f})")
                if ma.get("ma10"):
                    parts.append(f"MA10={ma['ma10']:.2f}")
                if ma.get("ma20"):
                    parts.append(f"MA20={ma['ma20']:.2f}")
        except Exception:
            pass
        try:
            import akshare as ak
            idx = ak.stock_zh_index_daily(symbol="sh000001")
            if idx is not None and not idx.empty and len(idx) >= 2:
                closes = pd.to_numeric(idx["close"], errors="coerce").dropna()
                idx_chg = (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100
                parts.append(f"上证今日{idx_chg:+.2f}%")
        except Exception:
            pass
        try:
            from data.fetcher import DataFetcher
            instant = DataFetcher.get_fund_flow_instant()
            if instant is not None and not instant.empty:
                m = instant[instant["code"].astype(str) == str(code).zfill(6)]
                if not m.empty:
                    net = float(m.iloc[0].get("net_amount", 0) or 0)
                    parts.append(f"主力净流入{net / 1e8:+.2f}亿")
        except Exception:
            pass
        return " | ".join(parts) if parts else "暂无额外上下文"

    @classmethod
    def format_alert_message(
        cls,
        trigger_type: str,
        stock_code: str,
        stock_name: str,
        current_price: float,
        change_pct: float,
        volume_ratio: float,
        strategy_tag: str,
        detail_info: str
    ) -> str:
        """
        将盘中异动 + 实时分时 OHLCV 数据发给 LLM，让其基于真实数据给出具体操作判断。
        LLM 失败时降级为规则化文案。
        """
        # 拉取实时分时数据
        intraday_raw = cls._fetch_intraday(stock_code)

        user_prompt = DYNAMICS_USER_TEMPLATE.format(
            trigger_type=trigger_type,
            stock_code=stock_code,
            stock_name=stock_name,
            current_price=current_price,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            strategy_tag=strategy_tag,
            detail_info=detail_info,
            intraday_data=intraday_raw,
            context_data=cls._fetch_context(stock_code, current_price)
        )

        try:
            message = llm_client.generate(
                system_prompt=DYNAMICS_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                module="sell_advisor"
            )
            return message
        except Exception as e:
            logger.error(f"生成异动提示文本失败: {e}")
            return (f"【{trigger_type}】{stock_name}({stock_code}) "
                    f"现价:{current_price} (+{change_pct}%) 量比:{volume_ratio} "
                    f"标签:[{strategy_tag}]")
