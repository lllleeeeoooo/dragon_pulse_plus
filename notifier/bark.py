import logging
import re
import requests
from typing import Optional, Dict, Any, List

from config.settings import settings

logger = logging.getLogger(__name__)

# Bark 单条消息上限，留余量
BARK_MAX_CHARS = 1000  # 中文每字3字节，1000字≈3000字节，留安全余量


def _strip_json_block(text: str) -> str:
    """去掉所有 JSON 代码块（```json ... ```），只留用户可读的 Markdown。
    所有 Bark 推送统一在此去结构化 JSON，调用方无需重复剥。"""
    return re.sub(r"```json\s*.*?\s*```", "", text, flags=re.DOTALL).strip()


def _split_body(body: str, max_chars: int = BARK_MAX_CHARS) -> List[str]:
    """将长文本按段落边界拆分为多个 chunk，每个不超过 max_chars"""
    if len(body) <= max_chars:
        return [body]

    chunks = []
    paragraphs = body.split("\n")
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 1 <= max_chars:
            current += ("\n" + p) if current else p
        else:
            if current:
                chunks.append(current)
            # 如果单段就超长，强制截断
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i + max_chars])
                current = ""
            else:
                current = p
    if current:
        chunks.append(current)
    return chunks if chunks else [body[:max_chars]]


class BarkNotifier:
    """
    Bark 消息推送封装类。
    超长内容自动分批推送，末尾 JSON 块自动去除。
    内置频率限制：每分钟最多推送 MAX_PER_MINUTE 条，防止极端行情下刷屏。
    """

    MAX_PER_MINUTE = 8

    def __init__(
        self,
        token: Optional[str] = None,
        server_url: Optional[str] = None,
        group: Optional[str] = None,
        sound: Optional[str] = None,
        enabled: Optional[bool] = None
    ):
        self.token = token or settings.BARK_TOKEN
        self.server_url = (server_url or settings.BARK_SERVER_URL).rstrip("/")
        self.group = group or settings.BARK_GROUP
        self.sound = sound or settings.BARK_SOUND
        self.enabled = enabled if enabled is not None else settings.BARK_ENABLED
        self._send_timestamps: List[float] = []

    def _persist_log(self, title: str, body: str, group: str, level: str,
                     sound: str, success: bool, error_msg: str = ""):
        try:
            from database.services import PushLogManager
            PushLogManager.add_log(
                title=title, body=body, push_group=group, level=level,
                send_success=success, error_msg=error_msg
            )
        except Exception as e:
            logger.warning(f"推送日志落库异常: {e}")

    def _do_send(self, title: str, body: str, group: str, level: str,
                 sound: str) -> bool:
        """单次发送，不处理分片"""
        if not self.enabled:
            logger.info(f"[Bark 已禁用] 跳过推送: {title}")
            self._persist_log(title, body, group, level, sound, False, "Bark推送已禁用")
            return False

        if not self.token or self.token == "your_bark_token_here":
            logger.warning(f"[Bark 未配置 Token] 无法推送消息: {title}")
            self._persist_log(title, body, group, level, sound, False, "Bark Token未配置")
            return False

        endpoint = f"{self.server_url}/{self.token}"
        payload: Dict[str, Any] = {
            "title": title, "markdown": body, "group": group,
            "sound": sound, "level": level, "isArchive": 1,
            "ttl": 604800,  # 7 天后自动清理历史记录和通知中心
        }

        try:
            # 记录 payload 字节大小，方便排查 413
            import json
            payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            logger.debug(f"Bark 推送: title={title[:30]} body_bytes={payload_bytes}")

            response = requests.post(endpoint, json=payload, timeout=10)
            if response.status_code != 200:
                error_msg = f"Bark HTTP {response.status_code}: {response.text[:100]}"
                logger.error(error_msg)
                self._persist_log(title, body, group, level, sound, False, error_msg)
                return False
            if not response.text or not response.text.strip():
                self._persist_log(title, body, group, level, sound, False, "Bark 返回空响应")
                return False
            res_json = response.json()
            if res_json.get("code") == 200:
                logger.info(f"Bark 消息推送成功: {title}")
                self._persist_log(title, body, group, level, sound, True)
                return True
            else:
                self._persist_log(title, body, group, level, sound, False, str(res_json)[:200])
                return False
        except Exception as e:
            self._persist_log(title, body, group, level, sound, False, str(e)[:200])
            return False

    def send(
        self,
        title: str,
        body: str,
        group: Optional[str] = None,
        sound: Optional[str] = None,
        level: str = "active",
        url: Optional[str] = None,
        icon: Optional[str] = None
    ) -> bool:
        """
        发送 Bark 消息通知。超长内容自动分批，末尾 JSON 块自动去除。
        内置频率限制防止刷屏。
        """
        import time as _time

        # 频率限制：清理超过60秒的时间戳，检查是否超限
        now = _time.time()
        self._send_timestamps = [t for t in self._send_timestamps if now - t < 60]
        if len(self._send_timestamps) >= self.MAX_PER_MINUTE:
            logger.warning(f"[Bark 频率限制] 每分钟已推送{self.MAX_PER_MINUTE}条，跳过: {title[:30]}")
            self._persist_log(title, body, group or self.group, level, sound or self.sound, False, "频率限制跳过")
            return False
        self._send_timestamps.append(now)
        resolved_group = group or self.group
        resolved_sound = sound or self.sound

        # 去掉末尾的 JSON 代码块（非人类阅读内容）
        body = _strip_json_block(body)

        # 分批
        chunks = _split_body(body)
        total = len(chunks)

        all_ok = True
        for i, chunk in enumerate(reversed(chunks)):
            seq = total - i  # 编号保持 3/3, 2/3, 1/3 倒序发送
            chunk_title = f"{title} ({seq}/{total})" if total > 1 else title
            ok = self._do_send(chunk_title, chunk, resolved_group, level, resolved_sound)
            if not ok:
                all_ok = False

        return all_ok


# 全局默认单例实例
bark_notifier = BarkNotifier()
