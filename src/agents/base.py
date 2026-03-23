import logging
from typing import Optional

from groq import AsyncGroq

from src.config.settings import Settings

logger = logging.getLogger(__name__)


class BaseAgent:

    def __init__(self, name: str) -> None:
        self.name = name
        self._client: Optional[AsyncGroq] = None

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
        model = model or Settings.GROQ_LLM_MODEL
        logger.info(f"[{self.name}] LLM call: model={model}")
        response = await self.llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content or ""
        logger.info(f"[{self.name}] LLM response: {len(content)} chars")
        return content

    async def llm_json_call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
    ) -> str:
        model = model or Settings.GROQ_LLM_MODEL
        response = await self.llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or "{}"
