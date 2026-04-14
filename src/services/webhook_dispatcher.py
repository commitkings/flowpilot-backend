"""
Webhook dispatcher — sends signed HTTP POST requests to registered webhook URLs
when business events occur.

Usage (from any route handler or background task):
    import asyncio
    from src.services.webhook_dispatcher import dispatch_event

    # Fire and forget — does not block the caller
    asyncio.create_task(dispatch_event(business_id, "run.completed", {
        "run_id": "...",
        "objective": "...",
        "status": "completed",
    }))

Supported events:
    run.completed           — a payout run finished successfully
    run.failed              — a payout run failed
    payout.succeeded        — an individual payout candidate succeeded
    payout.failed           — an individual payout candidate failed
    approval.requested      — a run moved to awaiting_approval
    approval.completed      — a run was approved or rejected and executed
    candidate.flagged       — a candidate was flagged with a high risk score
    webhook.test            — sent once when a webhook is first registered

Signing:
    Each payload is signed with HMAC-SHA256 using the webhook's signing_secret.
    The signature is sent in the X-FlowPilot-Signature header as:
        sha256=<hex_digest>

    Recipients should verify:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(f"sha256={expected}", received_signature)

Retry / failure handling:
    - Each failed delivery increments failure_count on the webhook record.
    - After MAX_FAILURES consecutive failures the webhook is auto-disabled
      (is_active set to False) to prevent hammering unreachable endpoints.
    - Successful delivery resets failure_count to 0.
"""

import asyncio
import hashlib
import hmac as hmac_lib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select, update

logger = logging.getLogger(__name__)

MAX_FAILURES = 5        # auto-disable after this many consecutive failures
DELIVERY_TIMEOUT = 10   # seconds to wait for the endpoint to respond


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

async def dispatch_event(
    business_id: uuid.UUID,
    event_name: str,
    payload: dict[str, Any],
) -> None:
    """
    Deliver `event_name` to all active webhooks for `business_id` that
    subscribed to it.

    This function creates its own DB session so it can be safely called as an
    asyncio background task after the originating request has finished.
    """
    try:
        from src.infrastructure.database.connection import get_session_factory
        from src.infrastructure.database.flowpilot_models import WebhookModel

        async with get_session_factory()() as session:
            result = await session.execute(
                select(WebhookModel).where(
                    WebhookModel.business_id == business_id,
                    WebhookModel.is_active.is_(True),
                )
            )
            all_webhooks = result.scalars().all()

        # Filter to those subscribed to this specific event
        subscribed = [
            wh for wh in all_webhooks
            if event_name in (wh.events or [])
        ]

        if not subscribed:
            return

        logger.info(
            f"[Webhook] Dispatching {event_name} to {len(subscribed)} endpoint(s)"
        )

        for webhook in subscribed:
            await _deliver_one(webhook, event_name, payload)

    except Exception as exc:
        logger.exception(f"[Webhook] dispatch_event failed for {event_name}: {exc}")


async def send_test_ping(webhook_url: str, signing_secret: str) -> bool:
    """
    Send a webhook.test event to verify the endpoint is reachable.
    Returns True if the endpoint responded with a 2xx status, False otherwise.
    Used synchronously during webhook creation.
    """
    body_dict = {
        "event": "webhook.test",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "delivery_id": str(uuid.uuid4()),
        "data": {
            "message": (
                "This is a test event from FlowPilot to verify your webhook endpoint. "
                "No action is required."
            )
        },
    }
    body_bytes = json.dumps(body_dict, default=str).encode()
    signature = _sign(signing_secret, body_bytes)

    headers = {
        "Content-Type": "application/json",
        "X-FlowPilot-Event": "webhook.test",
        "X-FlowPilot-Signature": f"sha256={signature}",
        "X-FlowPilot-Delivery": str(uuid.uuid4()),
        "User-Agent": "FlowPilot-Webhooks/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT) as client:
            resp = await client.post(webhook_url, content=body_bytes, headers=headers)
            success = 200 <= resp.status_code < 300
            logger.info(
                f"[Webhook] Test ping to {webhook_url} → {resp.status_code} "
                f"({'ok' if success else 'failed'})"
            )
            return success
    except Exception as exc:
        logger.warning(f"[Webhook] Test ping to {webhook_url} failed: {exc}")
        return False


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _sign(secret: str, body: bytes) -> str:
    """Compute HMAC-SHA256 hex digest for the given body using the webhook secret."""
    return hmac_lib.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _deliver_one(webhook: Any, event_name: str, payload: dict[str, Any]) -> None:
    """Deliver a single event to one webhook endpoint and update its DB record."""
    body_dict = {
        "event": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "delivery_id": str(uuid.uuid4()),
        "data": payload,
    }
    body_bytes = json.dumps(body_dict, default=str).encode()
    signing_secret = webhook.signing_secret or ""
    signature = _sign(signing_secret, body_bytes)

    headers = {
        "Content-Type": "application/json",
        "X-FlowPilot-Event": event_name,
        "X-FlowPilot-Signature": f"sha256={signature}",
        "X-FlowPilot-Delivery": str(uuid.uuid4()),
        "User-Agent": "FlowPilot-Webhooks/1.0",
    }

    success = False
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT) as client:
            resp = await client.post(webhook.url, content=body_bytes, headers=headers)
            success = 200 <= resp.status_code < 300
            logger.info(
                f"[Webhook] {event_name} → {webhook.url} {resp.status_code} "
                f"({'ok' if success else 'non-2xx'})"
            )
    except Exception as exc:
        logger.warning(f"[Webhook] Delivery to {webhook.url} failed: {exc}")

    await _update_webhook_record(webhook.id, success, webhook.failure_count)


async def _update_webhook_record(
    webhook_id: uuid.UUID,
    success: bool,
    current_failure_count: int,
) -> None:
    """Persist delivery outcome: update last_triggered_at, failure_count, is_active."""
    try:
        from src.infrastructure.database.connection import get_session_factory
        from src.infrastructure.database.flowpilot_models import WebhookModel

        now = datetime.now(timezone.utc)

        if success:
            values: dict = {
                "last_triggered_at": now,
                "failure_count": 0,
            }
        else:
            new_count = current_failure_count + 1
            values = {"failure_count": new_count}
            if new_count >= MAX_FAILURES:
                values["is_active"] = False
                logger.warning(
                    f"[Webhook] {webhook_id} auto-disabled after {new_count} failures"
                )

        async with get_session_factory()() as session:
            await session.execute(
                update(WebhookModel)
                .where(WebhookModel.id == webhook_id)
                .values(**values)
            )
            await session.commit()

    except Exception as exc:
        logger.exception(f"[Webhook] Failed to update record for {webhook_id}: {exc}")
