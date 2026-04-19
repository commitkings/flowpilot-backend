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
from sqlalchemy.orm import selectinload

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session, get_session_factory
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    BusinessModel,
    BusinessProfileModel,
    BusinessVirtualAccountModel,
    IndividualKycSubmissionModel,
    KycDocumentModel,
    KycLimitTrackerModel,
    KycSubmissionModel,
    KycUpgradeRequestModel,
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
from src.services.user_audit_service import log_user_audit_event

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


async def _upsert_kyc_document(
    session: AsyncSession,
    *,
    submission_id: uuid.UUID,
    business_id: uuid.UUID,
    doc_type: str,
    storage_key: Optional[str],
) -> None:
    if not storage_key:
        return
    r = await session.execute(
        select(KycDocumentModel).where(
            KycDocumentModel.submission_id == submission_id,
            KycDocumentModel.document_type == doc_type,
        ).limit(1)
    )
    row = r.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row:
        row.storage_key = storage_key
        row.uploaded_at = now
    else:
        session.add(
            KycDocumentModel(
                submission_id=submission_id,
                business_id=business_id,
                document_type=doc_type,
                storage_key=storage_key,
                is_current=True,
            )
        )


def _kyc_doc_key(kyc: KycSubmissionModel, doc_type: str) -> Optional[str]:
    for d in kyc.documents or []:
        if d.document_type == doc_type and d.is_current:
            return d.storage_key
    return None


def _digits_id(raw: str | None, length: int = 11) -> str | None:
    if not raw:
        return None
    d = "".join(c for c in raw if c.isdigit())
    if len(d) < 10:
        return None
    return d[:length]


def _pick_business_kyc_bvn(kyc: KycSubmissionModel) -> str | None:
    """BVN Monnify can bind for a verified business submission (by business type)."""
    bt = (kyc.business_type or "").lower()
    if bt == "ngo":
        order = (kyc.trustee_bvn, kyc.director_bvn)
    elif bt == "mda":
        order = (kyc.authorized_officer_bvn, kyc.director_bvn)
    else:
        order = (kyc.director_bvn, kyc.trustee_bvn, kyc.authorized_officer_bvn)
    for raw in order:
        b = _digits_id(raw, 11)
        if b:
            return b
    return None


async def _resolve_monnify_identity(
    session: AsyncSession, business_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """Return (bvn, nin) for Monnify reserved-account creation — at least one is required."""
    ind_r = await session.execute(
        select(IndividualKycSubmissionModel).where(
            IndividualKycSubmissionModel.business_id == business_id
        )
    )
    ind = ind_r.scalar_one_or_none()
    if ind and ind.level_1_status == "verified" and ind.level_1_type in ("bvn", "nin") and ind.level_1_value:
        digits = _digits_id(ind.level_1_value, 11)
        if not digits:
            return None, None
        if ind.level_1_type == "bvn":
            return digits, None
        return None, digits

    kyc_r = await session.execute(
        select(KycSubmissionModel).where(KycSubmissionModel.business_id == business_id)
    )
    kyc = kyc_r.scalar_one_or_none()
    if kyc and kyc.status == "verified":
        bvn = _pick_business_kyc_bvn(kyc)
        if bvn:
            return bvn, None
    return None, None


async def _ensure_reserved_virtual_account(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    owner_display_name: Optional[str],
    owner_email: str,
    notify: bool = True,
) -> None:
    """Provision Monnify reserved account once KYC is verified (idempotent)."""
    await session.flush()

    existing = await session.execute(
        select(BusinessVirtualAccountModel).where(
            BusinessVirtualAccountModel.business_id == business_id,
            BusinessVirtualAccountModel.is_active.is_(True),
        ).limit(1)
    )
    if existing.scalar_one_or_none():
        return

    biz_result = await session.execute(select(BusinessModel).where(BusinessModel.id == business_id))
    biz = biz_result.scalar_one_or_none()
    if biz is None:
        return

    acct_type = getattr(biz, "account_type", "business") or "business"
    account_name = (
        biz.business_name
        if acct_type == "business"
        else (owner_display_name or biz.business_name)
    )
    customer_name = owner_display_name or owner_email or account_name

    account_reference = f"fp-{biz.id}"

    if not Settings.is_production():
        # Non-production: create a simulated virtual account locally — no Monnify call needed.
        fake_acct_num = "000" + str(biz.id).replace("-", "")[:10]
        session.add(
            BusinessVirtualAccountModel(
                business_id=business_id,
                account_number=fake_acct_num,
                account_name=account_name,
                bank_name="FlowPilot Dev Bank",
                bank_code="000",
                account_reference=account_reference,
                provider="internal",
                is_primary=True,
                is_active=True,
            )
        )
        await session.flush()
        logger.info(
            "Non-production: simulated virtual account created for business %s (acct: %s)",
            business_id,
            fake_acct_num,
        )
        return

    bvn, nin = await _resolve_monnify_identity(session, business_id)
    if not bvn and not nin:
        logger.warning(
            "Skipping Monnify reserved account for business %s: no verified BVN or NIN on file "
            "(Monnify requires one of them to create a reserved account).",
            business_id,
        )
        return

    try:
        payment_service = PaymentService()
        va_response = await payment_service.create_reserved_account(
            account_reference=account_reference,
            account_name=account_name or "Customer",
            customer_email=owner_email,
            customer_name=customer_name,
            bvn=bvn,
            nin=nin,
        )
        va_body = va_response.get("responseBody", {})
        accounts = va_body.get("accounts", [])
        account = accounts[0] if accounts else {}
        acct_num = account.get("accountNumber") or ""
        if not acct_num:
            logger.warning(
                "Monnify reserved account returned no account number for business %s",
                business_id,
            )
            return
        session.add(
            BusinessVirtualAccountModel(
                business_id=business_id,
                account_number=acct_num,
                account_name=account_name,
                bank_name=account.get("bankName"),
                bank_code=account.get("bankCode"),
                account_reference=va_body.get("reservedAccountCode") or account_reference,
                provider="monnify",
                is_primary=True,
                is_active=True,
            )
        )
        await session.flush()
    except Exception as exc:
        logger.warning("Monnify reserved account creation failed for %s: %s", business_id, exc)
        return

    if notify:
        va_row = (
            await session.execute(
                select(BusinessVirtualAccountModel).where(
                    BusinessVirtualAccountModel.business_id == business_id,
                    BusinessVirtualAccountModel.is_active.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
        acct_display = va_row.account_number if va_row else ""
        bank_display = (va_row.bank_name if va_row else None) or "your assigned bank"
        notif_repo = NotificationRepository(session)
        await notif_repo.create(
            user_id=owner_user_id,
            business_id=business_id,
            title="Your wallet account details are ready",
            message=(
                f"Fund your FlowPilot wallet by transferring to account {acct_display} "
                f"at {bank_display}. Find these details on your Wallet page."
            ),
            type="success",
            resource_type="business",
            resource_id=str(business_id),
        )


async def _auto_verify_kyc(business_id: str, owner_email: str, owner_name: str, business_name: str) -> None:
    """Background task: wait 30 seconds then mark KYC as verified."""
    await asyncio.sleep(30)
    try:
        session_factory = get_session_factory()
        wants_kyc_email = True
        async with session_factory() as session:
            async with session.begin():
                bid = uuid.UUID(business_id)
                now = datetime.now(timezone.utc)

                # Only verify the latest pending submission — older rows are historical
                latest_sub = (await session.execute(
                    select(KycSubmissionModel)
                    .where(KycSubmissionModel.business_id == bid, KycSubmissionModel.status == "pending")
                    .order_by(KycSubmissionModel.submitted_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if latest_sub:
                    latest_sub.status = "verified"
                    latest_sub.verified_at = now
                    latest_sub.updated_at = now

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
                    select(UserModel)
                    .where(UserModel.email == owner_email)
                    .options(selectinload(UserModel.notification_pref_rows))
                )
                owner = owner_result.scalar_one_or_none()
                # Resolve prefs while session is active — avoid lazy IO after commit (xd2s).
                from src.services.email_service import check_notification_pref as _cnp_k_in

                wants_kyc_email = bool(owner and _cnp_k_in(owner, "kyc_updates"))
                if owner:
                    await _ensure_reserved_virtual_account(
                        session,
                        business_id=bid,
                        owner_user_id=owner.id,
                        owner_display_name=owner_name,
                        owner_email=owner_email,
                        notify=True,
                    )
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

        # Fetch virtual account created during verification
        _va_result = await session.execute(
            select(BusinessVirtualAccountModel).where(
                BusinessVirtualAccountModel.business_id == bid,
                BusinessVirtualAccountModel.is_active.is_(True),
            ).limit(1)
        )
        _va = _va_result.scalar_one_or_none()

        if wants_kyc_email:
            await email_service.send_kyc_verified_email(
                to=owner_email,
                display_name=owner_name,
                business_name=business_name,
                monthly_limit=_fmtb(_biz_l1["monthly"]) if _biz_l1 else "₦1,500,000",
                single_limit=_fmtb(_biz_l1["single"]) if _biz_l1 else "₦300,000",
                wallet_limit=_fmtb(_biz_l1["wallet"]) if _biz_l1 else "₦3,000,000",
                max_monthly_limit=_fmtb(_biz_max["monthly"]) if _biz_max else "₦50,000,000",
                account_number=_va.account_number if _va else None,
                bank_name=_va.bank_name if _va else None,
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

    # Block re-submission unless the latest submission was rejected
    latest_kyc = (
        await session.execute(
            select(KycSubmissionModel)
            .where(KycSubmissionModel.business_id == biz.id)
            .order_by(KycSubmissionModel.submitted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_kyc and latest_kyc.status in ("pending", "verified"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Your KYC submission is currently under review. "
                "You may only resubmit after a rejection."
                if latest_kyc.status == "pending"
                else "Your business is already verified. Use the upgrade endpoints to reach Level 2 or 3."
            ),
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

    # ── Always insert a new submission row — preserves full history ──────────
    kyc = KycSubmissionModel(
        business_id=biz.id,
        status="pending",
        business_type=business_type,
        registration_number=registration_number,
        tin_number=tin_number,
        director_name=director_name,
        director_bvn=director_bvn,
        trustee_name=trustee_name,
        trustee_bvn=trustee_bvn,
        scuml_number=scuml_number,
        partner_names=partner_names,
        authorized_officer_name=authorized_officer_name,
        authorized_officer_bvn=authorized_officer_bvn,
        submitted_at=now,
    )
    session.add(kyc)
    await session.flush()  # populate kyc.id for document FKs

    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="cac_certificate", storage_key=cac_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="tin_document", storage_key=tin_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="proof_of_address", storage_key=poa_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="director_id", storage_key=dir_id_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="trustee_id", storage_key=trustee_id_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="scuml_letter", storage_key=scuml_letter_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="partner_id", storage_key=partner_id_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="mda_letter", storage_key=mda_letter_key,
    )
    await _upsert_kyc_document(
        session, submission_id=kyc.id, business_id=biz.id,
        doc_type="authorized_officer_id", storage_key=auth_officer_id_key,
    )

    # Update business kyc_status + profile business_type
    biz.kyc_status = "pending"
    if business_type:
        bp_result = await session.execute(
            select(BusinessProfileModel).where(
                BusinessProfileModel.business_id == biz.id
            )
        )
        bp = bp_result.scalar_one_or_none()
        if bp is None:
            session.add(BusinessProfileModel(business_id=biz.id, business_type=business_type))
        else:
            bp.business_type = business_type
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

    await log_user_audit_event(
        session,
        event_type="kyc.business_submitted",
        user_id=user.id,
        business_id=biz.id,
        resource_type="kyc_submission",
        metadata={"business_type": business_type, "doc_count": len([k for k in [cac_key, tin_key, poa_key, dir_id_key, trustee_id_key, partner_id_key, scuml_letter_key, mda_letter_key, auth_officer_id_key] if k])},
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

    if Settings.VERIFICATION_ENV != "production":
        asyncio.create_task(_auto_verify_kyc(
            str(biz.id), user.email,
            user.display_name or user.email,
            biz.business_name,
        ))

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

    # Business account — return the latest submission (most recent by submitted_at)
    kyc_result = await session.execute(
        select(KycSubmissionModel)
        .options(
            selectinload(KycSubmissionModel.documents),
            selectinload(KycSubmissionModel.upgrade_requests),
        )
        .where(KycSubmissionModel.business_id == biz.id)
        .order_by(KycSubmissionModel.submitted_at.desc())
        .limit(1)
    )
    kyc = kyc_result.scalar_one_or_none()

    if kyc is None:
        return {
            "kyc_status": biz.kyc_status,
            "limit_info": limit_info,
            "submission": None,
        }

    cac_k = _kyc_doc_key(kyc, "cac_certificate")
    tin_k = _kyc_doc_key(kyc, "tin_document")
    poa_k = _kyc_doc_key(kyc, "proof_of_address")
    dir_k = _kyc_doc_key(kyc, "director_id")
    tru_k = _kyc_doc_key(kyc, "trustee_id")
    prt_k = _kyc_doc_key(kyc, "partner_id")
    scm_k = _kyc_doc_key(kyc, "scuml_letter")
    mda_k = _kyc_doc_key(kyc, "mda_letter")
    aoi_k = _kyc_doc_key(kyc, "authorized_officer_id")

    # Never return presigned document URLs once verified — security policy.
    is_verified = kyc.status == "verified"

    # Build a level→most-recent-request map: pending takes priority, otherwise latest by date.
    upgrade_by_level: dict[int, KycUpgradeRequestModel] = {}
    for r in sorted(kyc.upgrade_requests or [], key=lambda x: x.requested_at):
        if r.level not in upgrade_by_level or r.status == "pending":
            upgrade_by_level[r.level] = r

    def _upgrade_field(level: int, field: str):
        r = upgrade_by_level.get(level)
        if r is None:
            return None
        if field == "status":
            return r.status
        return r.requested_at.isoformat() if r.requested_at else None

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
            "has_cac_certificate": bool(cac_k),
            "has_tin_document": bool(tin_k),
            "has_director_id": bool(dir_k),
            "has_proof_of_address": bool(poa_k),
            # URLs only while pending/rejected; locked after verification (security policy).
            "cac_certificate_url": None if is_verified else _presigned(cac_k),
            "tin_document_url": None if is_verified else _presigned(tin_k),
            "proof_of_address_url": None if is_verified else _presigned(poa_k),
            "director_id_url": None if is_verified else _presigned(dir_k),
            "trustee_id_url": None if is_verified else _presigned(tru_k),
            "partner_id_url": None if is_verified else _presigned(prt_k),
            "scuml_letter_url": None if is_verified else _presigned(scm_k),
            "mda_letter_url": None if is_verified else _presigned(mda_k),
            "authorized_officer_id_url": None if is_verified else _presigned(aoi_k),
            "level_2_status": _upgrade_field(2, "status"),
            "level_2_requested_at": _upgrade_field(2, "requested_at"),
            "level_3_status": _upgrade_field(3, "status"),
            "level_3_requested_at": _upgrade_field(3, "requested_at"),
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
        wants_kyc_email = True
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
                    select(UserModel)
                    .where(UserModel.email == owner_email)
                    .options(selectinload(UserModel.notification_pref_rows))
                )
                owner = owner_result.scalar_one_or_none()
                from src.services.email_service import check_notification_pref as _cnp_kiv_in

                wants_kyc_email = bool(owner and _cnp_kiv_in(owner, "kyc_updates"))
                if owner and biz:
                    await _ensure_reserved_virtual_account(
                        session,
                        business_id=bid,
                        owner_user_id=owner.id,
                        owner_display_name=owner_name,
                        owner_email=owner_email,
                        notify=True,
                    )
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
        if wants_kyc_email:
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

    if ind and ind.level_1_status in ("pending", "verified"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Identity verification is already under review."
                if ind.level_1_status == "pending"
                else "Identity is already verified."
            ),
        )

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
    if Settings.VERIFICATION_ENV != "production":
        # Test mode: skip Monnify (live-only API) and auto-verify after 30 s
        await log_user_audit_event(
            session,
            event_type="kyc.individual_level1_submitted",
            user_id=user.id,
            business_id=biz.id,
            resource_type="kyc_submission",
            metadata={"id_type": id_type, "result": "pending_auto"},
        )
        await session.commit()
        asyncio.create_task(_auto_verify_individual_kyc(
            str(biz.id), 1, user.email, user.display_name or user.email,
        ))
        return {"status": "pending", "message": f"{id_type.upper()} submitted. Auto-verification in progress."}

    # Production: real verification with Monnify
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
        await _ensure_reserved_virtual_account(
            session,
            business_id=biz.id,
            owner_user_id=user.id,
            owner_display_name=(user.email or "customer").split("@", 1)[0],
            owner_email=user.email,
            notify=True,
        )
        va_row = (
            await session.execute(
                select(BusinessVirtualAccountModel).where(
                    BusinessVirtualAccountModel.business_id == biz.id,
                    BusinessVirtualAccountModel.is_active.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
        va_ref = (va_row.account_reference if va_row else None) or f"fp-{biz.id}"
        if id_type == "bvn":
            try:
                await payment_service.attach_bvn(
                    account_reference=va_ref,
                    bvn=id_value,
                )
            except Exception as exc:
                logger.warning("Failed attaching BVN to reserved account for %s: %s", biz.id, exc)
    else:
        biz.kyc_status = "rejected"
    biz.updated_at = now2
    await log_user_audit_event(
        session,
        event_type="kyc.individual_level1_submitted",
        user_id=user.id,
        business_id=biz.id,
        resource_type="kyc_submission",
        metadata={"id_type": id_type, "result": ind.level_1_status},
    )
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

    if ind.level_2_status in ("pending", "verified"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Address verification is already under review."
                if ind.level_2_status == "pending"
                else "Address is already verified."
            ),
        )

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
    await log_user_audit_event(
        session,
        event_type="kyc.individual_level2_submitted",
        user_id=user.id,
        business_id=biz.id,
        resource_type="kyc_submission",
        metadata={"has_document": bool(poa_key)},
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
    if Settings.VERIFICATION_ENV != "production":
        asyncio.create_task(_auto_verify_individual_kyc(
            str(biz.id), 2, user.email, user.display_name or user.email,
        ))

    return {"status": "pending", "message": "Address verification submitted and queued for review."}


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

    if ind.level_3_status in ("pending", "verified"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Government ID verification is already under review."
                if ind.level_3_status == "pending"
                else "Level 3 is already verified."
            ),
        )

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
    await log_user_audit_event(
        session,
        event_type="kyc.individual_level3_submitted",
        user_id=user.id,
        business_id=biz.id,
        resource_type="kyc_submission",
        metadata={"has_gov_id": bool(gov_id_key), "has_selfie": bool(selfie_key)},
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
    if Settings.VERIFICATION_ENV != "production":
        asyncio.create_task(_auto_verify_individual_kyc(
            str(biz.id), 3, user.email, user.display_name or user.email,
        ))

    return {"status": "pending", "message": "Government ID submitted for Level 3 review."}


# ── Business KYC upgrade requests (Level 2 and 3) ────────────────────────────
# For businesses, Level 2 and 3 upgrades require no additional documents.
# The owner submits a brief reason; the team reviews it (in prod) or it is
# auto-approved after a short delay (non-prod).

async def _auto_approve_business_upgrade(
    business_id: str, level: int, owner_email: str, owner_name: str, business_name: str
) -> None:
    """Background task: auto-approve a business KYC level upgrade in non-production."""
    await asyncio.sleep(30)
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                bid = uuid.UUID(business_id)
                now = datetime.now(timezone.utc)

                await session.execute(
                    update(KycUpgradeRequestModel)
                    .where(
                        KycUpgradeRequestModel.business_id == bid,
                        KycUpgradeRequestModel.level == level,
                        KycUpgradeRequestModel.status == "pending",
                    )
                    .values(status="verified", verified_at=now)
                )
                await session.execute(
                    update(BusinessModel)
                    .where(BusinessModel.id == bid)
                    .values(kyc_level=level, kyc_status="verified", updated_at=now)
                )

                owner_result = await session.execute(
                    select(UserModel)
                    .where(UserModel.email == owner_email)
                    .options(selectinload(UserModel.notification_pref_rows))
                )
                owner = owner_result.scalar_one_or_none()
                if owner:
                    notif_repo = NotificationRepository(session)
                    await notif_repo.create(
                        user_id=owner.id,
                        business_id=bid,
                        title=f"Business KYC Level {level} Approved",
                        message=f"{business_name} has been upgraded to Level {level}. Your payout limits have been increased.",
                        type="success",
                        resource_type="kyc",
                        resource_id=business_id,
                    )

        _limits = get_limits("business", level)
        _max_level = max(KYC_LIMITS.get("business", {}).keys(), default=level)
        def _fmtb(n) -> str:
            return f"\u20a6{float(n):,.0f}"
        from src.services.email_service import check_notification_pref as _cnp_bu
        if _limits:
            async with session_factory() as session:
                owner_check = (await session.execute(
                    select(UserModel)
                    .where(UserModel.email == owner_email)
                    .options(selectinload(UserModel.notification_pref_rows))
                )).scalar_one_or_none()
            if owner_check and _cnp_bu(owner_check, "kyc_updates"):
                await email_service.send_kyc_verified_email(
                    to=owner_email,
                    display_name=owner_name,
                    business_name=business_name,
                    monthly_limit=_fmtb(_limits["monthly"]),
                    single_limit=_fmtb(_limits["single"]),
                    wallet_limit=_fmtb(_limits["wallet"]),
                    max_monthly_limit=_fmtb(get_limits("business", _max_level)["monthly"]) if get_limits("business", _max_level) else "₦50,000,000",
                )
        logger.info("Business KYC Level %d auto-approved for business %s", level, business_id)
    except Exception as exc:
        logger.error("Business KYC upgrade auto-approve failed (level %d, business %s): %s", level, business_id, exc)


@router.post("/business/level2")
async def request_business_kyc_level2(
    reason: str = Form(..., min_length=10, max_length=1000, description="Why you need Level 2 limits"),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Request a business KYC Level 2 upgrade. No documents needed — provide a reason."""
    biz, user = await _get_business_and_owner(current_user, session)

    if getattr(biz, "account_type", "business") == "individual":
        raise HTTPException(status_code=400, detail="Individual accounts use /kyc/individual routes.")

    if (getattr(biz, "kyc_level", 0) or 0) < 1 or biz.kyc_status != "verified":
        raise HTTPException(status_code=400, detail="Complete Level 1 verification before requesting Level 2.")

    if (getattr(biz, "kyc_level", 0) or 0) >= 2:
        raise HTTPException(status_code=400, detail="Already at Level 2 or higher.")

    kyc_result = await session.execute(
        select(KycSubmissionModel)
        .where(KycSubmissionModel.business_id == biz.id, KycSubmissionModel.status == "verified")
        .order_by(KycSubmissionModel.submitted_at.desc())
        .limit(1)
    )
    kyc = kyc_result.scalar_one_or_none()
    if kyc is None:
        raise HTTPException(status_code=400, detail="No verified Level 1 submission found.")

    pending_check = (await session.execute(
        select(KycUpgradeRequestModel).where(
            KycUpgradeRequestModel.submission_id == kyc.id,
            KycUpgradeRequestModel.level == 2,
            KycUpgradeRequestModel.status == "pending",
        ).limit(1)
    )).scalar_one_or_none()
    if pending_check:
        raise HTTPException(status_code=400, detail="A Level 2 upgrade request is already under review.")

    now = datetime.now(timezone.utc)
    session.add(KycUpgradeRequestModel(
        submission_id=kyc.id,
        business_id=biz.id,
        level=2,
        status="pending",
        reason=reason.strip(),
        requested_at=now,
    ))

    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=user.id,
        business_id=biz.id,
        title="Level 2 Upgrade Requested",
        message="Your request to upgrade to KYC Level 2 is under review. We will notify you once it is approved.",
        type="info",
        resource_type="kyc",
        resource_id=str(biz.id),
    )
    await log_user_audit_event(
        session,
        event_type="kyc.business_level2_requested",
        user_id=user.id,
        business_id=biz.id,
        resource_type="kyc_submission",
        metadata={"reason_length": len(reason)},
    )
    await session.commit()

    if Settings.VERIFICATION_ENV != "production":
        asyncio.create_task(_auto_approve_business_upgrade(
            str(biz.id), 2, user.email, user.display_name or user.email, biz.business_name,
        ))

    return {"status": "pending", "message": "Level 2 upgrade request submitted and queued for review."}


@router.post("/business/level3")
async def request_business_kyc_level3(
    reason: str = Form(..., min_length=10, max_length=1000, description="Why you need Level 3 limits"),
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Request a business KYC Level 3 upgrade. No documents needed — provide a reason."""
    biz, user = await _get_business_and_owner(current_user, session)

    if getattr(biz, "account_type", "business") == "individual":
        raise HTTPException(status_code=400, detail="Individual accounts use /kyc/individual routes.")

    if (getattr(biz, "kyc_level", 0) or 0) < 2:
        raise HTTPException(status_code=400, detail="Complete Level 2 verification before requesting Level 3.")

    if (getattr(biz, "kyc_level", 0) or 0) >= 3:
        raise HTTPException(status_code=400, detail="Already at the maximum level.")

    kyc_result = await session.execute(
        select(KycSubmissionModel)
        .where(KycSubmissionModel.business_id == biz.id, KycSubmissionModel.status == "verified")
        .order_by(KycSubmissionModel.submitted_at.desc())
        .limit(1)
    )
    kyc = kyc_result.scalar_one_or_none()
    if kyc is None:
        raise HTTPException(status_code=400, detail="No verified KYC submission found.")

    pending_check = (await session.execute(
        select(KycUpgradeRequestModel).where(
            KycUpgradeRequestModel.submission_id == kyc.id,
            KycUpgradeRequestModel.level == 3,
            KycUpgradeRequestModel.status == "pending",
        ).limit(1)
    )).scalar_one_or_none()
    if pending_check:
        raise HTTPException(status_code=400, detail="A Level 3 upgrade request is already under review.")

    now = datetime.now(timezone.utc)
    session.add(KycUpgradeRequestModel(
        submission_id=kyc.id,
        business_id=biz.id,
        level=3,
        status="pending",
        reason=reason.strip(),
        requested_at=now,
    ))

    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=user.id,
        business_id=biz.id,
        title="Level 3 Upgrade Requested",
        message="Your request to upgrade to KYC Level 3 is under review. We will notify you once it is approved.",
        type="info",
        resource_type="kyc",
        resource_id=str(biz.id),
    )
    await log_user_audit_event(
        session,
        event_type="kyc.business_level3_requested",
        user_id=user.id,
        business_id=biz.id,
        resource_type="kyc_submission",
        metadata={"reason_length": len(reason)},
    )
    await session.commit()

    if Settings.VERIFICATION_ENV != "production":
        asyncio.create_task(_auto_approve_business_upgrade(
            str(biz.id), 3, user.email, user.display_name or user.email, biz.business_name,
        ))

    return {"status": "pending", "message": "Level 3 upgrade request submitted and queued for review."}
