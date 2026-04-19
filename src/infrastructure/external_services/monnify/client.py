from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from src.config.settings import Settings

logger = logging.getLogger(__name__)


class MonnifyClient:
    def __init__(self) -> None:
        self.base_url = Settings.MONNIFY_BASE_URL.rstrip("/")
        self.api_key = Settings.MONNIFY_API_KEY or ""
        self.secret_key = Settings.MONNIFY_SECRET_KEY or ""
        self.contract_code = Settings.MONNIFY_CONTRACT_CODE or ""
        self._access_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    async def _get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._access_token and self._expires_at and now < self._expires_at:
            return self._access_token

        raw = f"{self.api_key}:{self.secret_key}".encode()
        basic = base64.b64encode(raw).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                headers={"Authorization": f"Basic {basic}"},
            )
            resp.raise_for_status()
            payload = resp.json().get("responseBody", {})

        self._access_token = payload["accessToken"]
        self._expires_at = now + timedelta(minutes=55)
        return self._access_token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        token = await self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.request(
                method=method,
                url=f"{self.base_url}{path}",
                headers=headers,
                **kwargs,
            )
            if resp.status_code == 401:
                self._access_token = None
                token = await self._get_token()
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.request(
                    method=method,
                    url=f"{self.base_url}{path}",
                    headers=headers,
                    **kwargs,
                )
            if resp.status_code >= 400:
                try:
                    err_detail = resp.json()
                except Exception:
                    err_detail = (resp.text or "")[:800]
                logger.warning(
                    "Monnify API error %s %s %s: %s",
                    resp.status_code,
                    method,
                    path,
                    err_detail,
                )
            resp.raise_for_status()
            return resp.json()

    async def create_reserved_account(
        self,
        *,
        account_reference: str,
        account_name: str,
        customer_email: str,
        customer_name: str,
        bvn: str | None = None,
        nin: str | None = None,
        get_all_available_banks: bool = True,
    ) -> dict:
        """Create a customer reserved account per Monnify v2 docs (BVN or NIN required)."""
        bvn_s = (bvn or "").strip()
        nin_s = (nin or "").strip()
        if not bvn_s and not nin_s:
            raise ValueError(
                "Monnify reserved account requires customer bvn or nin (see Customer Reserved Account API)."
            )

        preferred = [
            p.strip()
            for p in (Settings.MONNIFY_PREFERRED_BANKS or "50515").split(",")
            if p.strip()
        ]
        if not preferred:
            preferred = ["50515"]

        payload: dict[str, Any] = {
            "accountReference": account_reference,
            "accountName": account_name,
            "currencyCode": "NGN",
            "contractCode": self.contract_code,
            "customerEmail": customer_email,
            "customerName": customer_name,
            "getAllAvailableBanks": bool(get_all_available_banks),
            "preferredBanks": preferred,
        }
        if bvn_s:
            payload["bvn"] = bvn_s
        if nin_s:
            payload["nin"] = nin_s

        return await self._request("POST", "/api/v2/bank-transfer/reserved-accounts", json=payload)

    async def attach_bvn(self, *, account_reference: str, bvn: str) -> dict:
        return await self._request(
            "PUT",
            f"/api/v2/bank-transfer/reserved-accounts/update-payment-source-filter/{account_reference}",
            json={"bvn": bvn},
        )

    async def bvn_match(self, *, bvn: str, name: str, date_of_birth: str, mobile_no: str = "") -> dict:
        return await self._request(
            "POST",
            "/api/v1/kyc/bvn/match",
            json={
                "bvn": bvn,
                "name": name,
                "dateOfBirth": date_of_birth,
                "mobileNo": mobile_no,
            },
        )

    async def nin_lookup(self, *, nin: str, date_of_birth: str) -> dict:
        return await self._request(
            "POST",
            "/api/v1/kyc/nin",
            json={"nin": nin, "dateOfBirth": date_of_birth},
        )

    async def validate_account(self, *, account_number: str, bank_code: str) -> dict:
        return await self._request(
            "GET",
            f"/api/v1/disbursements/account/validate?accountNumber={account_number}&bankCode={bank_code}",
        )

    async def single_transfer(self, payload: dict) -> dict:
        return await self._request("POST", "/api/v1/disbursements/single", json=payload)

    async def batch_transfer(self, payload: dict) -> dict:
        return await self._request("POST", "/api/v1/disbursements/batch", json=payload)

    async def transfer_status(self, reference: str) -> dict:
        return await self._request(
            "GET", f"/api/v1/disbursements/single/summary?reference={reference}"
        )

    async def banks(self) -> dict:
        return await self._request("GET", "/api/v1/sdk/transactions/banks")

    def verify_signature(self, raw_body: bytes, signature: str) -> bool:
        computed = hmac.new(
            self.secret_key.encode(), raw_body, hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(computed, signature or "")
