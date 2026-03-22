"""
Interswitch Bank Account Verification — Marketplace API.

Resolves account_number + bank_code → account holder name via the
API Marketplace routing service.

Docs: Bank Account Verification API on developer.interswitchgroup.com
Base URL:  INTERSWITCH_BAV_BASE_URL
           (default: https://api-marketplace-routing.k8.isw.la)

Two endpoints:
  GET  .../verify/identity/account-number/bank-list   → supported banks
  POST .../verify/identity/account-number/resolve      → name resolution
"""

import logging
from typing import Optional

import httpx

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)

_PREFIX = "/marketplace-routing/api/v1/verify/identity/account-number"
_RESOLVE_PATH = f"{_PREFIX}/resolve"
_BANK_LIST_PATH = f"{_PREFIX}/bank-list"


class BankAccountVerificationClient:
    """Verify bank account details via Interswitch Bank Account Verification API."""

    def __init__(self) -> None:
        # Auth against QA Passport with scope=profile (required for Marketplace routing)
        self._auth = InterswitchAuth(
            base_url=Settings.INTERSWITCH_BASE_URL,
            scope="profile",
        )
        self._base_url = getattr(
            Settings,
            "INTERSWITCH_BAV_BASE_URL",
            "https://api-marketplace-routing.k8.isw.la",
        ).rstrip("/")

    async def resolve_account(
        self,
        account_number: str,
        bank_code: str,
    ) -> dict:
        """Resolve an account number + bank code to account holder details.

        Args:
            account_number: 10-digit NUBAN account number.
            bank_code: Bank institution code (e.g. '058' for GTBank).

        Returns:
            dict with fields:
                lookupStatus  (str)  — "SUCCESS" or "FAILED"
                canCredit     (bool) — True if account was found
                accountName   (str)  — name on the account
                accountNumber (str)
                bankName      (str)  — full bank name
                institutionCode (str)
                raw_response  (dict) — full API response
        """
        url = f"{self._base_url}{_RESOLVE_PATH}"
        payload = {
            "accountNumber": account_number,
            "bankCode": bank_code,
        }

        headers = await self._auth.get_headers()

        logger.info(
            f"BAV resolve: bank={bank_code}, account={account_number}, url={url}"
        )

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.post(url, json=payload, headers=headers)
                data = response.json()

                success = data.get("success", False)
                bank_details = data.get("data", {}).get("bankDetails", {})
                account_name = bank_details.get("accountName", "")

                logger.info(
                    f"BAV result: status={response.status_code}, success={success}, "
                    f"name={account_name!r}"
                )

                return {
                    "lookupStatus": "SUCCESS" if success and account_name else "FAILED",
                    "canCredit": bool(success and account_name),
                    "accountName": account_name,
                    "accountNumber": bank_details.get("accountNumber", account_number),
                    "bankName": bank_details.get("bankName", ""),
                    "institutionCode": bank_code,
                    "raw_response": data,
                }

        except Exception as e:
            logger.error(f"BAV resolve failed: {type(e).__name__}: {e}")
            return {
                "lookupStatus": "FAILED",
                "canCredit": False,
                "accountName": "",
                "accountNumber": account_number,
                "bankName": "",
                "institutionCode": bank_code,
                "raw_response": {"error": str(e)},
            }

    async def get_bank_list(self) -> list[dict]:
        """Fetch the list of supported banks and their codes.

        Returns:
            list of dicts with: id, name, slug, code, longCode, active, country, currency, type
        """
        url = f"{self._base_url}{_BANK_LIST_PATH}"
        headers = await self._auth.get_headers()

        logger.info(f"BAV bank-list: url={url}")

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
            response = await client.get(url, headers=headers)
            data = response.json()

            banks = data.get("data", [])
            logger.info(f"BAV bank-list: {len(banks)} banks returned")
            return banks
