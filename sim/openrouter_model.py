"""Camel model backend that calls OpenRouter API.

Drop-in replacement for ClaudeCliModel. Uses the same prompt injection +
JSON parsing approach for tool calling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
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
        # Running USD cost across calls, summed from each response's usage.cost.
        # Agents call concurrently within a round, so guard with a lock.
        self._cost_lock = threading.Lock()
        self._cost_accumulated = 0.0
        # Counter of LLM responses we replaced because they contained leaked
        # persona-template fingerprints (L3 output sanitization). The runner
        # reads + resets this per round, the same lifecycle as cost. Lock
        # shared with cost — same concurrency profile.
        self._sanitized_count = 0
        # Cooperative cancel signal. The runner sets this to a threading.Event
        # at sim creation; each call path checks it before talking to OpenRouter
        # and raises asyncio.CancelledError if set. None = cancellation disabled
        # (the OSS / local path that has no /conversations endpoint).
        self._cancel_event: "threading.Event | None" = None
        super().__init__(
            model_type="openrouter",
            model_config_dict={},
            api_key=self._or_key,
            timeout=timeout,
        )

    @property
    def cost_accumulated(self) -> float:
        """Total USD cost summed across calls since the last reset."""
        with self._cost_lock:
            return self._cost_accumulated

    def reset_cost(self) -> float:
        """Return the accumulated USD cost and zero the counter atomically."""
        with self._cost_lock:
            spent = self._cost_accumulated
            self._cost_accumulated = 0.0
            return spent

    def reset_sanitized_count(self) -> int:
        """Return the count of sanitized responses since the last reset and
        zero the counter atomically. Lifecycle parallel to reset_cost."""
        with self._cost_lock:
            n = self._sanitized_count
            self._sanitized_count = 0
            return n

    def _sanitize(self, content: str) -> str:
        """Apply L3 output filter. Replaces leaked content with an in-character
        refusal and increments the sanitized counter."""
        # Lazy import to avoid coupling sim/ to api/ in the OSS package shape
        # — local dev without an api module still imports OpenRouterModel.
        try:
            from api.security import sanitize_llm_output
        except ImportError:
            return content
        sanitized, was = sanitize_llm_output(content)
        if was:
            with self._cost_lock:
                self._sanitized_count += 1
        return sanitized

    def _record_cost(self, data: dict) -> None:
        """Add this response's usage.cost (USD) to the accumulator.

        OpenRouter includes usage.cost in every response automatically.
        A missing/null cost (e.g. an error payload) contributes 0 rather
        than crashing the call path — billing must never break inference.
        """
        cost = (data.get("usage") or {}).get("cost")
        if cost is None:
            return
        with self._cost_lock:
            self._cost_accumulated += float(cost)

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

    def _check_cancel(self) -> None:
        """Raise asyncio.CancelledError if the runner signaled cancel.

        Called before each LLM request so a cancel during a round aborts the
        remaining work instead of letting it complete and bill cost. The
        in-flight HTTP request (if any) still finishes — we eat that one
        call's cost — but the next 30 agents in the gather don't fire.
        """
        if self._cancel_event is not None and self._cancel_event.is_set():
            import asyncio
            raise asyncio.CancelledError("sim cancelled by user")

    def _call_api(self, messages: list[dict]) -> str:
        self._check_cancel()
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
            self._record_cost(data)
            return self._sanitize(data["choices"][0]["message"]["content"])

    async def _acall_api(self, messages: list[dict]) -> str:
        self._check_cancel()
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
            self._record_cost(data)
            return self._sanitize(data["choices"][0]["message"]["content"])

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
