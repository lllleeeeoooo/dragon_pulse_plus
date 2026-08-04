"""
龙虎榜营业部画像管理服务
========================
席位分类来源优先级：人工标签(is_manual) > 自动行为分类(seat_type) > 内置名气字典兜底。

- seed_famous_seats():   将内置 FAMOUS_SEATS（六一中路等）作为人工种子灌入（幂等）
- sync_from_lhb():       每日从东财活跃营业部数据 upsert 各席位累计买卖统计，
                         并按行为重新自动分类（净买/净卖/对倒/单笔大小）
- get_seat_type():       按营业部名查询分类（DB 精确 → 人工种子子串 → 内置字典兜底）
"""
import datetime
import logging
from typing import Dict, Any, Optional

from config.settings import settings
from database.models import SeatProfile
from database.connection import db_manager

logger = logging.getLogger(__name__)

# 对倒派判定常量（买卖占比区间 + 当日买卖总额下限，元）
_DUODAO_RATIO_LO = 0.40
_DUODAO_RATIO_HI = 0.60
_DUODAO_MIN_AMOUNT = 2e9


class SeatProfileManager:
    """营业部画像管理服务"""

    @staticmethod
    def seed_famous_seats():
        """将内置 FAMOUS_SEATS 作为人工种子灌入（幂等），保证老名席位永远有人工标签优先。"""
        from core.seat_analyzer import FAMOUS_SEATS  # 延迟导入避免循环依赖
        today = datetime.date.today().strftime("%Y%m%d")
        session = db_manager.get_session()
        try:
            for key, info in FAMOUS_SEATS.items():
                existing = session.query(SeatProfile).filter(
                    SeatProfile.seat_name == key
                ).first()
                if existing:
                    continue
                session.add(SeatProfile(
                    seat_name=key,
                    first_seen=today,
                    last_seen=today,
                    appear_count=0,
                    is_manual=True,
                    manual_type=info["type"],
                    desc=info["desc"],
                    seat_type=info["type"],
                    is_active=True,
                ))
            session.commit()
            logger.info(f"龙虎榜名席位种子已就绪（{len(FAMOUS_SEATS)} 条人工标签）")
        except Exception as e:
            session.rollback()
            logger.warning(f"名席位种子初始化失败: {e}")
        finally:
            session.close()

    @staticmethod
    def sync_from_lhb(lhb_seats_df, trade_date: str):
        """
        每日同步活跃营业部数据到画像表（upsert 累计统计 + 重新行为分类）。
        :param lhb_seats_df: get_lhb_seats 返回的 DataFrame（含 seat_name/buy_amount/sell_amount/net_amount/buy_stock_count）
        :param trade_date:  日期 YYYYMMDD
        """
        from core.seat_analyzer import FAMOUS_SEATS  # 延迟导入
        if lhb_seats_df is None or getattr(lhb_seats_df, "empty", True):
            return
        # 先保证名席位种子存在（首次运行也自动灌入）
        SeatProfileManager.seed_famous_seats()

        session = db_manager.get_session()
        try:
            # 先按营业部聚合（同一天同席位可能出现多行，避免重复 add 触发唯一约束）
            grouped: Dict[str, Dict[str, float]] = {}
            for _, row in lhb_seats_df.iterrows():
                seat_name = str(row.get("seat_name", "")).strip()
                if not seat_name or seat_name == "nan":
                    continue
                try:
                    buy_amount = float(row.get("buy_amount", 0) or 0)
                    sell_amount = float(row.get("sell_amount", 0) or 0)
                    net_amount = float(row.get("net_amount", 0) or 0)
                    buy_stock_count = int(float(row.get("buy_stock_count", 0) or 0))
                except (ValueError, TypeError):
                    continue
                agg = grouped.setdefault(seat_name, {"buy": 0.0, "sell": 0.0, "net": 0.0, "stocks": 0.0})
                agg["buy"] += buy_amount
                agg["sell"] += sell_amount
                agg["net"] += net_amount
                agg["stocks"] += buy_stock_count

            updated = 0
            for seat_name, agg in grouped.items():
                profile = session.query(SeatProfile).filter(
                    SeatProfile.seat_name == seat_name
                ).first()
                if profile is None:
                    # 新营业部，落库观察
                    profile = SeatProfile(
                        seat_name=seat_name,
                        first_seen=trade_date,
                        last_seen=trade_date,
                        appear_count=0,
                        is_manual=False,
                    )
                    session.add(profile)

                profile.appear_count = (profile.appear_count or 0) + 1
                profile.buy_amount_total = (profile.buy_amount_total or 0) + agg["buy"]
                profile.sell_amount_total = (profile.sell_amount_total or 0) + agg["sell"]
                profile.net_amount_total = (profile.net_amount_total or 0) + agg["net"]
                profile.buy_stock_count_total = (profile.buy_stock_count_total or 0) + int(agg["stocks"])
                if agg["net"] > 0:
                    profile.net_positive_days = (profile.net_positive_days or 0) + 1
                profile.last_seen = trade_date
                profile.is_active = True

                # 自动分类：非人工标签且样本足够才定型
                if not profile.is_manual:
                    profile.seat_type = SeatProfileManager._classify(profile)
                updated += 1

            # 近 30 天未出现的席位标记为不活跃
            cutoff = (datetime.datetime.strptime(trade_date, "%Y%m%d") -
                      datetime.timedelta(days=30)).strftime("%Y%m%d")
            session.query(SeatProfile).filter(
                SeatProfile.is_active == True,
                SeatProfile.last_seen < cutoff
            ).update({"is_active": False}, synchronize_session="fetch")

            session.commit()
            if updated:
                logger.info(f"龙虎榜席位画像同步完成：更新 {updated} 个营业部（{trade_date}）")
        except Exception as e:
            session.rollback()
            logger.warning(f"龙虎榜席位画像同步失败: {e}")
        finally:
            session.close()

    @staticmethod
    def _classify(profile) -> str:
        """基于累计行为统计自动分类席位类型"""
        if (profile.appear_count or 0) < settings.SEAT_CLASSIFY_MIN_SAMPLES:
            return "未知"
        buy = profile.buy_amount_total or 0
        sell = profile.sell_amount_total or 0
        total = buy + sell
        if total <= 0:
            return "未知"
        buy_ratio = buy / total
        buy_stock_count = profile.buy_stock_count_total or 0
        avg_per_stock = buy / buy_stock_count if buy_stock_count > 0 else 0
        net_ratio = (profile.net_positive_days or 0) / profile.appear_count

        # 对倒派：买卖几乎对半且体量大（量化/对倒做 T）
        if _DUODAO_RATIO_LO <= buy_ratio <= _DUODAO_RATIO_HI and total >= _DUODAO_MIN_AMOUNT:
            return "对倒派"
        # 散户/拉萨风格：平均单只买入金额很小
        if avg_per_stock < settings.SEAT_AVG_BUY_THRESHOLD_WAN * 1e4:
            return "散户派"
        # 砸盘派：净买入天数占比很低
        if net_ratio <= settings.SEAT_NET_POSITIVE_ZSHA:
            return "砸盘派"
        # 格局派：净买入为主且有体量
        if net_ratio >= settings.SEAT_NET_POSITIVE_ZHIGE:
            return "格局派"
        return "未知"

    @staticmethod
    def get_seat_type(full_seat_name: str) -> Optional[Dict[str, Any]]:
        """
        查询营业部分类：DB 精确匹配(自动画像) → DB 人工种子子串匹配 → 内置名气字典兜底。
        返回 {"type": 分类, "desc": 描述}；未命中返回 None。
        """
        name = str(full_seat_name or "").strip()
        if not name or name == "nan":
            return None

        session = db_manager.get_session()
        try:
            # 1. DB 精确匹配（含自动画像）
            row = session.query(SeatProfile).filter(
                SeatProfile.seat_name == name
            ).first()
            if row:
                return {"type": row.manual_type or row.seat_type or "未知", "desc": row.desc or ""}
            # 2. DB 人工种子子串匹配（如 "六一中路" in "招商证券福州六一中路"）
            manual_rows = session.query(SeatProfile).filter(
                SeatProfile.is_manual == True
            ).all()
            for m in manual_rows:
                if m.seat_name and m.seat_name in name:
                    return {"type": m.manual_type or m.seat_type or "未知", "desc": m.desc or ""}
        finally:
            session.close()

        # 3. 内置名气字典兜底（DB 不可用/未同步时仍能识别老名席位）
        from core.seat_analyzer import FAMOUS_SEATS
        for key, info in FAMOUS_SEATS.items():
            if key in name:
                return {"type": info["type"], "desc": info["desc"]}
        return None
