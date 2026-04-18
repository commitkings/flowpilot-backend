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
    IndividualKycSubmissionModel,
    KycLimitTrackerModel,
    KycSubmissionModel,
    UserModel,
)
from src.config.kyc_limits import KYC_LIMITS, SUPPORT_EMAIL, get_limits
from src.infrastructure.database.repositories.notification_repository import (
    NotificationRepository,
)
from src.infrastructure.storage import s3_client
from src.infrastructure.storage.s3_client import validate_document, get_presigned_url
from src.services import email_service
from src.config.settings import Settings
from src.services.payment_service import PaymentService

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
    """Background task: wait 30 seconds then mark KYC as verified."""
    await asyncio.sleep(30)
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

                biz_result = await session.execute(
                    select(BusinessModel).where(BusinessModel.id == bid)
                )
                biz = biz_result.scalar_one_or_none()
                va_updates: dict = {
                    "kyc_status": "verified",
                    "kyc_level": 1,  # Business full submission = Level 1
                    "updated_at": now,
                }
                await session.execute(
                    update(BusinessModel)
                    .where(BusinessModel.id == bid)
                    .values(**va_updates)
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

        _biz_l1 = get_limits("business", 1)
        _biz_max = get_limits("business", max(KYC_LIMITS.get("business", {}).keys(), default=1))
        def _fmtb(n) -> str:
            return f"\u20a6{float(n):,.0f}"
        from src.services.email_service import check_notification_pref as _cnp_k
        if _cnp_k(owner, "kyc_updates"):
            await email_service.send_kyc_verified_email(
                to=owner_email,
                display_name=owner_name,
                business_name=business_name,
                monthly_limit=_fmtb(_biz_l1["monthly"]) if _biz_l1 else "₦1,500,000",
                single_limit=_fmtb(_biz_l1["single"]) if _biz_l1 else "₦300,000",
                wallet_limit=_fmtb(_biz_l1["wallet"]) if _biz_l1 else "₦3,000,000",
                max_monthly_limit=_fmtb(_biz_max["monthly"]) if _biz_max else "₦50,000,000",
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

    # Block individual accounts from the business KYC route
    if getattr(biz, "account_type", "business") == "individual":
        raise HTTPException(
            status_code=400,
            detail="Individual accounts must use the /kyc/individual/level1-3 endpoints.",
        )

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

    _sub_l1 = get_limits("business", 1)
    def _fmts(n) -> str:
        return f"\u20a6{float(n):,.0f}"
    from src.services.email_service import check_notification_pref as _cnp_ks
    if _cnp_ks(user, "kyc_updates"):
        asyncio.create_task(
            email_service.send_kyc_submitted_email(
                to=user.email,
                display_name=user.display_name or user.email,
                business_name=biz.business_name,
                submitted_docs=submitted_docs,
                monthly_limit=_fmts(_sub_l1["monthly"]) if _sub_l1 else "₦1,500,000",
                single_limit=_fmts(_sub_l1["single"]) if _sub_l1 else "₦300,000",
                wallet_limit=_fmts(_sub_l1["wallet"]) if _sub_l1 else "₦3,000,000",
            )
        )

    return {
        "status": "pending",
        "message": "KYC documents submitted and queued for verification.",
        "submitted_docs": submitted_docs,
    }


@router.get("/status")
async def get_kyc_status(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Return the current KYC status for the user's business.

    Includes account_type, kyc_level, limit info, and submission details.
    """
    biz, _ = await _get_business_and_owner(current_user, session)

    account_type = getattr(biz, "account_type", "business") or "business"
    kyc_level = getattr(biz, "kyc_level", 0) or 0

    # Build limit info
    from datetime import date as _date
    limits = get_limits(account_type, kyc_level)
    max_level = max(KYC_LIMITS.get(account_type, {}).keys(), default=0)

    # Fetch monthly usage tracker
    _today = _date.today()
    _month_start = _today.replace(day=1)
    _tracker_result = await session.execute(
        select(KycLimitTrackerModel).where(KycLimitTrackerModel.business_id == biz.id)
    )
    _tracker = _tracker_result.scalar_one_or_none()
    monthly_payout_used = 0.0
    if _tracker:
        if _tracker.month_start < _month_start:
            # New month — treat as zero (will reset on next approval)
            monthly_payout_used = 0.0
        else:
            monthly_payout_used = float(_tracker.monthly_payout_used)

    limit_info = {
        "account_type": account_type,
        "kyc_level": kyc_level,
        "monthly_limit": float(limits["monthly"]) if limits else 0,
        "single_limit": float(limits["single"]) if limits else 0,
        "wallet_limit": float(limits["wallet"]) if limits else 0,
        "at_max_level": kyc_level >= max_level,
        "support_email": SUPPORT_EMAIL,
        "monthly_payout_used": monthly_payout_used,
    }

    if account_type == "individual":
        ind_result = await session.execute(
            select(IndividualKycSubmissionModel).where(
                IndividualKycSubmissionModel.business_id == biz.id
            )
        )
        ind = ind_result.scalar_one_or_none()

        def _mask_id(value: Optional[str]) -> Optional[str]:
            """Mask BVN/NIN — show first 3 and last 2 digits, hide the rest."""
            if not value or len(value) < 6:
                return value
            return value[:3] + "·" * (len(value) - 5) + value[-2:]

        return {
            "kyc_status": biz.kyc_status,
            "limit_info": limit_info,
            "individual_submission": {
                "level_1_type": ind.level_1_type if ind else None,
                # Masked — never expose raw BVN/NIN
                "level_1_masked_value": _mask_id(ind.level_1_value) if ind else None,
                "level_1_status": ind.level_1_status if ind else "not_submitted",
                "level_1_submitted_at": ind.level_1_submitted_at.isoformat() if ind and ind.level_1_submitted_at else None,
                "level_1_verified_at": ind.level_1_verified_at.isoformat() if ind and ind.level_1_verified_at else None,
                # Address is non-sensitive, show as-is
                "level_2_address": ind.level_2_address if ind else None,
                "level_2_status": ind.level_2_status if ind else "not_submitted",
                # Only indicate whether a document was uploaded — never return the URL
                "level_2_document_uploaded": bool(ind.level_2_document_key) if ind else False,
                "level_2_submitted_at": ind.level_2_submitted_at.isoformat() if ind and ind.level_2_submitted_at else None,
                "level_2_verified_at": ind.level_2_verified_at.isoformat() if ind and ind.level_2_verified_at else None,
                "level_3_status": ind.level_3_status if ind else "not_submitted",
                # Only indicate what was uploaded — never return document URLs or selfie
                "level_3_document_uploaded": bool(ind.level_3_document_key) if ind else False,
                "level_3_selfie_uploaded": bool(ind.level_3_selfie_key) if ind else False,
                "level_3_submitted_at": ind.level_3_submitted_at.isoformat() if ind and ind.level_3_submitted_at else None,
                "level_3_verified_at": ind.level_3_verified_at.isoformat() if ind and ind.level_3_verified_at else None,
            } if ind else None,
            "submission": None,
        }

    # Business account — return existing business KYC submission
    kyc_result = await session.execute(
        select(KycSubmissionModel).where(KycSubmissionModel.business_id == biz.id)
    )
    kyc = kyc_result.scalar_one_or_none()

    if kyc is None:
        return {
            "kyc_status": biz.kyc_status,
            "limit_info": limit_info,
            "submission": None,
        }

    return {
        "kyc_status": biz.kyc_status,
        "limit_info": limit_info,
        "submission": {
            "status": kyc.status,
            "business_type": kyc.business_type,
            "registration_number": kyc.registration_number,
            "tin_number": kyc.tin_number,
            "director_name": kyc.director_name,
            "trustee_name": kyc.trustee_name,
            "scuml_number": kyc.scuml_number,
            "partner_names": kyc.partner_names,
            "authorized_officer_name": kyc.authorized_officer_name,
            "submitted_at": kyc.submitted_at.isoformat() if kyc.submitted_at else None,
            "verified_at": kyc.verified_at.isoformat() if kyc.verified_at else None,
            "has_cac_certificate": bool(kyc.cac_certificate_key),
            "has_tin_document": bool(kyc.tin_document_key),
            "has_director_id": bool(kyc.director_id_key),
            "has_proof_of_address": bool(kyc.proof_of_address_key),
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


# ── Individual KYC routes ─────────────────────────────────────────────────────

async def _auto_verify_individual_kyc(
    business_id: str, level: int, owner_email: str, owner_name: str
) -> None:
    """Background task: wait 30 seconds then mark individual KYC level as verified."""
    await asyncio.sleep(30)
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                bid = uuid.UUID(business_id)
                now = datetime.now(timezone.utc)

                level_field = f"level_{level}_status"
                verified_field = f"level_{level}_verified_at"

                await session.execute(
                    update(IndividualKycSubmissionModel)
                    .where(IndividualKycSubmissionModel.business_id == bid)
                    .values({level_field: "verified", verified_field: now, "updated_at": now})
                )

                # Update business kyc_level and kyc_status
                biz_result = await session.execute(
                    select(BusinessModel).where(BusinessModel.id == bid)
                )
                biz = biz_result.scalar_one_or_none()
                if biz:
                    new_level = max(getattr(biz, "kyc_level", 0) or 0, level)
                    biz_updates: dict = {
                        "kyc_level": new_level,
                        "kyc_status": "verified",
                        "updated_at": now,
                    }
                    await session.execute(
                        update(BusinessModel).where(BusinessModel.id == bid).values(**biz_updates)
                    )

                # In-app notification
                owner_result = await session.execute(
                    select(UserModel).where(UserModel.email == owner_email)
                )
                owner = owner_result.scalar_one_or_none()
                if owner and biz:
                    notif_repo = NotificationRepository(session)
                    await notif_repo.create(
                        user_id=owner.id,
                        business_id=bid,
                        title=f"KYC Level {level} Verified",
                        message=f"Your Level {level} verification is complete. Your monthly limit has been updated.",
                        type="success",
                        resource_type="kyc",
                        resource_id=business_id,
                    )

        # Send approval email with new limits
        _level_names = {1: "Identity", 2: "Address", 3: "Government ID"}
        _limits = get_limits("individual", level)
        _max_level = max(KYC_LIMITS.get("individual", {}).keys(), default=0)
        def _fmt(n) -> str:
            return f"\u20a6{float(n):,.0f}"
        from src.services.email_service import check_notification_pref as _cnp_kiv
        if _cnp_kiv(owner, "kyc_updates"):
            await email_service.send_individual_kyc_verified_email(
                to=owner_email,
                display_name=owner_name,
                level=level,
                level_name=_level_names.get(level, f"Level {level}"),
                monthly_limit=_fmt(_limits["monthly"]) if _limits else "N/A",
                single_limit=_fmt(_limits["single"]) if _limits else "N/A",
                wallet_limit=_fmt(_limits["wallet"]) if _limits else "N/A",
                at_max_level=(level >= _max_level),
                support_email=SUPPORT_EMAIL,
            )
        logger.info("Individual KYC Level %d auto-verified for business %s", level, business_id)
    except Exception as exc:
        logger.error("Individual KYC auto-verification failed (level %d, business %s): %s", level, business_id, exc)


@router.post("/individual/level1")
async def submit_individual_kyc_level1(
    id_type: str = Form(..., description="'nin' or 'bvn'"),
    id_value: str = Form(..., min_length=10, max_length=20),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Submit NIN or BVN for Level 1 individual KYC."""
    biz, user = await _get_business_and_owner(current_user, session)

    if getattr(biz, "account_type", "business") != "individual":
        raise HTTPException(status_code=400, detail="This endpoint is for individual accounts only.")

    if id_type not in ("nin", "bvn"):
        raise HTTPException(status_code=422, detail="id_type must be 'nin' or 'bvn'")

    now = datetime.now(timezone.utc)

    ind_result = await session.execute(
        select(IndividualKycSubmissionModel).where(
            IndividualKycSubmissionModel.business_id == biz.id
        )
    )
    ind = ind_result.scalar_one_or_none()

    if ind is None:
        ind = IndividualKycSubmissionModel(
            business_id=biz.id,
            level_1_type=id_type,
            level_1_value=id_value,
            level_1_status="pending",
            level_1_submitted_at=now,
        )
        session.add(ind)
    else:
        ind.level_1_type = id_type
        ind.level_1_value = id_value
        ind.level_1_status = "pending"
        ind.level_1_submitted_at = now
        ind.updated_at = now

    biz.kyc_status = "pending"
    biz.updated_at = now

    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=user.id,
        business_id=biz.id,
        title="Identity Verification Submitted",
        message=f"Your {id_type.upper()} has been submitted. We'll verify it shortly.",
        type="info",
        resource_type="kyc",
        resource_id=str(biz.id),
    )
    await session.commit()

    from src.services.email_service import check_notification_pref as _cnp_kis1
    if _cnp_kis1(user, "kyc_updates"):
        asyncio.create_task(
            email_service.send_individual_kyc_submitted_email(
                to=user.email,
                display_name=user.display_name or user.email,
                level=1,
                level_name=f"Identity ({id_type.upper()})",
            )
        )
    )
    # Real verification with Monnify
    payment_service = PaymentService()
    passed = False
    try:
        if id_type == "bvn":
            match_status = await payment_service.bvn_match(
                bvn=id_value,
                name=user.display_name or user.email,
                date_of_birth=(user.date_of_birth.isoformat() if user.date_of_birth else "1990-01-01"),
            )
            passed = match_status in ("EXACT_MATCH", "PARTIAL_MATCH")
        else:
            nin = await payment_service.nin_lookup(
                nin=id_value,
                date_of_birth=(user.date_of_birth.isoformat() if user.date_of_birth else "1990-01-01"),
            )
            passed = bool(nin)
    except Exception as exc:
        logger.warning("Monnify KYC verification failed for business=%s: %s", biz.id, exc)
        passed = False

    now2 = datetime.now(timezone.utc)
    ind.level_1_status = "verified" if passed else "rejected"
    ind.level_1_verified_at = now2 if passed else None
    if passed:
        biz.kyc_level = max(getattr(biz, "kyc_level", 0) or 0, 1)
        biz.kyc_status = "verified"
        if id_type == "bvn" and biz.virtual_account_reference:
            try:
                await payment_service.attach_bvn(
                    account_reference=biz.virtual_account_reference or f"fp-{biz.id}",
                    bvn=id_value,
                )
            except Exception as exc:
                logger.warning("Failed attaching BVN to reserved account for %s: %s", biz.id, exc)
    else:
        biz.kyc_status = "rejected"
    biz.updated_at = now2
    await session.commit()

    return {
        "status": ind.level_1_status,
        "message": f"{id_type.upper()} verification {ind.level_1_status}.",
    }


@router.post("/individual/level2")
async def submit_individual_kyc_level2(
    address: str = Form(..., min_length=5, max_length=500),
    proof_of_address: Optional[UploadFile] = File(None),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Submit address + proof of address document for Level 2 individual KYC.

    Requires Level 1 to already be verified.
    """
    biz, user = await _get_business_and_owner(current_user, session)

    if getattr(biz, "account_type", "business") != "individual":
        raise HTTPException(status_code=400, detail="This endpoint is for individual accounts only.")

    if (getattr(biz, "kyc_level", 0) or 0) < 1:
        raise HTTPException(
            status_code=400,
            detail="Complete Level 1 verification before proceeding to Level 2.",
        )

    now = datetime.now(timezone.utc)

    poa_key: Optional[str] = None
    if proof_of_address:
        content = await proof_of_address.read()
        error = validate_document(content, max_bytes=10 * 1024 * 1024)
        if error:
            raise HTTPException(status_code=400, detail=f"{proof_of_address.filename}: {error}")
        poa_key = await s3_client.upload_file(
            content, proof_of_address.filename or "proof_of_address", folder="kyc/individual/poa"
        )

    ind_result = await session.execute(
        select(IndividualKycSubmissionModel).where(
            IndividualKycSubmissionModel.business_id == biz.id
        )
    )
    ind = ind_result.scalar_one_or_none()
    if ind is None:
        raise HTTPException(status_code=400, detail="Level 1 submission not found.")

    ind.level_2_address = address
    if poa_key:
        ind.level_2_document_key = poa_key
    ind.level_2_status = "pending"
    ind.level_2_submitted_at = now
    ind.updated_at = now

    biz.kyc_status = "pending"
    biz.updated_at = now

    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=user.id,
        business_id=biz.id,
        title="Address Verification Submitted",
        message="Your address and proof of address have been submitted for review.",
        type="info",
        resource_type="kyc",
        resource_id=str(biz.id),
    )
    await session.commit()

    from src.services.email_service import check_notification_pref as _cnp_kis2
    if _cnp_kis2(user, "kyc_updates"):
        asyncio.create_task(
            email_service.send_individual_kyc_submitted_email(
                to=user.email,
                display_name=user.display_name or user.email,
                level=2,
                level_name="Address",
            )
        )
    asyncio.create_task(
        _auto_verify_individual_kyc(
            business_id=str(biz.id),
            level=2,
            owner_email=user.email,
            owner_name=user.display_name or user.email,
        )
    )

    return {"status": "pending", "message": "Address verification submitted."}


@router.post("/individual/level3")
async def submit_individual_kyc_level3(
    government_id: UploadFile = File(...),
    liveness_selfie: UploadFile = File(...),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Submit government-issued photo ID + liveness selfie for Level 3 individual KYC.

    Requires Level 2 to already be verified. Accepts NIN card, passport, or driver's licence.
    The liveness selfie must be a photo taken live via the device camera.
    """
    biz, user = await _get_business_and_owner(current_user, session)

    if getattr(biz, "account_type", "business") != "individual":
        raise HTTPException(status_code=400, detail="This endpoint is for individual accounts only.")

    if (getattr(biz, "kyc_level", 0) or 0) < 2:
        raise HTTPException(
            status_code=400,
            detail="Complete Level 2 verification before proceeding to Level 3.",
        )

    now = datetime.now(timezone.utc)

    # Upload government ID
    content = await government_id.read()
    error = validate_document(content, max_bytes=10 * 1024 * 1024)
    if error:
        raise HTTPException(status_code=400, detail=f"{government_id.filename}: {error}")
    gov_id_key = await s3_client.upload_file(
        content, government_id.filename or "government_id", folder="kyc/individual/gov_id"
    )

    # Upload liveness selfie
    selfie_content = await liveness_selfie.read()
    selfie_error = validate_document(selfie_content, max_bytes=10 * 1024 * 1024)
    if selfie_error:
        raise HTTPException(status_code=400, detail=f"Selfie: {selfie_error}")
    selfie_key = await s3_client.upload_file(
        selfie_content, liveness_selfie.filename or "liveness_selfie.jpg", folder="kyc/individual/selfie"
    )

    ind_result = await session.execute(
        select(IndividualKycSubmissionModel).where(
            IndividualKycSubmissionModel.business_id == biz.id
        )
    )
    ind = ind_result.scalar_one_or_none()
    if ind is None:
        raise HTTPException(status_code=400, detail="Level 1 submission not found.")

    ind.level_3_document_key = gov_id_key
    ind.level_3_selfie_key = selfie_key
    ind.level_3_status = "pending"
    ind.level_3_submitted_at = now
    ind.updated_at = now

    biz.kyc_status = "pending"
    biz.updated_at = now

    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=user.id,
        business_id=biz.id,
        title="Government ID Submitted",
        message="Your government-issued ID has been submitted for Level 3 verification.",
        type="info",
        resource_type="kyc",
        resource_id=str(biz.id),
    )
    await session.commit()

    from src.services.email_service import check_notification_pref as _cnp_kis3
    if _cnp_kis3(user, "kyc_updates"):
        asyncio.create_task(
            email_service.send_individual_kyc_submitted_email(
                to=user.email,
                display_name=user.display_name or user.email,
                level=3,
                level_name="Government ID",
            )
        )
    asyncio.create_task(
        _auto_verify_individual_kyc(
            business_id=str(biz.id),
            level=3,
            owner_email=user.email,
            owner_name=user.display_name or user.email,
        )
    )

    return {"status": "pending", "message": "Government ID submitted for Level 3 verification."}
