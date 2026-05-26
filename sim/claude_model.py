"""Camel model backend that shells out to `claude -p`.

Routes OASIS agent LLM calls through the local Claude CLI, using
the user's existing Max subscription auth. No API key needed.

Tool calling is implemented via prompt injection + JSON parsing,
the same approach Ollama uses internally.
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Type, Union

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

OpenAIMessage = Dict[str, Any]

_prompt_log = logging.getLogger("sim.prompts")


TOOL_SYSTEM_PROMPT = """\
You have access to the following tools. To use a tool, respond with ONLY \
a JSON object in this exact format, nothing else:

{{"tool_name": "<name>", "args": {{<arguments>}}}}

Available tools:
{tool_definitions}

Rules:
- Respond with exactly ONE tool call as a JSON object.
- Do NOT wrap in markdown code blocks.
- Do NOT add any text before or after the JSON.
- If you have nothing to do, use: {{"tool_name": "do_nothing", "args": {{}}}}"""


def _render_tool_definitions(tools: List[Dict[str, Any]]) -> str:
    """Render tool schemas into a compact text block for the prompt."""
    lines = []
    for tool in tools:
        func = tool.get("function", {})
        name = func.get("name", "")
        desc = func.get("description", "").split("\n")[0]  # first line only
        params = func.get("parameters", {}).get("properties", {})
        required = func.get("parameters", {}).get("required", [])

        param_parts = []
        for pname, pinfo in params.items():
            ptype = pinfo.get("type", "string")
            req = " (required)" if pname in required else ""
            param_parts.append(f"{pname}: {ptype}{req}")

        params_str = ", ".join(param_parts) if param_parts else "none"
        lines.append(f"- {name}({params_str}): {desc}")

    return "\n".join(lines)


def _parse_tool_call(text: str) -> Optional[tuple[str, dict]]:
    """Parse a tool call from model text output.

    Tries multiple strategies:
    1. Direct JSON parse of entire response
    2. Extract JSON object from surrounding text
    3. Extract from markdown code block
    """
    text = text.strip()

    # Strategy 1: direct parse
    try:
        data = json.loads(text)
        if "tool_name" in data:
            return data["tool_name"], data.get("args", {})
    except json.JSONDecodeError:
        pass

    # Strategy 2: find first { ... } block
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
    if match:
        try:
            data = json.loads(match.group())
            if "tool_name" in data:
                return data["tool_name"], data.get("args", {})
        except json.JSONDecodeError:
            pass

    # Strategy 3: markdown code block
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if "tool_name" in data:
                return data["tool_name"], data.get("args", {})
        except json.JSONDecodeError:
            pass

    return None


class ClaudeCliModel(BaseModelBackend):
    """Model backend that calls `claude -p` for inference.

    Supports tool calling by injecting tool schemas into the system
    prompt and parsing structured JSON responses.
    """

    def __init__(
        self,
        model: str = "haiku",
        timeout: float = 30.0,
    ):
        super().__init__(
            model_type="claude-cli",
            model_config_dict={},
            api_key="cli",
            timeout=timeout,
        )
        self._cli_model = model
        self._cli_timeout = timeout

    def _messages_to_prompt(
        self,
        messages: List[OpenAIMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple[str, str]:
        """Extract system prompt and user content from OpenAI-format messages.

        If tools are provided, appends tool definitions to the system prompt.
        """
        system_parts = []
        user_parts = []

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
                prefix = "" if role == "user" else "[Assistant]: "
                user_parts.append(f"{prefix}{content}")

        system_prompt = "\n\n".join(system_parts) if system_parts else ""

        if tools:
            tool_defs = _render_tool_definitions(tools)
            tool_prompt = TOOL_SYSTEM_PROMPT.format(tool_definitions=tool_defs)
            system_prompt = f"{system_prompt}\n\n{tool_prompt}" if system_prompt else tool_prompt

        user_prompt = "\n\n".join(user_parts) if user_parts else ""
        return system_prompt, user_prompt

    def _call_cli(self, system_prompt: str, user_prompt: str) -> str:
        """Call claude CLI and return the result text."""
        cmd = [
            "claude", "-p",
            "--model", self._cli_model,
            "--output-format", "json",
            "--no-session-persistence",
            "--allowedTools", "",
        ]
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        _prompt_log.debug(
            "SYSTEM:\n%s\n\nUSER:\n%s",
            system_prompt,
            user_prompt,
        )

        result = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=self._cli_timeout,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            try:
                data = json.loads(stdout)
                if data.get("is_error"):
                    raise RuntimeError(f"claude CLI error: {data.get('result', stderr)}")
            except (json.JSONDecodeError, KeyError):
                pass
            raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {stderr or stdout}")

        data = json.loads(result.stdout)
        return data.get("result", "")

    async def _acall_cli(self, system_prompt: str, user_prompt: str) -> str:
        """Async version — runs CLI in a thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._call_cli, system_prompt, user_prompt)

    def _make_response(
        self,
        content: str,
        tool_call: Optional[tuple[str, dict]] = None,
    ) -> ChatCompletion:
        """Wrap response in OpenAI ChatCompletion format.

        If tool_call is provided, constructs a tool_calls response.
        Otherwise returns plain content.
        """
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
            id=f"claude-cli-{int(time.time())}",
            model=self._cli_model,
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
        system_prompt, user_prompt = self._messages_to_prompt(messages, tools)
        content = self._call_cli(system_prompt, user_prompt)

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
        system_prompt, user_prompt = self._messages_to_prompt(messages, tools)
        content = await self._acall_cli(system_prompt, user_prompt)

        tool_call = None
        if tools:
            tool_call = _parse_tool_call(content)

        return self._make_response(content, tool_call)

    @property
    def token_limit(self) -> int:
        return 200_000

    @property
    def token_counter(self):
        from camel.models.stub_model import StubTokenCounter
        return StubTokenCounter()

    def check_model_config(self):
        pass

    @property
    def stream(self):
        return False
