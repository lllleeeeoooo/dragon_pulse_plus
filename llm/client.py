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
    ========================

    兼容所有 OpenAI 协议的大模型服务（DeepSeek / OpenAI / Claude / Moonshot 等）。
    全局单例 llm_client 在模块底部创建，整个系统共享同一个客户端实例。

    核心流程 (generate 方法):
    ┌─────────────┐    失败重试(最多N次)    全部失败
    │ 主模型调用   │ ─────────────────────→ ──────────→ 备用模型
    │ (LLM_MODEL)  │                          │         (LLM_BACKUP_*)
    └──────┬──────┘                          │              │
           │ 成功                             │ 成功         │ 失败
           ▼                                  ▼              ▼
      返回内容 + 落库                    返回内容 + 落库   返回 ""

    每次调用自动记录到 llm_logs 表：
    - module: 调用来源（pre_market/call_auction/post_market/sell_advisor）
    - system_prompt / user_prompt: 完整提示词
    - response: LLM 回复全文
    - tokens_used: Token 消耗量
    - success: 成功/失败
    - error_msg: 失败原因（含 fallback: 前缀标识备用模型）
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
        self.timeout = timeout if timeout is not None else settings.LLM_TIMEOUT  # 审计🟡⑥：允许显式传 0
        self.max_retries = max_retries if max_retries is not None else settings.LLM_MAX_RETRIES

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
        同步生成 LLM 文本响应（系统唯一的 LLM 调用入口）。

        执行逻辑：
        1. 组装 system + user 消息
        2. 主模型调用（最多 self.max_retries 次，每次失败间隔 2 秒）
        3. 每次调用（成功/失败）自动写入 llm_logs 表
        4. 主模型全部失败后，自动降级到备用模型（LLM_BACKUP_*）
        5. 备用模型也失败则返回空字符串 ""

        :param system_prompt: 系统提示词（角色设定、输出格式要求）
        :param user_prompt:   用户提示词（具体问题、数据上下文）
        :param temperature:   覆盖默认温度，None 则用 settings.LLM_TEMPERATURE
        :param module:        调用模块标识，用于日志分组
                              pre_market / call_auction / post_market / sell_advisor
        :return: LLM 响应文本，失败时返回空字符串 ""
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
                self._persist_llm_log(module, system_prompt, user_prompt, "",
                                      0, False, f"主备均失败 fallback:{backup_model} {str(e)[:200]}")  # 审计🟡⑦

        return ""

    def _persist_llm_log(self, module: str, system_prompt: str, user_prompt: str,
                         response: str, tokens: int, success: bool, error_msg: str):
        """
        将每次 LLM 调用的完整上下文写入 llm_logs 表。
        刻意捕获所有异常——落库失败绝不能影响主业务流程。
        """
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
# 所有模块通过 `from llm.client import llm_client` 引用此实例
# 启动时自动读取 .env 中的 LLM_* 配置初始化
llm_client = LLMClient()
