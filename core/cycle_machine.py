import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class EmotionCycleMachine:
    """
    A-share short-term emotion cycle state machine.
    Phases: 冰点 -> 启动 -> 发酵 -> 高潮 -> 退潮 -> 冰点
    Transitions are strictly ordered (no phase skipping) with an emergency drop to 冰点.
    """

    PHASES = ["冰点", "启动", "发酵", "高潮", "退潮"]
    PHASE_ORDER = {phase: i for i, phase in enumerate(PHASES)}

    @classmethod
    def determine_phase(cls, today_emotion: dict, yesterday_phase: str) -> Dict[str, Any]:
        """
        Determine today's cycle phase based on today's emotion vector and yesterday's phase.

        :param today_emotion: dict with keys: sentiment_index, yield_rate, height, zt_count,
                              dt_count, zhaban_rate, breadth, seal_force_ratio
        :param yesterday_phase: previous trading day's cycle_stage string
        :return: {"phase": str, "reason": str, "transition": bool, "yesterday": str}
        """
        si = today_emotion.get("sentiment_index", 50)
        premium = today_emotion.get("yield_rate", 0)
        height = today_emotion.get("height", 0)
        zt_count = today_emotion.get("zt_count", 0)
        dt_count = today_emotion.get("dt_count", 0)
        zhaban_rate = today_emotion.get("zhaban_rate", 0)

        if yesterday_phase not in cls.PHASES:
            yesterday_phase = "冰点"

        # Emergency drop: extreme panic overrides everything
        if dt_count >= 20 or premium <= -4.0:
            return cls._result("冰点", f"极端恐慌(跌停{dt_count}家/溢价{premium}%)，紧急降至冰点",
                               yesterday_phase)

        # Determine based on yesterday's phase and transition rules
        if yesterday_phase == "冰点":
            if premium >= 0 and dt_count < 10 and si >= 30:
                return cls._result("启动", f"溢价转正({premium}%)+跌停缓和({dt_count}家)+情绪回升({si}分)，冰点->启动",
                                   yesterday_phase)
            return cls._result("冰点", f"溢价{premium}%/跌停{dt_count}/情绪{si}分，尚未脱离冰点",
                               yesterday_phase)

        elif yesterday_phase == "启动":
            # Can advance to 发酵 or regress to 冰点
            if height >= 4 and zt_count >= 30 and premium >= 1.0 and zhaban_rate < 30:
                return cls._result("发酵", f"高度{height}板+涨停{zt_count}家+溢价{premium}%+炸板率{zhaban_rate}%，启动->发酵",
                                   yesterday_phase)
            if premium <= -2.0 or dt_count >= 15:
                return cls._result("冰点", f"溢价崩塌({premium}%)/跌停爆发({dt_count}家)，启动->冰点",
                                   yesterday_phase)
            return cls._result("启动", f"高度{height}/涨停{zt_count}/溢价{premium}%，启动期延续",
                               yesterday_phase)

        elif yesterday_phase == "发酵":
            # Can advance to 高潮 or regress to 启动
            if si >= 65 and (zt_count >= 60 or height >= 7):
                return cls._result("高潮", f"情绪{si}分+涨停{zt_count}家+高度{height}板，发酵->高潮",
                                   yesterday_phase)
            if premium <= -1.0 or si < 35:
                return cls._result("启动", f"溢价回落({premium}%)/情绪下降({si}分)，发酵->启动",
                                   yesterday_phase)
            return cls._result("发酵", f"情绪{si}/涨停{zt_count}/高度{height}，发酵期延续",
                               yesterday_phase)

        elif yesterday_phase == "高潮":
            # Can advance to 退潮 or stay
            if (zhaban_rate >= 35 and premium < 1.0) or premium <= -1.5:
                return cls._result("退潮", f"炸板率{zhaban_rate}%+溢价{premium}%，高潮->退潮",
                                   yesterday_phase)
            return cls._result("高潮", f"情绪{si}/炸板{zhaban_rate}%/溢价{premium}%，高潮延续(警惕退潮)",
                               yesterday_phase)

        elif yesterday_phase == "退潮":
            # Can advance to 冰点 or recover to 启动 (V-shaped reversal)
            if premium <= -2.0 and dt_count >= 10 and si < 30:
                return cls._result("冰点", f"溢价{premium}%+跌停{dt_count}家+情绪{si}分，退潮->冰点",
                                   yesterday_phase)
            if premium >= 0.5 and dt_count < 5 and si >= 40:
                return cls._result("启动", f"溢价回正({premium}%)+跌停消退({dt_count}家)，退潮V反->启动",
                                   yesterday_phase)
            return cls._result("退潮", f"溢价{premium}%/跌停{dt_count}/情绪{si}，退潮期延续",
                               yesterday_phase)

        return cls._result("冰点", "未知状态，降级为冰点", yesterday_phase)

    @classmethod
    def _result(cls, phase: str, reason: str, yesterday_phase: str) -> Dict[str, Any]:
        transition = (phase != yesterday_phase)
        if transition:
            logger.info(f"情绪周期转换: {yesterday_phase} -> {phase} | {reason}")
        return {
            "phase": phase,
            "reason": reason,
            "transition": transition,
            "yesterday": yesterday_phase
        }

    @classmethod
    def get_trading_stance(cls, phase: str) -> Dict[str, Any]:
        """
        Get recommended trading stance for a given cycle phase.
        Used by the intraday monitor to modulate aggressiveness.
        """
        stances = {
            "冰点": {
                "allow_auto_buy": False,
                "stance": "空仓观望",
                "desc": "现金为王，不做任何买入",
            },
            "启动": {
                "allow_auto_buy": True,
                "only_recommended": True,
                "stance": "精选试错",
                "desc": "只买复盘推荐标的，小仓位试错",
            },
            "发酵": {
                "allow_auto_buy": True,
                "only_recommended": False,
                "stance": "积极进攻",
                "desc": "全力进攻，接力打板，加大仓位",
            },
            "高潮": {
                "allow_auto_buy": True,
                "only_recommended": False,
                "stance": "逢高减仓",
                "desc": "高位标的止盈，不追高，只做确定性强的",
            },
            "退潮": {
                "allow_auto_buy": False,
                "stance": "防守为主",
                "desc": "停止买入，持仓逢高减仓",
            },
        }
        return stances.get(phase, stances["冰点"])
