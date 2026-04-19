import json
import logging
from decimal import Decimal
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessModel,
    BusinessVirtualAccountModel,
)
from src.infrastructure.database.repositories.wallet_repository import WalletRepository
from src.infrastructure.external_services.monnify.client import MonnifyClient
from src.services.ledger_writer import insert_ledger_entry
from src.services.wallet_limit_service import check_and_flag_overlimit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/monnify", tags=["monnify-webhooks"])


async def _replay_guard(raw_body: bytes, payment_reference: str) -> bool:
    redis_url = Settings.REDIS_URL
    if not redis_url:
        if Settings.is_production():
            return False
        return True
    digest = sha256(raw_body).hexdigest()
    key = f"monnify:webhook:seen:{payment_reference}:{digest}"
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        created = await redis.set(key, "1", ex=60 * 60 * 24, nx=True)
        return bool(created)
    finally:
        await redis.aclose()


@router.post("", status_code=status.HTTP_200_OK)
async def monnify_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    raw_body = await request.body()
    signature = request.headers.get("monnify-signature", "")
    client = MonnifyClient()
    if not client.verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(raw_body.decode("utf-8"))
    event_type = payload.get("eventType")
    event_data = payload.get("eventData", {})
    account_reference = (event_data.get("product") or {}).get("reference")
    if not account_reference:
        return {"ok": True}

    business = (
        await session.execute(
            select(BusinessModel)
            .join(
                BusinessVirtualAccountModel,
                BusinessVirtualAccountModel.business_id == BusinessModel.id,
            )
            .where(BusinessVirtualAccountModel.account_reference == account_reference)
        )
    ).scalar_one_or_none()
    if business is None:
        logger.warning("Monnify webhook received for unknown account reference %s", account_reference)
        return {"ok": True}

    if event_type == "SUCCESSFUL_TRANSACTION":
        amount = Decimal(str(event_data.get("amountPaid", 0)))
        payment_reference = event_data.get("paymentReference") or f"monnify:{account_reference}:{amount}"

        # Redis guard: prevents duplicate concurrent deliveries hitting the DB
        # simultaneously. If the guard fires but DB commit later fails, we clear
        # the key so Monnify's retry can proceed (see except block below).
        redis_key: str | None = None
        is_fresh = await _replay_guard(raw_body, payment_reference)
        if not is_fresh:
            # Already processed — idempotent ACK
            return {"ok": True}

        redis_url = Settings.REDIS_URL
        digest = sha256(raw_body).hexdigest()
        redis_key = f"monnify:webhook:seen:{payment_reference}:{digest}"

        repo = WalletRepository(session)
        try:
            tx, created = await repo.credit(
                business_id=business.id,
                amount=amount,
                reference=f"monnify_topup_{payment_reference}",
                description="Monnify reserved account top-up",
            )
            if created:
                await insert_ledger_entry(
                    session,
                    entry_type="wallet_topup",
                    gross_amount=amount,
                    net_amount=amount,
                    direction="credit",
                    status="completed",
                    originator_type="monnify",
                    beneficiary_type="business_wallet",
                    business_id=business.id,
                    narration="Monnify reserved account top-up",
                    provider_reference=payment_reference,
                    prefix="TOP",
                )
                await check_and_flag_overlimit(session, business, tx.balance_after)
                await session.commit()
        except Exception as exc:
            logger.error(
                "Monnify webhook DB commit failed for %s — clearing Redis guard so Monnify can retry. Error: %s",
                payment_reference,
                exc,
            )
            # Clear the Redis guard so Monnify's next delivery attempt is not
            # blocked. Without this, the guard would suppress the retry and the
            # payment would be silently lost.
            if redis_key and redis_url:
                try:
                    _r = Redis.from_url(redis_url, decode_responses=True)
                    await _r.delete(redis_key)
                    await _r.aclose()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail="Internal error processing payment — please retry")

    return {"ok": True}
