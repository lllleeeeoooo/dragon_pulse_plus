"""
立案调查 / 违规处罚 / 监管警示 记录管理
========================================

数据源：东方财富个股风险提示 (ak.stock_gsrl_gsdt_em)
同步频率：每日 08:30 盘前简报时自动同步
过滤策略：立案调查股票在 filter_stocks() 中被源头剔除（1 小时缓存黑名单）
过期策略：公告超过 365 天的记录在每日 04:00 自动标记为 inactive
手动操作：POST /data/investigations/resolve?code=000001

表结构 (investigation_records):
  - code/name:        股票代码/名称
  - event_type:       事件类型（立案调查/违规处罚/监管警示/问询函/通报批评 等）
  - event_content:    事件详细内容（公告原文，截断至 2000 字符）
  - announce_date:    公告日期 YYYYMMDD
  - detected_date:    系统检测到该事件的日期 YYYYMMDD
  - is_active:        是否仍处于风险状态（True=活跃 / False=已过期或手动标记解决）
"""

import logging
from typing import Dict, Any, List, Optional
import datetime as _dt

from database.connection import db_manager
from database.models import InvestigationRecord

logger = logging.getLogger(__name__)

# 风险事件关键词：事件类型包含任一关键词即判定为风险事件，需要落库
# 注意：这些是东方财富返回的原始事件类型中的匹配词，不是完整的类型名称
_RISK_KEYWORDS = [
    "立案",       # 立案调查（最严重，证监会/交易所正式立案）
    "调查",       # 各类调查
    "处罚",       # 行政处罚、纪律处分
    "警示",       # 监管警示函、风险警示
    "监管",       # 监管函、监管措施
    "问询",       # 问询函、关注函
    "谴责",       # 通报谴责
    "通报批评",   # 交易所通报批评
    "纪律处分",   # 纪律处分决定
]


class InvestigationManager:
    """
    立案调查与风险事件管理服务

    核心职责：
    1. 每日从东方财富同步风险事件 → sync_from_gsrl()
    2. 提供黑名单供源头过滤 → get_blacklist_codes()
    3. 提供单只股票风险查询 → check_code()
    4. 自动过期清理 → expire_old()
    5. 手动标记已解决 → mark_resolved()

    注意：除 sync_from_gsrl() 外，其余方法均只读/更新本地数据库，不调用外部 API。
    """

    # =========================================================================
    # 数据同步
    # =========================================================================

    @staticmethod
    def sync_from_gsrl(date_str: str = None) -> List[Dict[str, Any]]:
        """
        从东方财富个股风险提示接口同步数据到本地数据库。

        执行流程：
        1. 调用 ak.stock_gsrl_gsdt_em(date=date_str) 获取当日所有个股风险事件
        2. 按 _RISK_KEYWORDS 过滤，仅保留立案调查/违规处罚/监管警示等风险类事件
           （过滤掉资产出售、股份质押、对外担保等非风险事件）
        3. 对每条风险事件做幂等检查（同代码+同类型+同公告日期不重复写入）
        4. 新记录写入 investigation_records 表，is_active=True
        5. 返回新增记录列表（用于日志/Bark 推送）

        :param date_str: 日期 YYYYMMDD，默认今天
        :return: 新增的风险记录列表 [{"code","name","event_type","event_content","announce_date"}, ...]
                无新增时返回空列表
        """
        if date_str is None:
            date_str = _dt.datetime.now().strftime("%Y%m%d")

        # 1. 调用 akshare 获取当日风险提示数据
        #    数据可能滞后 1-2 天，今日失败则回退到最近 3 天
        import akshare as ak
        df = None
        dates_to_try = [date_str]
        # 回退窗口：今天 → 昨天 → 前天
        base = _dt.datetime.strptime(date_str, "%Y%m%d")
        for i in range(1, 3):
            back = base - _dt.timedelta(days=i)
            dates_to_try.append(back.strftime("%Y%m%d"))

        for d in dates_to_try:
            try:
                df = ak.stock_gsrl_gsdt_em(date=d)
                if df is not None and not df.empty:
                    if d != date_str:
                        logger.info(f"今日({date_str})无风险数据，使用 {d} 数据")
                    break
            except Exception:
                continue

        if df is None or df.empty:
            return []

        # 2. 解析列（列名优先、位置兜底——akshare 列名/顺序可能随版本调整，
        #    硬编码位置索引会导致静默失败、表一直空）
        cols = {str(c): c for c in df.columns}

        def _pick(names, pos):
            for n in names:
                if n in cols:
                    return cols[n]
            if pos < len(df.columns):
                return df.columns[pos]
            return None

        code_col = _pick(["代码", "股票代码"], 1)      # 000001
        name_col = _pick(["名称", "简称", "股票名称"], 2)  # 接口实为"简称"
        type_col = _pick(["事件类型", "类型"], 3)       # 如"立案调查"
        content_col = _pick(["具体事项", "事件内容", "内容"], 4)  # 公告全文
        date_col = _pick(["交易日", "公告日期", "日期"], 5)
        if any(c is None for c in (code_col, name_col, type_col, content_col, date_col)):
            logger.warning(f"个股风险提示返回格式异常(列={list(df.columns)})，跳过同步")
            return []

        new_records = []
        session = db_manager.get_session()
        try:
            for _, row in df.iterrows():
                event_type = str(row[type_col] or "")

                # 3. 仅保留风险类事件（立案/处罚/监管等），
                #    过滤掉资产出售、股份质押等与风险无关的事件
                if not any(kw in event_type for kw in _RISK_KEYWORDS):
                    continue

                # 4. 数据清洗（None 防御：字段缺失的行跳过，避免 str(None) 污染）
                code = str(row[code_col] or "").zfill(6)     # 补齐 6 位
                announce_date = str(row[date_col] or "")[:10].replace("-", "")
                if not code or not announce_date or code == "000000":
                    continue
                name = str(row[name_col] or "")
                content = str(row[content_col] or "")
                if len(content) > 2000:
                    content = content[:2000]                 # 截断防超长

                # 5. 幂等检查：同一股票的同一类型、同一公告日期不重复落库
                existing = session.query(InvestigationRecord).filter(
                    InvestigationRecord.code == code,
                    InvestigationRecord.event_type == event_type,
                    InvestigationRecord.announce_date == announce_date,
                ).first()
                if existing:
                    continue

                # 6. 落库
                record = InvestigationRecord(
                    code=code,
                    name=name,
                    event_type=event_type,
                    event_content=content,
                    announce_date=announce_date,
                    detected_date=date_str,     # 系统检测日期 = 数据同步日期
                    is_active=True,             # 新记录默认活跃
                )
                session.add(record)
                new_records.append({
                    "code": code,
                    "name": name,
                    "event_type": event_type,
                    "event_content": content[:200],   # 日志只输出前 200 字
                    "announce_date": announce_date,
                })

            if new_records:
                session.commit()
                logger.warning(
                    f"⚠ 检测到 {len(new_records)} 条新增风险事件！"
                )
                for r in new_records:
                    logger.warning(
                        f"  {r['code']} {r['name']} | {r['event_type']} | "
                        f"{r['announce_date']}"
                    )
            else:
                logger.info(
                    f"今日 ({date_str}) 无新增立案/处罚/监管风险事件"
                )
        except Exception as e:
            session.rollback()
            logger.error(f"同步风险事件落库失败: {e}")
        finally:
            session.close()

        return new_records

    # =========================================================================
    # 查询
    # =========================================================================

    @staticmethod
    def get_active_investigations() -> List[Dict[str, Any]]:
        """
        获取所有活跃（未过期）的调查/处罚记录。

        用途：看板展示、API 查询 /data/investigations

        :return: 按公告日期降序排列的记录列表，每个记录包含：
                 code, name, event_type, event_content(截断300字),
                 announce_date, detected_date
        """
        session = db_manager.get_session()
        try:
            records = (
                session.query(InvestigationRecord)
                .filter(InvestigationRecord.is_active == True)
                .order_by(InvestigationRecord.announce_date.desc())
                .all()
            )
            return [{
                "code": r.code,
                "name": r.name,
                "event_type": r.event_type,
                "event_content": r.event_content[:300] if r.event_content else "",
                "announce_date": r.announce_date,
                "detected_date": r.detected_date,
            } for r in records]
        finally:
            session.close()

    @staticmethod
    def get_blacklist_codes() -> set:
        """
        获取应排除的股票代码黑名单集合。

        用途：filter_stocks() 在每次全市场快照过滤时调用，
              将立案调查股票与科创板/北交所/ST 一起从候选池中剔除。
              当前使用 1 小时 TTL 缓存（见 data/fetcher_spot.py），
              避免 15 秒轮询时高频查库。

        :return: 活跃风险股票的代码集合，如 {"000001", "600519"}
        """
        session = db_manager.get_session()
        try:
            records = (
                session.query(InvestigationRecord.code)
                .filter(InvestigationRecord.is_active == True)
                .distinct()
                .all()
            )
            return {r[0] for r in records}
        finally:
            session.close()

    @staticmethod
    def check_code(code: str) -> Optional[Dict[str, Any]]:
        """
        检查某只股票是否存在活跃风险记录。

        用途：下单前快速校验，盘中监控时可对持仓股做风险提示。
              也供 API /holdings 查询时附加风险标签。

        :param code: 股票代码（6 位字符串）
        :return: 有风险时返回 {"code","name","event_type","event_content","announce_date"}
                 无风险时返回 None
        """
        session = db_manager.get_session()
        try:
            record = (
                session.query(InvestigationRecord)
                .filter(
                    InvestigationRecord.code == code,
                    InvestigationRecord.is_active == True,
                )
                .order_by(InvestigationRecord.announce_date.desc())
                .first()
            )
            if record:
                return {
                    "code": record.code,
                    "name": record.name,
                    "event_type": record.event_type,
                    "event_content": (
                        record.event_content[:300]
                        if record.event_content else ""
                    ),
                    "announce_date": record.announce_date,
                }
            return None
        finally:
            session.close()

    # =========================================================================
    # 状态管理
    # =========================================================================

    @staticmethod
    def expire_old(days: int = 365):
        """
        自动过期：将公告日期超过 N 天的记录标记为 is_active=False。

        调用时机：每日 04:00 由 main.py 中的定时清理任务调用，
                  与日志清理、龙头过期标记一起执行。

        设计考量：
        - 默认 365 天（一年），因为立案调查从立案到结案通常数月甚至跨年
        - 过期不等于"已解决"，只是从活跃黑名单中移除（不再自动过滤）
        - 如需永久保留历史记录，不会删除数据，只是 is_active 置为 False

        :param days: 公告超过多少天后标记为失效，默认 365
        """
        cutoff = (
            _dt.datetime.now() - _dt.timedelta(days=days)
        ).strftime("%Y%m%d")
        session = db_manager.get_session()
        try:
            count = (
                session.query(InvestigationRecord)
                .filter(
                    InvestigationRecord.is_active == True,
                    InvestigationRecord.announce_date < cutoff,
                )
                .update({"is_active": False}, synchronize_session="fetch")
            )
            if count:
                session.commit()
                logger.info(
                    f"立案调查过期标记: {count} 条记录（>{days}天）标记为已失效"
                )
        except Exception as e:
            session.rollback()
            logger.warning(f"立案调查过期标记失败: {e}")
        finally:
            session.close()

    @staticmethod
    def mark_resolved(code: str) -> bool:
        """
        手动标记某只股票的所有活跃风险记录为已解决。

        调用时机：用户在确认某只股票的立案调查/处罚已结案后，
                  通过 API POST /data/investigations/resolve?code=000001 调用。

        注意：这会将该股票的所有活跃风险记录（可能有多条不同类型/日期的）
              一次性全部标记为 is_active=False。

        :param code: 股票代码（6 位字符串）
        :return: True=操作成功，False=操作失败（DB 异常）
        """
        session = db_manager.get_session()
        try:
            count = (
                session.query(InvestigationRecord)
                .filter(
                    InvestigationRecord.code == code,
                    InvestigationRecord.is_active == True,
                )
                .update({"is_active": False}, synchronize_session="fetch")
            )
            session.commit()
            if count:
                logger.info(
                    f"手动标记 {code} 的 {count} 条风险记录为已解决"
                )
            return True
        except Exception as e:
            session.rollback()
            logger.warning(f"标记 {code} 风险已解决失败: {e}")
            return False
        finally:
            session.close()
