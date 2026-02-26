import uuid
import logging
from typing import Optional

import httpx

from src.config.settings import Settings

logger = logging.getLogger(__name__)


class InterswitchAuth:

    def __init__(self) -> None:
        self._base_url = Settings.INTERSWITCH_BASE_URL.rstrip("/")
        self._client_id = Settings.INTERSWITCH_CLIENT_ID
        self._client_secret = Settings.INTERSWITCH_CLIENT_SECRET

    def get_headers(self, access_token: Optional[str] = None) -> dict[str, str]:
        token = access_token or Settings.get_interswitch_access_token()
        if not token:
            raise ValueError("Interswitch access token not configured")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
        }

    @property
    def base_url(self) -> str:
        return self._base_url

    def get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self.get_headers(),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def get_resilient_client(self):
        """Return a ResilientClient with retry/backoff for transient errors."""
        from src.infrastructure.external_services.interswitch.http_client import (
            ResilientClient,
        )

        return ResilientClient(self.get_client())
