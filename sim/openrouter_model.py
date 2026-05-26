"""Camel model backend that calls OpenRouter API.

Drop-in replacement for ClaudeCliModel. Uses the same prompt injection +
JSON parsing approach for tool calling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Type

import httpx
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.completion_usage import CompletionUsage
from pydantic import BaseModel

from camel.models.base_model import BaseModelBackend

from sim.claude_model import (
    TOOL_SYSTEM_PROMPT,
    _render_tool_definitions,
    _parse_tool_call,
)

OpenAIMessage = Dict[str, Any]

log = logging.getLogger("sim.openrouter")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Optional OpenRouter attribution headers (see https://openrouter.ai/docs/app-attribution).
# Set AGAR_HTTP_REFERER / AGAR_APP_TITLE to attribute requests to your deployment.
HTTP_REFERER = os.environ.get("AGAR_HTTP_REFERER", "")
APP_TITLE = os.environ.get("AGAR_APP_TITLE", "Agar")


def _attribution_headers() -> dict[str, str]:
    headers = {"X-Title": APP_TITLE}
    if HTTP_REFERER:
        headers["HTTP-Referer"] = HTTP_REFERER
    return headers
DEFAULT_MODEL = os.environ.get("AGAR_OPENROUTER_MODEL", "google/gemini-2.5-flash")


class OpenRouterModel(BaseModelBackend):
    """Model backend that calls OpenRouter for inference."""

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 60.0,
        api_key: str | None = None,
    ):
        self._or_model = model or DEFAULT_MODEL
        self._or_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._timeout = timeout
        super().__init__(
            model_type="openrouter",
            model_config_dict={},
            api_key=self._or_key,
            timeout=timeout,
        )

    def _messages_to_prompt(
        self,
        messages: List[OpenAIMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> list[dict]:
        """Convert CAMEL messages to OpenRouter chat format.

        Injects tool definitions into system prompt (same as ClaudeCliModel).
        """
        result = []
        system_parts = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )

            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                result.append({"role": role, "content": content})

        # Inject tool definitions into system prompt
        system_prompt = "\n\n".join(system_parts) if system_parts else ""
        if tools:
            tool_defs = _render_tool_definitions(tools)
            tool_prompt = TOOL_SYSTEM_PROMPT.format(tool_definitions=tool_defs)
            system_prompt = f"{system_prompt}\n\n{tool_prompt}" if system_prompt else tool_prompt

        if system_prompt:
            result.insert(0, {"role": "system", "content": system_prompt})

        return result

    def _build_request_body(self, messages: list[dict]) -> dict:
        return {
            "model": self._or_model,
            "messages": messages,
            "temperature": 0.9,
            "user": uuid.uuid4().hex,  # unique per request — breaks prompt caching
        }

    def _call_api(self, messages: list[dict]) -> str:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._or_key}",
                    **_attribution_headers(),
                },
                json=self._build_request_body(messages),
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _acall_api(self, messages: list[dict]) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._or_key}",
                    **_attribution_headers(),
                },
                json=self._build_request_body(messages),
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    def _make_response(
        self,
        content: str,
        tool_call: tuple[str, dict] | None = None,
    ) -> ChatCompletion:
        tool_calls = None
        finish_reason = "stop"

        if tool_call:
            tool_name, args = tool_call
            tool_calls = [
                ChatCompletionMessageToolCall(
                    id=f"call_{int(time.time())}_{tool_name}",
                    type="function",
                    function=Function(
                        name=tool_name,
                        arguments=json.dumps(args),
                    ),
                )
            ]
            finish_reason = "tool_calls"
            content = None

        return ChatCompletion(
            id=f"openrouter-{int(time.time())}",
            model=self._or_model,
            object="chat.completion",
            created=int(time.time()),
            choices=[
                Choice(
                    finish_reason=finish_reason,
                    index=0,
                    message=ChatCompletionMessage(
                        content=content,
                        role="assistant",
                        tool_calls=tool_calls,
                    ),
                    logprobs=None,
                )
            ],
            usage=CompletionUsage(
                completion_tokens=10,
                prompt_tokens=0,
                total_tokens=10,
            ),
        )

    def _run(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        chat_messages = self._messages_to_prompt(messages, tools)
        content = self._call_api(chat_messages)

        tool_call = None
        if tools:
            tool_call = _parse_tool_call(content)

        return self._make_response(content, tool_call)

    async def _arun(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        chat_messages = self._messages_to_prompt(messages, tools)
        content = await self._acall_api(chat_messages)

        tool_call = None
        if tools:
            tool_call = _parse_tool_call(content)

        return self._make_response(content, tool_call)

    @property
    def token_limit(self) -> int:
        return 1_000_000

    @property
    def token_counter(self):
        from camel.models.stub_model import StubTokenCounter
        return StubTokenCounter()

    def check_model_config(self):
        pass

    @property
    def stream(self):
        return False
