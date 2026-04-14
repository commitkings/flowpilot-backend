"""KYC (Know Your Business) verification routes.

Flow:
  POST /kyc/submit  — upload documents to MinIO, mark business kyc_status=pending,
                      send submitted email + in-app notification,
                      schedule 1-minute auto-verification task.
  GET  /kyc/status  — return current KYC status + document availability.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session, get_session_factory
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    BusinessModel,
    KycSubmissionModel,
    UserModel,
)
from src.infrastructure.database.repositories.notification_repository import (
    NotificationRepository,
)
from src.infrastructure.storage import s3_client
from src.infrastructure.storage.s3_client import validate_document
from src.services import email_service
from src.config.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kyc", tags=["kyc"])


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_business_and_owner(
    current_user, session: AsyncSession
) -> tuple[BusinessModel, UserModel]:
    """Return (business, user) for the current user's first membership."""
    from src.infrastructure.database.repositories.user_repository import UserRepository

    repo = UserRepository(session)
    memberships = await repo.get_memberships(current_user.id)
    if not memberships:
        raise HTTPException(status_code=404, detail="No organisation found for user")

    business_id = memberships[0].business_id

    biz_result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == business_id)
    )
    biz = biz_result.scalar_one_or_none()
    if biz is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    user_result = await session.execute(
        select(UserModel).where(UserModel.id == current_user.id)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return biz, user


async def _auto_verify_kyc(business_id: str, owner_email: str, owner_name: str, business_name: str) -> None:
    """Background task: wait 60 seconds then mark KYC as verified."""
    await asyncio.sleep(60)
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                # Update kyc_submission
                bid = uuid.UUID(business_id)
                now = datetime.now(timezone.utc)

                await session.execute(
                    update(KycSubmissionModel)
                    .where(KycSubmissionModel.business_id == bid)
                    .values(status="verified", verified_at=now, updated_at=now)
                )
                # Update business.kyc_status
                await session.execute(
                    update(BusinessModel)
                    .where(BusinessModel.id == bid)
                    .values(kyc_status="verified", updated_at=now)
                )

                # Notify the owner in-app
                owner_result = await session.execute(
                    select(UserModel).where(UserModel.email == owner_email)
                )
                owner = owner_result.scalar_one_or_none()
                if owner:
                    notif_repo = NotificationRepository(session)
                    await notif_repo.create(
                        user_id=owner.id,
                        business_id=bid,
                        title="Business Verified",
                        message=f"{business_name} has been verified. You can now create payout runs.",
                        type="success",
                        resource_type="business",
                        resource_id=business_id,
                    )

        # Send verification email (outside transaction)
        await email_service.send_kyc_verified_email(
            to=owner_email,
            display_name=owner_name,
            business_name=business_name,
        )
        logger.info("KYC auto-verified for business %s", business_id)

    except Exception as exc:
        logger.error("KYC auto-verification failed for business %s: %s", business_id, exc)


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/submit")
async def submit_kyc(
    # Optional file uploads
    cac_certificate: Optional[UploadFile] = File(None),
    tin_document: Optional[UploadFile] = File(None),
    director_id: Optional[UploadFile] = File(None),
    proof_of_address: Optional[UploadFile] = File(None),
    # Director details
    director_name: Optional[str] = Form(None),
    director_bvn: Optional[str] = Form(None),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Submit KYC documents and initiate the verification process.

    Uploads documents to MinIO, marks the business as pending,
    sends a confirmation email + in-app notification, then schedules
    auto-verification in 60 seconds.
    """
    biz, user = await _get_business_and_owner(current_user, session)

    # At least one document must be provided
    files_provided = [f for f in [cac_certificate, tin_document, director_id, proof_of_address] if f]
    if not files_provided and not director_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one document or director details must be provided",
        )

    # Upload documents to MinIO
    async def _upload(upload: Optional[UploadFile], folder: str) -> Optional[str]:
        if upload is None:
            return None
        content = await upload.read()
        # Magic-byte validation: ensure it's actually a PDF or image, not malware
        error = validate_document(content, max_bytes=10 * 1024 * 1024)
        if error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{upload.filename}: {error}",
            )
        return await s3_client.upload_file(content, upload.filename or "document", folder=folder)

    cac_key = await _upload(cac_certificate, "kyc/cac")
    tin_key = await _upload(tin_document, "kyc/tin")
    dir_key = await _upload(director_id, "kyc/director_id")
    poa_key = await _upload(proof_of_address, "kyc/proof_of_address")

    now = datetime.now(timezone.utc)

    # Upsert KycSubmission row
    kyc_result = await session.execute(
        select(KycSubmissionModel).where(KycSubmissionModel.business_id == biz.id)
    )
    kyc = kyc_result.scalar_one_or_none()

    if kyc is None:
        kyc = KycSubmissionModel(
            business_id=biz.id,
            status="pending",
            cac_certificate_key=cac_key,
            tin_document_key=tin_key,
            director_id_key=dir_key,
            proof_of_address_key=poa_key,
            director_name=director_name,
            director_bvn=director_bvn,
            submitted_at=now,
        )
        session.add(kyc)
    else:
        kyc.status = "pending"
        kyc.submitted_at = now
        kyc.updated_at = now
        if cac_key:
            kyc.cac_certificate_key = cac_key
        if tin_key:
            kyc.tin_document_key = tin_key
        if dir_key:
            kyc.director_id_key = dir_key
        if poa_key:
            kyc.proof_of_address_key = poa_key
        if director_name:
            kyc.director_name = director_name
        if director_bvn:
            kyc.director_bvn = director_bvn

    # Update business kyc_status
    biz.kyc_status = "pending"
    biz.updated_at = now

    # In-app notification for submitted
    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=user.id,
        business_id=biz.id,
        title="KYC Documents Submitted",
        message="We've received your verification documents and will review them within 10 minutes.",
        type="info",
        resource_type="kyc",
        resource_id=str(biz.id),
    )

    await session.commit()

    # Build list of submitted doc names for the email
    submitted_docs = []
    if cac_key:
        submitted_docs.append("CAC Certificate of Incorporation")
    if tin_key:
        submitted_docs.append("Tax Identification Number (TIN) Document")
    if dir_key:
        submitted_docs.append("Director's Government-Issued ID")
    if poa_key:
        submitted_docs.append("Proof of Business Address")
    if director_name:
        submitted_docs.append(f"Director Details ({director_name})")

    # Fire-and-forget: send submitted email
    asyncio.create_task(
        email_service.send_kyc_submitted_email(
            to=user.email,
            display_name=user.display_name or user.email,
            business_name=biz.business_name,
            submitted_docs=submitted_docs,
        )
    )

    # Schedule auto-verification in 60 seconds
    asyncio.create_task(
        _auto_verify_kyc(
            business_id=str(biz.id),
            owner_email=user.email,
            owner_name=user.display_name or user.email,
            business_name=biz.business_name,
        )
    )

    return {
        "status": "pending",
        "message": "KYC documents submitted. You'll be notified once the review is complete.",
        "submitted_docs": submitted_docs,
    }


@router.get("/status")
async def get_kyc_status(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Return the current KYC status for the user's business."""
    biz, _ = await _get_business_and_owner(current_user, session)

    kyc_result = await session.execute(
        select(KycSubmissionModel).where(KycSubmissionModel.business_id == biz.id)
    )
    kyc = kyc_result.scalar_one_or_none()

    if kyc is None:
        return {
            "kyc_status": biz.kyc_status,
            "submission": None,
        }

    return {
        "kyc_status": biz.kyc_status,
        "submission": {
            "status": kyc.status,
            "director_name": kyc.director_name,
            "submitted_at": kyc.submitted_at.isoformat() if kyc.submitted_at else None,
            "verified_at": kyc.verified_at.isoformat() if kyc.verified_at else None,
            "has_cac_certificate": bool(kyc.cac_certificate_key),
            "has_tin_document": bool(kyc.tin_document_key),
            "has_director_id": bool(kyc.director_id_key),
            "has_proof_of_address": bool(kyc.proof_of_address_key),
        },
    }
