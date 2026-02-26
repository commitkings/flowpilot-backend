"""
Resilient HTTP client for Interswitch API calls.

Wraps httpx.AsyncClient with:
  - Exponential backoff + jitter on 429, 502, 503, 504
  - Configurable retries (default 3), base wait 1s, max wait 30s
  - Per-retry logging with status code and wait time
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
    RetryCallState,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

MAX_RETRIES = 3
BASE_WAIT_SECONDS = 1
MAX_WAIT_SECONDS = 30


class RetryableHTTPError(Exception):
    """Raised when an HTTP response has a retryable status code."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(
            f"HTTP {response.status_code} from {response.request.method} {response.request.url}"
        )


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableHTTPError)


def _log_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    status = getattr(exc, "response", None)
    code = status.status_code if status is not None else "?"
    wait = retry_state.next_action.sleep if retry_state.next_action else 0
    logger.warning(
        "Interswitch retry attempt %d — HTTP %s — waiting %.1fs",
        retry_state.attempt_number,
        code,
        wait,
    )


def _raise_if_retryable(response: httpx.Response) -> None:
    if response.status_code in _RETRYABLE_STATUS_CODES:
        raise RetryableHTTPError(response)
    response.raise_for_status()


class ResilientClient:
    """Drop-in replacement for httpx.AsyncClient with retry semantics."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(MAX_RETRIES + 1),
        wait=wait_exponential_jitter(initial=BASE_WAIT_SECONDS, max=MAX_WAIT_SECONDS),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.get(url, **kwargs)
        _raise_if_retryable(response)
        return response

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(MAX_RETRIES + 1),
        wait=wait_exponential_jitter(initial=BASE_WAIT_SECONDS, max=MAX_WAIT_SECONDS),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.post(url, **kwargs)
        _raise_if_retryable(response)
        return response

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ResilientClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
