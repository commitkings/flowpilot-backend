"""
Interswitch authentication — OAuth 2.0 Client Credentials flow.

Acquires a bearer token via POST /passport/oauth/token, caches it in-memory
with TTL, and auto-refreshes when expired.  Falls back to a static
INTERSWITCH_ACCESS_TOKEN env var when OAuth2 credentials are absent.
"""

import base64
import logging
import time
import uuid
from typing import Optional

import httpx

from src.config.settings import Settings

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN_SECONDS = 60


class InterswitchAuth:

    # Per-base-URL token cache: {base_url: (token, expires_at)}
    _token_cache: dict[str, tuple[str, float]] = {}

    def __init__(self, base_url: Optional[str] = None, scope: Optional[str] = None) -> None:
        self._base_url = (base_url or Settings.INTERSWITCH_BASE_URL).rstrip("/")
        self._client_id = Settings.INTERSWITCH_CLIENT_ID
        self._client_secret = Settings.INTERSWITCH_CLIENT_SECRET
        self._scope = scope

    async def get_access_token(self) -> str:
        """Return a valid bearer token, refreshing via OAuth2 if needed."""
        cache_key = f"{self._base_url}|{self._scope or ''}"
        cached = self._token_cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]

        if self._client_id and self._client_secret:
            return await self._acquire_oauth2_token()

        static_token = Settings.get_interswitch_access_token()
        if static_token:
            logger.info("Using static INTERSWITCH_ACCESS_TOKEN (no OAuth2 credentials)")
            return static_token

        raise ValueError(
            "Interswitch credentials not configured. "
            "Set INTERSWITCH_CLIENT_ID + INTERSWITCH_CLIENT_SECRET for OAuth2, "
            "or set INTERSWITCH_ACCESS_TOKEN as a static fallback."
        )

    async def _acquire_oauth2_token(self) -> str:
        """Acquire token via OAuth2 Client Credentials grant."""
        token_url = f"{self._base_url}/passport/oauth/token"
        credentials = f"{self._client_id}:{self._client_secret}"
        basic_auth = base64.b64encode(credentials.encode()).decode()

        logger.info("Acquiring Interswitch OAuth2 token via client_credentials grant")

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            form_data: dict[str, str] = {"grant_type": "client_credentials"}
            if self._scope:
                form_data["scope"] = self._scope
            response = await client.post(
                token_url,
                data=form_data,
                headers={
                    "Authorization": f"Basic {basic_auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()
            data = response.json()

        access_token = data.get("access_token")
        if not access_token:
            raise ValueError(f"OAuth2 response missing access_token: {data}")

        expires_in = int(data.get("expires_in", 3600))
        expires_at = time.time() + expires_in - _TOKEN_REFRESH_MARGIN_SECONDS
        cache_key = f"{self._base_url}|{self._scope or ''}"
        InterswitchAuth._token_cache[cache_key] = (access_token, expires_at)

        logger.info(f"OAuth2 token acquired, expires in {expires_in}s")
        return access_token

    async def get_headers(self, access_token: Optional[str] = None) -> dict[str, str]:
        """Build request headers with a valid bearer token."""
        token = access_token or await self.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
        }

    @property
    def base_url(self) -> str:
        return self._base_url

    async def get_client(self) -> httpx.AsyncClient:
        """Create an httpx.AsyncClient with current auth headers."""
        headers = await self.get_headers()
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def get_resilient_client(self):
        """Return a ResilientClient with retry/backoff for transient errors."""
        from src.infrastructure.external_services.interswitch.http_client import (
            ResilientClient,
        )
        client = await self.get_client()
        return ResilientClient(client)

    @classmethod
    def clear_token_cache(cls, base_url: Optional[str] = None) -> None:
        """Clear cached token(s). Pass base_url to clear a specific cache entry."""
        if base_url:
            cls._token_cache.pop(base_url.rstrip("/"), None)
        else:
            cls._token_cache.clear()
