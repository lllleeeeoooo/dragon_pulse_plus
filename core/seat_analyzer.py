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
        分类来源：SeatProfileManager（DB 画像：人工种子 + 行为自动分类），
        DB 未命中时回退内置 FAMOUS_SEATS 名气字典。
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

        from database import SeatProfileManager  # 延迟导入避免循环依赖

        detected_seats = []
        has_a_class = False   # 格局派（净买入为主）
        has_b_class = False   # 砸盘/散户派
        has_c_class = False   # 对倒派
        a_class_net_total = 0.0  # 格局派席位当日净买入汇总（万元）
        a_class_selling = []     # 格局派但当日净卖出的席位名

        for _, row in lhb_df.iterrows():
            seat_name = str(row.get("seat_name", ""))
            if not seat_name or seat_name == "nan":
                continue

            buy_amt = float(row.get("buy_amount", 0) or 0) / 1e4  # 万元
            sell_amt = float(row.get("sell_amount", 0) or 0) / 1e4
            net_amt = float(row.get("net_amount", 0) or 0) / 1e4

            # 新格式中 stock_name 可能在 buy_stocks 字段中
            stock_name = str(row.get("name", row.get("buy_stocks", "")))

            # 分类查询：DB 画像（人工种子 + 行为自动）→ 内置名气字典兜底
            profile = SeatProfileManager.get_seat_type(seat_name)
            seat_type = profile.get("type", "") if profile else ""
            desc = profile.get("desc", "") if profile else ""

            if "格局" in seat_type or "A类" in seat_type:
                has_a_class = True
                a_class_net_total += net_amt
                if net_amt < 0:
                    a_class_selling.append(seat_name)
            elif "砸盘" in seat_type or "散户" in seat_type or "B类" in seat_type:
                has_b_class = True
            elif "对倒" in seat_type or "C类" in seat_type:
                has_c_class = True

            # 只把有明确分类的营业部列入"核心游资动向"
            if seat_type and seat_type not in ("未知", "普通/未知"):
                detected_seats.append({
                    "stock_name": stock_name,
                    "seat_name": seat_name,
                    "seat_type": seat_type,
                    "desc": desc,
                    "buy_amt_wan": round(buy_amt, 2),
                    "net_amt_wan": round(net_amt, 2)
                })

        # 生成"神韵"结论
        if has_a_class and not has_b_class and not has_c_class:
            if a_class_net_total < 0:
                # 格局派席位整体净卖出 → 出货迹象，不再是"强共识"
                selling_desc = f"（{','.join(a_class_selling[:3])}）" if a_class_selling else ""
                summary = (f"龙虎榜【格局派】游资席位今日整体净卖出{selling_desc}，出货迹象明显，主力兑现意愿强，"
                           f"明日谨防高开回落、溢价压缩。")
                risk = "中"
            else:
                summary = "龙虎榜出现【格局派】游资锁仓，主强买入，主力锁仓意愿极强，属于'强共识'，明日溢价/高开概率大。"
                risk = "低"
        elif has_b_class and not has_a_class and not has_c_class:
            summary = "龙虎榜大量出现【散户天团/砸盘派】，筹码结构极其松动，主力缺乏格局，明日注意低开砸盘风险。"
            risk = "高"
        elif has_a_class and has_b_class:
            summary = "龙虎榜呈【多空博弈分歧】状态，格局派与散户/砸盘派对砸，换手极其充分，次日需看竞价弱转强信号。"
            risk = "中"
        elif has_c_class and not has_a_class and not has_b_class:
            summary = "龙虎榜以【对倒派】为主，买卖几乎对半，疑似量化对倒制造虚假繁荣，警惕诱多。"
            risk = "中"
        else:
            summary = "龙虎榜主要为普通机构或游资营业部，筹码表现中规中矩。"
            risk = "中"

        return {
            "summary": summary,
            "risk_warning": risk,
            "detected_seats": detected_seats
        }
