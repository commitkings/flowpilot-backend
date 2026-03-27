import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from groq import AsyncGroq

from src.agents.tools import ToolRegistry, ToolCall, ToolResult, parse_tool_calls_from_response
from src.config.settings import Settings
from src.utilities.logging_config import (
    get_logger,
    log_agent_event,
    log_llm_call,
    log_tool_call,
)

logger = get_logger(__name__)


@dataclass
class LLMCallResult:
    content: str
    model: str = ""
    prompt_summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0


_AGENT_TYPE_RE = re.compile(r"Agent$", re.IGNORECASE)


def _normalize_agent_type(name: str) -> str:
    return _AGENT_TYPE_RE.sub("", name).lower()


MAX_REACT_ITERATIONS = 15
REACT_SYSTEM_SUFFIX = """

## Tool Use Protocol

You have access to tools. To accomplish your task, you MUST use tools to gather real data — never fabricate data or assume values.

When you have gathered enough information via tools, produce your final answer.

Always think step-by-step:
1. THINK: What do I need to find out next?
2. ACT: Call the appropriate tool.
3. OBSERVE: Read the tool result.
4. REPEAT or CONCLUDE: Either call another tool or produce the final answer.

If a tool call fails, reason about why and try an alternative approach.
"""


class BaseAgent:

    def __init__(self, name: str) -> None:
        self.name = name
        self._agent_type_key = _normalize_agent_type(name)
        self._client: Optional[AsyncGroq] = None
        self._event_publisher = None
        self._current_step_id: Optional[UUID] = None
        self._reasoning_entries: list[dict] = []
        self.registry = ToolRegistry()

    def set_publisher(self, publisher, step_id: Optional[UUID] = None) -> None:
        self._event_publisher = publisher
        self._current_step_id = step_id
        self._reasoning_entries = []

    @property
    def llm_client(self) -> AsyncGroq:
        if self._client is None:
            api_key = Settings.GROQ_API_KEY or Settings().groq_api_key
            if not api_key:
                raise ValueError("GROQ_API_KEY not configured")
            self._client = AsyncGroq(api_key=api_key)
        return self._client

    async def reason_and_act(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        max_iterations: int = MAX_REACT_ITERATIONS,
    ) -> str:
        model = model or Settings.GROQ_LLM_MODEL
        full_system = system_prompt + REACT_SYSTEM_SUFFIX

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": user_prompt},
        ]

        tools_schema = self.registry.to_llm_tools() if self.registry.tools else None
        iteration = 0

        log_agent_event(self.name, "react_start", {
            "model": model,
            "max_iterations": max_iterations,
            "tools_count": len(self.registry.tools) if self.registry.tools else 0,
            "prompt_preview": user_prompt[:200],
        })

        while iteration < max_iterations:
            iteration += 1
            logger.info(f"[{self.name}] ReAct iteration {iteration}/{max_iterations}")

            kwargs: dict[str, Any] = dict(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if tools_schema:
                kwargs["tools"] = tools_schema
                kwargs["tool_choice"] = "auto"

            t0 = time.monotonic()
            response = await self.llm_client.chat.completions.create(**kwargs)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            msg = response.choices[0].message
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0

            log_llm_call(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=elapsed_ms,
                agent=self.name,
                iteration=iteration,
            )

            self._record_reasoning(
                thinking=msg.content[:500] if msg.content else f"[tool_calls: {len(msg.tool_calls or [])}]",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=elapsed_ms,
                iteration=iteration,
            )

            tool_calls = parse_tool_calls_from_response(msg)
            if not tool_calls:
                final_content = msg.content or ""
                log_agent_event(self.name, "react_complete", {
                    "iterations": iteration,
                    "result_length": len(final_content),
                })
                logger.info(f"[{self.name}] ReAct concluded after {iteration} iteration(s)")
                return final_content

            messages.append(self._assistant_message_from_response(msg))

            for tc in tool_calls:
                await self._emit_tool_call_event(tc)
                t_start = time.monotonic()
                result = await self.registry.execute(tc)
                tool_duration = int((time.monotonic() - t_start) * 1000)
                
                log_tool_call(
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                    success=result.success,
                    duration_ms=tool_duration,
                    result_preview=str(result.data)[:200] if result.data else result.error,
                    agent=self.name,
                )
                
                await self._emit_tool_result_event(tc, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.call_id,
                    "content": result.to_message_content(),
                })

        log_agent_event(self.name, "react_max_iterations", {
            "iterations": max_iterations,
        })
        logger.warning(f"[{self.name}] ReAct hit max iterations ({max_iterations})")
        last_content = ""
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                last_content = m["content"]
                break
        return last_content or '{"error": "Agent reached maximum reasoning iterations without a final answer"}'

    async def reason_and_act_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_iterations: int = MAX_REACT_ITERATIONS,
    ) -> str:
        json_instruction = (
            "\n\nIMPORTANT: When you are ready to give your final answer (after using tools), "
            "respond with ONLY a valid JSON object. No markdown, no explanation — just the JSON."
        )
        raw = await self.reason_and_act(
            system_prompt=system_prompt,
            user_prompt=user_prompt + json_instruction,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
        )
        return self._extract_json(raw)

    async def llm_call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        result = await self._llm_call_with_reasoning(
            system_prompt, user_prompt, model=model,
            temperature=temperature, max_tokens=max_tokens,
        )
        return result.content

    async def llm_json_call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
    ) -> str:
        result = await self._llm_call_with_reasoning(
            system_prompt, user_prompt, model=model,
            temperature=temperature, max_tokens=4096,
            json_mode=True,
        )
        return result.content

    async def _llm_call_with_reasoning(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMCallResult:
        model = model or Settings.GROQ_LLM_MODEL
        logger.info(f"[{self.name}] LLM call: model={model}")

        kwargs: dict = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        t0 = time.monotonic()
        response = await self.llm_client.chat.completions.create(**kwargs)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        content = response.choices[0].message.content or ("" if not json_mode else "{}")
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0

        prompt_summary = user_prompt[:200] + ("..." if len(user_prompt) > 200 else "")

        result = LLMCallResult(
            content=content,
            model=model,
            prompt_summary=prompt_summary,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=elapsed_ms,
        )

        logger.info(
            f"[{self.name}] LLM response: {len(content)} chars, "
            f"{prompt_tokens}+{completion_tokens} tokens, {elapsed_ms}ms"
        )

        self._record_reasoning(
            thinking=content[:500],
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=elapsed_ms,
        )

        return result

    async def emit_progress(self, message: str, metadata: dict | None = None) -> None:
        if self._event_publisher:
            try:
                await self._event_publisher.step_progress(
                    self._agent_type_key, message, detail=metadata,
                    step_id=self._current_step_id,
                )
            except Exception:
                logger.debug(f"[{self.name}] Failed to emit progress event", exc_info=True)

    def _record_reasoning(
        self,
        thinking: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
        iteration: int | None = None,
    ) -> None:
        entry = {
            "agent_type": self._agent_type_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thinking": thinking,
            "token_usage": {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "duration_ms": duration_ms,
            },
        }
        if iteration is not None:
            entry["react_iteration"] = iteration
        self._reasoning_entries.append(entry)

        if self._event_publisher:
            try:
                import asyncio
                asyncio.ensure_future(self._event_publisher.reasoning(
                    agent_type=self._agent_type_key,
                    thinking=thinking,
                    prompt_summary=f"ReAct iteration {iteration}" if iteration else None,
                    token_usage=entry["token_usage"],
                    step_id=self._current_step_id,
                ))
            except Exception:
                logger.debug(f"[{self.name}] Failed to emit reasoning event", exc_info=True)

    async def _emit_tool_call_event(self, call: ToolCall) -> None:
        if self._event_publisher:
            try:
                await self._event_publisher.step_progress(
                    self._agent_type_key,
                    f"Calling tool: {call.tool_name}",
                    detail={"tool": call.tool_name, "arguments": call.arguments},
                    step_id=self._current_step_id,
                )
            except Exception:
                logger.debug(f"[{self.name}] Failed to emit tool call event", exc_info=True)

    async def _emit_tool_result_event(self, call: ToolCall, result: ToolResult) -> None:
        if self._event_publisher:
            try:
                detail: dict[str, Any] = {
                    "tool": call.tool_name,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                }
                if not result.success:
                    detail["error"] = result.error
                msg = f"Tool {call.tool_name}: {'OK' if result.success else 'FAILED'} ({result.duration_ms}ms)"
                await self._event_publisher.step_progress(
                    self._agent_type_key, msg, detail=detail,
                    step_id=self._current_step_id,
                )
            except Exception:
                logger.debug(f"[{self.name}] Failed to emit tool result event", exc_info=True)

    @staticmethod
    def _assistant_message_from_response(msg) -> dict[str, Any]:
        m: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            m["content"] = msg.content
        if msg.tool_calls:
            m["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        return m

    @staticmethod
    def _extract_json(raw: str) -> str:
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return stripped

        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", stripped, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()

        brace_match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if brace_match:
            return brace_match.group(0)

        bracket_match = re.search(r"\[.*\]", stripped, re.DOTALL)
        if bracket_match:
            return bracket_match.group(0)

        return stripped
