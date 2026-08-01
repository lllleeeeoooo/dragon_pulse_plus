import logging
import time
import json
from typing import Dict, Any, Optional
import requests
from openai import OpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """
    统一 LLM API 调用客户端
    兼容 DeepSeek / OpenAI / Claude 及任何兼容 OpenAI 协议的大模型服务
    支持自动重试、超时与结构化响应处理
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None
    ):
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = base_url or settings.LLM_BASE_URL
        self.model = model or settings.LLM_MODEL
        self.temperature = temperature if temperature is not None else settings.LLM_TEMPERATURE
        self.timeout = timeout or settings.LLM_TIMEOUT
        self.max_retries = max_retries or settings.LLM_MAX_RETRIES

        # 初始化 OpenAI 客户端（SDK 层重试设为 0，由 generate() 方法统一控制重试次数）
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0
        )

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        module: str = "unknown"
    ) -> str:
        """
        同步生成文本响应。每次调用自动落库记录。
        :param module: 调用模块标识 (pre_market/call_auction/post_market/sell_advisor)
        """
        temp = temperature if temperature is not None else self.temperature

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        logger.info(f"发送 LLM 请求 [{self.model}] 模块: {module}")

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temp,
                )
                content = response.choices[0].message.content or ""
                tokens = response.usage.total_tokens if response.usage else 0
                logger.info(f"LLM 响应成功 (消耗 Tokens: {tokens})")

                # 落库
                self._persist_llm_log(module, system_prompt, user_prompt, content.strip(),
                                      tokens, True, "")
                return content.strip()
            except Exception as e:
                last_error = e
                logger.warning(f"LLM 请求失败 (第 {attempt}/{self.max_retries} 次): {e}")
                if attempt == self.max_retries:
                    logger.error("LLM 请求已达最大重试次数，尝试备用模型...")
                    self._persist_llm_log(module, system_prompt, user_prompt, "",
                                          0, False, str(e)[:200])
                else:
                    time.sleep(2.0)

        # 主模型全部失败 → 尝试备用模型
        if settings.LLM_BACKUP_BASE_URL and settings.LLM_BACKUP_MODEL:
            backup_model = settings.LLM_BACKUP_MODEL
            backup_key = settings.LLM_BACKUP_API_KEY or self.api_key
            logger.info(f"主模型 [{self.model}] 失败，切换备用模型 [{backup_model}]")
            try:
                backup_client = OpenAI(
                    api_key=backup_key,
                    base_url=settings.LLM_BACKUP_BASE_URL,
                    timeout=self.timeout,
                    max_retries=0
                )
                response = backup_client.chat.completions.create(
                    model=backup_model,
                    messages=messages,
                    temperature=temp,
                )
                content = response.choices[0].message.content or ""
                tokens = response.usage.total_tokens if response.usage else 0
                logger.info(f"备用模型 [{backup_model}] 响应成功 (消耗 Tokens: {tokens})")
                self._persist_llm_log(module, system_prompt, user_prompt, content.strip(),
                                      tokens, True, f"fallback:{backup_model}")
                return content.strip()
            except Exception as e:
                logger.error(f"备用模型 [{backup_model}] 也失败: {e}")

        return ""

    def _persist_llm_log(self, module: str, system_prompt: str, user_prompt: str,
                         response: str, tokens: int, success: bool, error_msg: str):
        """内部方法：LLM 调用记录落库，静默失败不影响主流程"""
        try:
            from database.services import LLMLogManager
            LLMLogManager.add_log(
                module=module,
                model=self.model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=response,
                tokens_used=tokens,
                success=success,
                error_msg=error_msg
            )
        except Exception as e:
            logger.warning(f"LLM 日志落库异常（不影响主流程）: {e}")


# 全局 LLM 单例客户端
llm_client = LLMClient()
