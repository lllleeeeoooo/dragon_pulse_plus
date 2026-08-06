import logging
import threading
import time
from typing import Dict
import pandas as pd

from llm.client import llm_client
from config.prompt_templates import DYNAMICS_SYSTEM_PROMPT, DYNAMICS_USER_TEMPLATE

logger = logging.getLogger(__name__)

# 全市场快照短 TTL 缓存：同一监控周期内多个候选股共用一份 LLM 上下文，
# 避免每只股票都重下全市场资金流/涨停池/指数快照（审计#7 性能）
_CTX_CACHE_TTL = 30  # 秒：覆盖一个 15s 监控周期 + LLM 决策耗时，数据变化可忽略
# 容量上限：per-code 键(intraday:/daily_closes:)全天累积，无界增长会持有一整天 DataFrame
# 引用并拖慢查找——超过上限驱逐最旧键（审查#8）
_CTX_CACHE_MAX = 256
_ctx_cache: Dict[str, tuple] = {}
_ctx_lock = threading.Lock()


def _is_empty_ctx_value(val) -> bool:
    """空/失败结果判定：空结果不缓存——数据源瞬时故障期间每次调用重试，
    避免复用过期空数据喂给 LLM（审查#8）。"""
    if val is None:
        return True
    if isinstance(val, pd.DataFrame):
        return val.empty
    if isinstance(val, (list, tuple, dict, str)):
        return len(val) == 0
    return not val


def _evict_ctx_stale(now: float) -> None:
    """先清 TTL 过期条目；仍满则驱逐最早写入的键（FIFO），保持缓存有界。"""
    stale = [k for k, (ts, _) in _ctx_cache.items() if now - ts >= _CTX_CACHE_TTL]
    for k in stale:
        del _ctx_cache[k]
    if len(_ctx_cache) >= _CTX_CACHE_MAX and _ctx_cache:
        oldest_key = min(_ctx_cache, key=lambda k: _ctx_cache[k][0])
        del _ctx_cache[oldest_key]


def _ctx_cached(key: str, fetcher):
    """TTL 缓存包装：命中且未过期直接复用；过期/未命中则重新拉取并缓存。
    - 空/None 结果不缓存（故障期间重试）；
    - 容量超限驱逐最旧键；
    - 锁只包住字典操作（微秒级），不跨网络拉取，后台 LLM 线程与主循环并发安全。"""
    now = time.time()
    with _ctx_lock:
        hit = _ctx_cache.get(key)
        if hit is not None and now - hit[0] < _CTX_CACHE_TTL:
            return hit[1]
    val = fetcher()
    if _is_empty_ctx_value(val):
        with _ctx_lock:
            _ctx_cache.pop(key, None)
        return val
    with _ctx_lock:
        if key not in _ctx_cache and len(_ctx_cache) >= _CTX_CACHE_MAX:
            _evict_ctx_stale(now)
        _ctx_cache[key] = (now, val)
    return val


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
            # per-code 短 TTL 缓存：同一监控周期内多只候选共享分时数据，避免重复网络拉取（审查#8）
            df = _ctx_cached(f"intraday:{code}",
                             lambda: DataFetcher._fetch_intraday_5min(code))
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
            # per-code 短 TTL 缓存：多候选共享日线，避免每只重复拉取（审查#8）
            closes = _ctx_cached(f"daily_closes:{code}",
                                 lambda: DataFetcher.get_stock_daily_closes(code, lookback=30))
            if closes:
                # 实时均线：昨收日线序列 + 今日现价 合成（保证 MA 含今日，非昨收口径）
                real = [float(x) for x in closes] + [float(current_price)]
                ma5 = sum(real[-5:]) / 5 if len(real) >= 5 else None
                ma10 = sum(real[-10:]) / min(10, len(real)) if len(real) >= 10 else None
                ma20 = sum(real[-20:]) / min(20, len(real)) if len(real) >= 20 else None
                # 近5日累计：现价 vs 5个交易日前收盘（含今日实时）。
                # closes 不含今日（closes[-1]=昨收），5个交易日前 = closes[-5]，勿用 closes[-6]（那是 6 日跨度）
                if len(closes) >= 5 and closes[-5]:
                    recent_5d = round((float(current_price) - closes[-5]) / closes[-5] * 100, 2)
                    parts.append(f"个股近5日累计{recent_5d:+.1f}%")
                if ma5:
                    pos = "高于" if current_price >= ma5 else "低于"
                    parts.append(f"现价{pos}MA5({ma5:.2f})")
                    if ma10:
                        parts.append(f"MA10={ma10:.2f}")
                    if ma20:
                        parts.append(f"MA20={ma20:.2f}")
        except Exception:
            pass
        try:
            # 上证实时涨跌（新浪实时指数快照；勿用 stock_zh_index_daily——盘中只更新到昨日）
            import akshare as ak
            idx = _ctx_cached("index_spot", ak.stock_zh_index_spot_sina)
            if idx is not None and not idx.empty:
                m = idx[idx["代码"].astype(str) == "sh000001"]
                if not m.empty:
                    idx_chg = float(m.iloc[0].get("涨跌幅", 0) or 0)
                    parts.append(f"上证今日{idx_chg:+.2f}%")
        except Exception:
            pass
        try:
            from data.fetcher import DataFetcher
            instant = _ctx_cached("fund_flow_instant", DataFetcher.get_fund_flow_instant)
            if instant is not None and not instant.empty:
                m = instant[instant["code"].astype(str) == str(code).zfill(6)]
                if not m.empty:
                    net = float(m.iloc[0].get("net_amount", 0) or 0)
                    parts.append(f"个股主力净流入{net / 1e8:+.2f}亿")
        except Exception:
            pass
        # 4. 市场情绪周期（高潮/退潮/冰点——决定卖出敏感度）
        try:
            from database import SentimentManager
            recent = SentimentManager.get_recent_sentiments(days_lookback=1)
            if recent:
                stage = recent[0].get("cycle_stage", "")
                if stage:
                    parts.append(f"市场情绪[{stage}]")
        except Exception:
            pass
        # 5. 板块强弱 + 该股连板高度 + 市场最高板（今日涨停池）
        try:
            import datetime as _dt
            from data.fetcher import DataFetcher
            _zt_date = _dt.date.today().strftime("%Y%m%d")
            zt = _ctx_cached(f"zt_pool_{_zt_date}",
                             lambda: DataFetcher.get_zt_pool(date_str=_zt_date))
            if zt is not None and not zt.empty and "code" in zt.columns:
                code6 = str(code).zfill(6)
                m = zt[zt["code"].astype(str) == code6]
                if not m.empty:
                    lbc = int(m.iloc[0].get("lbc", 1))
                    parts.append(f"今日{lbc}板")
                    ind = str(m.iloc[0].get("industry", "") or "")
                    if ind:
                        ind_cnt = int((zt["industry"].astype(str) == ind).sum())
                        parts.append(f"板块[{ind}]涨停{ind_cnt}家")
                if "lbc" in zt.columns:
                    max_lbc = int(pd.to_numeric(zt["lbc"], errors="coerce").max())
                    parts.append(f"市场最高{max_lbc}板")
        except Exception:
            pass
        # 6. 概念（题材）—— A股短线炒的是概念，行业仅供参考。
        # 复用 ConceptCycleManager.get_stock_concepts（个股→概念+阶段/涨停家数/主线分，已按主线分排序），
        # 避免与 /data/stocks/{code}/concepts 接口各自实现、日后字段/过滤逻辑漂移（审计#10）
        try:
            from database import ConceptCycleManager
            stock_concepts = ConceptCycleManager.get_stock_concepts(code)
            if stock_concepts:
                # 有周期记录的概念才参与主线展示（无记录=冷门/新题材，phase/score 均空 → 自然排除；
                # get_stock_concepts 已按主线分降序，无需重排）
                mine = [r for r in stock_concepts if r["phase"] or r["mainline_score"] or r["zt_count"] or r["is_mainline"]]
                # 退潮/冰点概念不是当下主线，优先展示活跃概念（无活跃则回退全部）
                active = [r for r in mine if r["phase"] not in ("退潮", "冰点")]
                top_n = (active or mine)[:2]
                if top_n:
                    parts.append("概念:" + "/".join(
                        (("★" if r["is_mainline"] else "") +
                         f"{r['concept']}[{r['phase']}]{r['zt_count']}家")
                        for r in top_n))
                else:
                    parts.append(f"概念:{','.join(r['concept'] for r in stock_concepts[:3])}")
        except Exception:
            pass
        return " | ".join(parts) if parts else "暂无额外上下文"

    @staticmethod
    def _parse_verdict(text: str) -> str:
        """从 LLM 首行提取结论：🔥买入 / 👀观望 / 💀出货。无法识别返回空串。"""
        if not text:
            return ""
        first = text.strip().splitlines()[0] if text.strip() else ""
        if "🔥" in first or "买入" in first[:12]:
            return "买入"
        if "💀" in first or "出货" in first[:12] or "卖出" in first[:12]:
            return "出货"
        return "观望"

    @classmethod
    def format_sell_decision(cls, holding: dict, sig: dict, current_price: float,
                             change_pct: float) -> str:
        """
        B方案：卖出 LLM 决策（数据补全）。
        喂 持仓状态(成本/盈亏/天数/策略) + 规则触发信号 + 今日分时 + 位置/环境/资金，
        LLM 输出 💀卖出 / 👀持有。失败返回空串（调用方降级回规则）。
        """
        code = str(holding.get("code", ""))
        name = str(holding.get("name", code))
        cost = float(holding.get("cost_price", 0) or 0)
        days = int(holding.get("hold_days", 0) or 0)
        strategy = str(holding.get("buy_strategy", "") or "")
        profit = round((current_price - cost) / cost * 100, 2) if cost > 0 else 0.0
        intraday = cls._fetch_intraday(code)
        context = cls._fetch_context(code, current_price)
        user = (
            f"【持仓状态】{name}({code}) 成本{cost} 现价{current_price} 盈亏{profit:+.2f}% "
            f"持仓{days}天 策略[{strategy}]\n"
            f"【规则触发】{sig.get('type', '')}: {sig.get('reason', '')}\n"
            f"【今日分时OHLCV（O开 H高 L低 C收 V万）】\n{intraday}\n"
            f"【位置/大盘/资金上下文】{context}\n\n"
            "请判断是否卖出。首行必须输出：💀卖出 / 👀持有，并给出≤80字理由。"
        )
        system = (
            "你是A股持仓风控操盘手。基于持仓盈亏、规则触发信号与今日分时量价，判断是否卖出。\n"
            "注意：规则信号只是提示，你要用分时与位置/资金综合判断是真风险还是假摔。\n"
            "首行输出 💀卖出 或 👀持有，第二行起给理由。"
        )
        try:
            return llm_client.generate(system_prompt=system, user_prompt=user, module="sell_advisor")
        except Exception as e:
            logger.warning(f"卖出 LLM 决策失败: {e}")
            return ""

    @classmethod
    def format_buy_decision(cls, code, name, current_price, change_pct, volume_ratio,
                            signal_label, tags, detail="") -> str:
        """
        B方案：买入 LLM 决策。
        喂 候选信息 + 今日分时 + 位置/环境/资金，LLM 输出 🔥买入/👀观望/💀放弃。
        失败返回空串（调用方降级回规则）。
        """
        code, name = str(code), str(name)
        intraday = cls._fetch_intraday(code)
        context = cls._fetch_context(code, current_price)
        user = (
            f"【候选信息】{name}({code}) 现价{current_price} 涨幅{change_pct}% "
            f"量比{volume_ratio} 信号[{signal_label}] 标签[{','.join(tags or [])}]\n"
            f"【今日分时OHLCV（O开 H高 L低 C收 V万）】\n{intraday}\n"
            f"【位置/大盘/资金上下文】{context}\n\n"
            "判断是否值得买入。首行必须输出：🔥买入 / 👀观望 / 💀放弃，并给出≤80字理由。"
        )
        system = (
            "你是A股短线买入决策操盘手。基于候选信号、分时量价与位置/环境/资金综合判断是否买入。\n"
            "注意：高位放量可能出货、大盘弱势可能逆势陷阱。首行输出 🔥买入 / 👀观望 / 💀放弃，第二行起给理由。"
        )
        try:
            return llm_client.generate(system_prompt=system, user_prompt=user, module="sell_advisor")
        except Exception as e:
            logger.warning(f"买入 LLM 决策失败: {e}")
            return ""

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
