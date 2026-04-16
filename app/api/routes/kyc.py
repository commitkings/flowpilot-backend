"""KYC (Know Your Business) verification routes.

Flow:
  POST /kyc/submit  — upload documents to MinIO, mark business kyc_status=pending,
                      send submitted email + in-app notification,
                      schedule 1-minute auto-verification task.
  GET  /kyc/status  — return current KYC status + all text fields + presigned document URLs.

Business types and their documents:
  limited_company  : cac_certificate (req), tin_document (opt), director_id (req), proof_of_address (opt)
  ngo              : cac_certificate (req), trustee_id (req), scuml_letter (opt), proof_of_address (opt)
  sole_proprietorship: cac_certificate (req), tin_document (opt), director_id (req), proof_of_address (opt)
  partnership      : cac_certificate (req), tin_document (req), partner_id (req), proof_of_address (opt)
  mda              : mda_letter (req), authorized_officer_id (req), proof_of_address (opt)
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
from src.infrastructure.storage.s3_client import validate_document, get_presigned_url
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


def _presigned(key: Optional[str]) -> Optional[str]:
    """Generate a 1-hour presigned URL for the given MinIO object key, or None."""
    if not key:
        return None
    return get_presigned_url(key)


async def _auto_verify_kyc(business_id: str, owner_email: str, owner_name: str, business_name: str) -> None:
    """Background task: wait 60 seconds then mark KYC as verified."""
    await asyncio.sleep(60)
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                bid = uuid.UUID(business_id)
                now = datetime.now(timezone.utc)

                await session.execute(
                    update(KycSubmissionModel)
                    .where(KycSubmissionModel.business_id == bid)
                    .values(status="verified", verified_at=now, updated_at=now)
                )
                await session.execute(
                    update(BusinessModel)
                    .where(BusinessModel.id == bid)
                    .values(kyc_status="verified", updated_at=now)
                )

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
    # ── Business type ──────────────────────────────────────────
    business_type: Optional[str] = Form(None),
    registration_number: Optional[str] = Form(None),
    tin_number: Optional[str] = Form(None),

    # ── LLC / Sole Prop — director fields ──────────────────────
    director_name: Optional[str] = Form(None),
    director_bvn: Optional[str] = Form(None),

    # ── NGO / Non-profit ──────────────────────────────────────
    trustee_name: Optional[str] = Form(None),
    trustee_bvn: Optional[str] = Form(None),
    scuml_number: Optional[str] = Form(None),

    # ── Partnership ───────────────────────────────────────────
    partner_names: Optional[str] = Form(None),   # JSON string: ["Name 1","Name 2"]

    # ── Government / MDA ─────────────────────────────────────
    authorized_officer_name: Optional[str] = Form(None),
    authorized_officer_bvn: Optional[str] = Form(None),

    # ── Shared documents ──────────────────────────────────────
    cac_certificate: Optional[UploadFile] = File(None),
    tin_document: Optional[UploadFile] = File(None),
    proof_of_address: Optional[UploadFile] = File(None),

    # ── Type-specific documents ───────────────────────────────
    director_id: Optional[UploadFile] = File(None),          # LLC, Sole Prop
    trustee_id: Optional[UploadFile] = File(None),            # NGO
    partner_id: Optional[UploadFile] = File(None),            # Partnership rep ID
    scuml_letter: Optional[UploadFile] = File(None),          # NGO (optional)
    mda_letter: Optional[UploadFile] = File(None),            # Govt / MDA
    authorized_officer_id: Optional[UploadFile] = File(None), # Govt / MDA

    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Submit KYC documents and initiate the verification process.

    All uploads go to MinIO. Fields vary by business_type.
    Schedules auto-verification after 60 seconds.
    """
    biz, user = await _get_business_and_owner(current_user, session)

    # Validate business type
    valid_types = {"limited_company", "ngo", "sole_proprietorship", "partnership", "mda"}
    if business_type and business_type not in valid_types:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid business_type. Must be one of: {', '.join(sorted(valid_types))}",
        )

    # At least some data must be provided
    text_fields = [director_name, trustee_name, authorized_officer_name, partner_names]
    file_fields = [cac_certificate, tin_document, proof_of_address, director_id,
                   trustee_id, partner_id, scuml_letter, mda_letter, authorized_officer_id]
    if not any(text_fields) and not any(file_fields) and not business_type:
        raise HTTPException(
            status_code=422,
            detail="At least one document or field must be provided",
        )

    # ── Upload documents concurrently ─────────────────────────

    async def _upload(upload: Optional[UploadFile], folder: str) -> Optional[str]:
        if upload is None:
            return None
        content = await upload.read()
        error = validate_document(content, max_bytes=10 * 1024 * 1024)
        if error:
            raise HTTPException(
                status_code=400,
                detail=f"{upload.filename}: {error}",
            )
        return await s3_client.upload_file(content, upload.filename or "document", folder=folder)

    (
        cac_key, tin_key, poa_key, dir_id_key,
        trustee_id_key, partner_id_key, scuml_letter_key,
        mda_letter_key, auth_officer_id_key,
    ) = await asyncio.gather(
        _upload(cac_certificate,       "kyc/cac"),
        _upload(tin_document,          "kyc/tin"),
        _upload(proof_of_address,      "kyc/proof_of_address"),
        _upload(director_id,           "kyc/director_id"),
        _upload(trustee_id,            "kyc/trustee_id"),
        _upload(partner_id,            "kyc/partner_id"),
        _upload(scuml_letter,          "kyc/scuml"),
        _upload(mda_letter,            "kyc/mda_letter"),
        _upload(authorized_officer_id, "kyc/authorized_officer_id"),
    )

    now = datetime.now(timezone.utc)

    # ── Upsert KycSubmission ──────────────────────────────────

    kyc_result = await session.execute(
        select(KycSubmissionModel).where(KycSubmissionModel.business_id == biz.id)
    )
    kyc = kyc_result.scalar_one_or_none()

    if kyc is None:
        kyc = KycSubmissionModel(
            business_id=biz.id,
            status="pending",
            business_type=business_type,
            registration_number=registration_number,
            tin_number=tin_number,
            # Shared docs
            cac_certificate_key=cac_key,
            tin_document_key=tin_key,
            proof_of_address_key=poa_key,
            # LLC / Sole Prop
            director_name=director_name,
            director_bvn=director_bvn,
            director_id_key=dir_id_key,
            # NGO
            trustee_name=trustee_name,
            trustee_bvn=trustee_bvn,
            trustee_id_key=trustee_id_key,
            scuml_number=scuml_number,
            scuml_letter_key=scuml_letter_key,
            # Partnership
            partner_names=partner_names,
            partner_id_key=partner_id_key,
            # MDA
            mda_letter_key=mda_letter_key,
            authorized_officer_name=authorized_officer_name,
            authorized_officer_bvn=authorized_officer_bvn,
            authorized_officer_id_key=auth_officer_id_key,
            submitted_at=now,
        )
        session.add(kyc)
    else:
        kyc.status = "pending"
        kyc.submitted_at = now
        kyc.updated_at = now
        if business_type:
            kyc.business_type = business_type
        if registration_number:
            kyc.registration_number = registration_number
        if tin_number:
            kyc.tin_number = tin_number
        # Docs — only overwrite if a new file was uploaded
        if cac_key:
            kyc.cac_certificate_key = cac_key
        if tin_key:
            kyc.tin_document_key = tin_key
        if poa_key:
            kyc.proof_of_address_key = poa_key
        if dir_id_key:
            kyc.director_id_key = dir_id_key
        if director_name:
            kyc.director_name = director_name
        if director_bvn:
            kyc.director_bvn = director_bvn
        if trustee_name:
            kyc.trustee_name = trustee_name
        if trustee_bvn:
            kyc.trustee_bvn = trustee_bvn
        if trustee_id_key:
            kyc.trustee_id_key = trustee_id_key
        if scuml_number:
            kyc.scuml_number = scuml_number
        if scuml_letter_key:
            kyc.scuml_letter_key = scuml_letter_key
        if partner_names:
            kyc.partner_names = partner_names
        if partner_id_key:
            kyc.partner_id_key = partner_id_key
        if mda_letter_key:
            kyc.mda_letter_key = mda_letter_key
        if authorized_officer_name:
            kyc.authorized_officer_name = authorized_officer_name
        if authorized_officer_bvn:
            kyc.authorized_officer_bvn = authorized_officer_bvn
        if auth_officer_id_key:
            kyc.authorized_officer_id_key = auth_officer_id_key

    # Update business kyc_status + business_type
    biz.kyc_status = "pending"
    if business_type:
        biz.business_type = business_type
    biz.updated_at = now

    # In-app notification
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

    # Build list of submitted items for confirmation email
    submitted_docs = []
    if cac_key:
        submitted_docs.append("CAC Certificate / Business Registration")
    if tin_key:
        submitted_docs.append("TIN Document")
    if poa_key:
        submitted_docs.append("Proof of Business Address")
    if dir_id_key:
        submitted_docs.append("Director / Owner Government-Issued ID")
    if trustee_id_key:
        submitted_docs.append("Trustee Government-Issued ID")
    if partner_id_key:
        submitted_docs.append("Partner Representative ID")
    if scuml_letter_key:
        submitted_docs.append("SCUML Registration Letter")
    if mda_letter_key:
        submitted_docs.append("MDA Authorization Letter")
    if auth_officer_id_key:
        submitted_docs.append("Authorized Officer Government-Issued ID")
    if director_name:
        submitted_docs.append(f"Director Details ({director_name})")
    if trustee_name:
        submitted_docs.append(f"Trustee Details ({trustee_name})")
    if authorized_officer_name:
        submitted_docs.append(f"Authorized Officer Details ({authorized_officer_name})")

    asyncio.create_task(
        email_service.send_kyc_submitted_email(
            to=user.email,
            display_name=user.display_name or user.email,
            business_name=biz.business_name,
            submitted_docs=submitted_docs,
        )
    )

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
    """Return the current KYC status for the user's business.

    When verified, includes all text fields and presigned document URLs (1-hour expiry).
    """
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
            "business_type": kyc.business_type,
            "registration_number": kyc.registration_number,
            "tin_number": kyc.tin_number,
            # Director / owner
            "director_name": kyc.director_name,
            # NGO
            "trustee_name": kyc.trustee_name,
            "scuml_number": kyc.scuml_number,
            # Partnership
            "partner_names": kyc.partner_names,
            # MDA
            "authorized_officer_name": kyc.authorized_officer_name,
            # Timestamps
            "submitted_at": kyc.submitted_at.isoformat() if kyc.submitted_at else None,
            "verified_at": kyc.verified_at.isoformat() if kyc.verified_at else None,
            # Document presence flags (backwards compat)
            "has_cac_certificate": bool(kyc.cac_certificate_key),
            "has_tin_document": bool(kyc.tin_document_key),
            "has_director_id": bool(kyc.director_id_key),
            "has_proof_of_address": bool(kyc.proof_of_address_key),
            # Presigned document URLs — 1 hour expiry; None if not uploaded
            "cac_certificate_url": _presigned(kyc.cac_certificate_key),
            "tin_document_url": _presigned(kyc.tin_document_key),
            "proof_of_address_url": _presigned(kyc.proof_of_address_key),
            "director_id_url": _presigned(kyc.director_id_key),
            "trustee_id_url": _presigned(kyc.trustee_id_key),
            "partner_id_url": _presigned(kyc.partner_id_key),
            "scuml_letter_url": _presigned(kyc.scuml_letter_key),
            "mda_letter_url": _presigned(kyc.mda_letter_key),
            "authorized_officer_id_url": _presigned(kyc.authorized_officer_id_key),
        },
    }
