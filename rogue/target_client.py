"""被测 agent 客户端：OpenAI 兼容 /chat/completions 接口。"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx


class TargetAgentError(RuntimeError):
    """调用被测 agent 失败。"""


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    path = urlsplit(endpoint).path.lower()
    if path.endswith("/chat/completions"):
        return endpoint
    if path.endswith("/v1"):
        return endpoint + "/chat/completions"
    return endpoint + "/chat/completions"


def _extract_content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 多模态分段格式
        return "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and "text" in p
        )
    return ""


class TargetAgentClient:
    def __init__(
        self,
        endpoint: str,
        api_token: str = "",
        model: str = "",
        timeout_seconds: float = 120.0,
    ):
        self.endpoint = _normalize_endpoint(endpoint)
        self.model = model
        self._headers = {"Content-Type": "application/json"}
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"
        self._timeout = httpx.Timeout(timeout_seconds, connect=15.0)

    async def chat(self, messages: list[dict]) -> str:
        """发送多轮对话，返回被测 agent 的回复文本。"""
        payload = {"messages": messages}
        if self.model:
            payload["model"] = self.model

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self.endpoint, json=payload, headers=self._headers)
                if resp.status_code >= 500:
                    raise TargetAgentError(f"被测 agent 返回 {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()
                return self._parse_reply(data)
            except (httpx.TimeoutException, httpx.TransportError, TargetAgentError) as err:
                last_err = err
                await asyncio.sleep(2.0 * (attempt + 1))
        raise TargetAgentError(f"调用被测 agent 失败(已重试 3 次): {last_err}") from last_err

    @staticmethod
    def _parse_reply(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise TargetAgentError(f"被测 agent 响应缺少 choices: {str(data)[:300]}")
        text = _extract_content(choices[0].get("message", {}))
        if not text.strip():
            raise TargetAgentError("被测 agent 返回了空回复")
        return text
