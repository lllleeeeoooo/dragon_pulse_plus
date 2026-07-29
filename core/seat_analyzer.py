import logging
from typing import Dict, Any, List
import pandas as pd

logger = logging.getLogger(__name__)

# 游资营业部画像字典
FAMOUS_SEATS = {
    # 格局派 (A类：大单锁仓、喜欢格局、高溢价)
    "六一中路": {"type": "A类-格局派", "desc": "招商证券福州六一中路，顶级锁仓格局游资，带飞溢价高"},
    "上海超短": {"type": "A类-格局派", "desc": "国泰君安上海江苏路，传统超级游资，协同力强"},
    "养家": {"type": "A类-格局派", "desc": "华鑫证券上海宛平南路，炒股养家，主线打造者"},
    "小鳄鱼": {"type": "A类-格局派", "desc": "南京证券南京大桥南路，新生代顶级游资，体量大"},

    # 砸盘派 / 散户派 (B类：次日必砸、散户多、溢价低)
    "拉萨天团": {"type": "B类-砸盘派", "desc": "东方财富拉萨团结路/团结路等，散户聚集地，筹码极其松动，次日易砸盘"},
    "量化打压": {"type": "B类-量化派", "desc": "中国国际金融上海分公司/华泰总部等，量化对倒，高抛低吸核按钮"},

    # 对倒派 (C类：频繁买卖，虚假繁荣)
    "对倒": {"type": "C类-对倒派", "desc": "同一营业部大量买入同时大量卖出"}
}


class SeatAnalyzer:
    """
    龙虎榜席位画像与资金“神韵”分析器
    """

    @classmethod
    def analyze_lhb(cls, lhb_df: pd.DataFrame) -> Dict[str, Any]:
        """
        分析龙虎榜营业部数据，识别游资派系与筹码强度。
        支持两种数据格式：
        - 新版 seat 级数据（seat_name, buy_amount, sell_amount, net_amount, buy_stocks）
        - 旧版 stock+seat 级数据（向后兼容）
        """
        if lhb_df is None or lhb_df.empty:
            return {
                "summary": "今日无龙虎榜数据或数据为空",
                "seat_types": [],
                "risk_warning": "无"
            }

        detected_seats = []
        has_a_class = False
        has_b_class = False

        for _, row in lhb_df.iterrows():
            seat_name = str(row.get("seat_name", ""))
            if not seat_name or seat_name == "nan":
                continue

            buy_amt = float(row.get("buy_amount", 0) or 0) / 1e4  # 万元
            sell_amt = float(row.get("sell_amount", 0) or 0) / 1e4
            net_amt = float(row.get("net_amount", 0) or 0) / 1e4

            # 新格式中 stock_name 可能在 buy_stocks 字段中
            stock_name = str(row.get("name", row.get("buy_stocks", "")))

            # 匹配著名的营业部特征
            for key, info in FAMOUS_SEATS.items():
                if key in seat_name:
                    if "A类" in info["type"]:
                        has_a_class = True
                    elif "B类" in info["type"]:
                        has_b_class = True

                    detected_seats.append({
                        "stock_name": stock_name,
                        "seat_name": seat_name,
                        "seat_type": info["type"],
                        "desc": info["desc"],
                        "buy_amt_wan": round(buy_amt, 2),
                        "net_amt_wan": round(net_amt, 2)
                    })

        # 生成“神韵”结论
        if has_a_class and not has_b_class:
            summary = "龙虎榜出现顶级【格局派】游资锁仓，主强买入，主力锁仓意愿极强，属于‘强共识’，明日溢价/高开概率大。"
            risk = "低"
        elif has_b_class and not has_a_class:
            summary = "龙虎榜大量出现【拉萨散户天团/量化砸盘派】，筹码结构极其松动，主力缺乏格局，明日注意低开砸盘风险。"
            risk = "高"
        elif has_a_class and has_b_class:
            summary = "龙虎榜呈【多空博弈分歧】状态，顶级游资与散户/量化对砸，换手极其充分，次日需看竞价弱转强信号。"
            risk = "中"
        else:
            summary = "龙虎榜主要为普通机构或游资营业部，筹码表现中规中矩。"
            risk = "中"

        return {
            "summary": summary,
            "risk_warning": risk,
            "detected_seats": detected_seats
        }
