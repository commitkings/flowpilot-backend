import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from groq import AsyncGroq

from src.config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class LLMCallResult:
    """Structured result from an LLM call including reasoning metadata."""
    content: str
    model: str = ""
    prompt_summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0


# Maps "PlannerAgent" -> "planner", "ReconciliationAgent" -> "reconciliation", etc.
_AGENT_TYPE_RE = re.compile(r"Agent$", re.IGNORECASE)


def _normalize_agent_type(name: str) -> str:
    return _AGENT_TYPE_RE.sub("", name).lower()


class BaseAgent:

    def __init__(self, name: str) -> None:
        self.name = name
        self._agent_type_key = _normalize_agent_type(name)
        self._client: Optional[AsyncGroq] = None
        self._event_publisher = None  # Set by orchestrator before agent.run()
        self._current_step_id: Optional[UUID] = None
        self._reasoning_entries: list[dict] = []

    def set_publisher(self, publisher, step_id: Optional[UUID] = None) -> None:
        """Attach an EventPublisher and current step_id for event correlation."""
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
        """Core LLM call that captures full reasoning metadata."""
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

        # Build a short summary of the prompt (first 200 chars of user prompt)
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

        reasoning_entry = {
            "agent_type": self._agent_type_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thinking": content[:500],
            "token_usage": {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "duration_ms": elapsed_ms,
            },
        }
        self._reasoning_entries.append(reasoning_entry)

        if self._event_publisher:
            try:
                await self._event_publisher.reasoning(
                    agent_type=self._agent_type_key,
                    thinking=content[:500],
                    prompt_summary=prompt_summary,
                    token_usage=reasoning_entry["token_usage"],
                    step_id=self._current_step_id,
                )
            except Exception:
                logger.debug(f"[{self.name}] Failed to emit reasoning event", exc_info=True)

        return result

    async def emit_progress(self, message: str, metadata: dict | None = None) -> None:
        """Emit a progress event (for non-LLM operations like API calls)."""
        if self._event_publisher:
            try:
                await self._event_publisher.step_progress(
                    self._agent_type_key, message, detail=metadata,
                    step_id=self._current_step_id,
                )
            except Exception:
                logger.debug(f"[{self.name}] Failed to emit progress event", exc_info=True)
