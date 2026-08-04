"""
概念题材标签过滤（切片3·开篇）
================================
概念板块列表里混着三类标签，超短主线识别只应看「题材」：
  - 题材(THEME)  华为概念 / AI算力 / 军工航天 / 光伏 ... 有产业逻辑、有炒作叙事
  - 事件(EVENT)  股权激励 / 业绩预升 / 重组概念 ... 公司「发生了个事」，不是市场叙事
  - 属性(ATTR)   ST板块 / 专精特新 / QFII重仓 ... 公司「属于某类状态」，不是交易逻辑

本模块提供 classify_concept / is_thematic / filter_thematic，
供切片3后续的概念板块周期、概念主线复盘消费。
黑名单为手工校准（依据新浪/东财概念真实名单），边界项需随市场调参。
"""
from typing import Iterable, List, Tuple

THEME = "THEME"  # 题材：主线识别保留
EVENT = "EVENT"  # 事件型：过滤
ATTR = "ATTR"    # 属性型：过滤

# 类别中文名（用于展示/日志/复盘）
CATEGORY_LABELS = {THEME: "题材", EVENT: "事件", ATTR: "属性"}

# 包含规则：(类别, 子串)。按序匹配，命中即返回。
_PATTERN_RULES: List[Tuple[str, str]] = [
    # ---- 事件型（公司/市场发生了某件事，不是可炒作的产业叙事） ----
    (EVENT, "业绩"),          # 业绩预升/预降/预增/预亏
    (EVENT, "股权激励"),
    (EVENT, "重组"),          # 重组概念
    (EVENT, "整体上市"),
    (EVENT, "资产注入"),
    (EVENT, "送转"),          # 送转潜力（高送转行情）
    (EVENT, "摘帽"),          # 摘帽概念（ST摘帽行情，季节性事件）
    (EVENT, "解禁"),          # 本月解禁
    (EVENT, "出口退税"),      # 政策事件，非板块叙事
    # ---- 属性型（公司固有的状态/资质/背景，不构成炒作逻辑） ----
    (ATTR, "ST"),             # ST板块 / 准ST股
    (ATTR, "专精特新"),       # 资质标签
    (ATTR, "重仓"),           # QFII/保险/信托/券商/基金/社保重仓
    (ATTR, "参股"),           # 参股金融 / 金融参股
    (ATTR, "未股改"),
    (ATTR, "外资背景"),
    (ATTR, "高校背景"),
    (ATTR, "三板"),           # 三板精选
    (ATTR, "融资融券"),       # 交易资格属性
    (ATTR, "超大盘"),         # 规模属性
    (ATTR, "本地"),           # 上海本地/深圳本地（过泛的地域属性，不构成主线）
]

# 前缀规则：(类别, 前缀)
_PREFIX_RULES: List[Tuple[str, str]] = [
    (ATTR, "含"),             # 含B股/含GDR/含H股
]

# 后缀规则：(类别, 后缀)
_SUFFIX_RULES: List[Tuple[str, str]] = [
    (ATTR, "50"),             # 央企50 / 科创50（指数样本属性）
]


def classify_concept(name: str) -> str:
    """返回概念类别: THEME(题材/保留) / EVENT(事件) / ATTR(属性)。"""
    if not name:
        return THEME
    for cat, pat in _PATTERN_RULES:
        if pat in name:
            return cat
    for cat, prefix in _PREFIX_RULES:
        if name.startswith(prefix):
            return cat
    for cat, suffix in _SUFFIX_RULES:
        if name.endswith(suffix):
            return cat
    return THEME


def is_thematic(name: str) -> bool:
    """是否为题材型概念（超短主线识别的候选）。"""
    return classify_concept(name) == THEME


def filter_thematic(names: Iterable[str]) -> List[str]:
    """从概念名列表中过滤掉非题材标签（事件/属性），只返回题材型。"""
    return [n for n in names if is_thematic(n)]


def classify_batch(names: Iterable[str]) -> List[Tuple[str, str]]:
    """返回 [(概念名, 类别)]，供展示/调试/复盘。"""
    return [(n, classify_concept(n)) for n in names]
