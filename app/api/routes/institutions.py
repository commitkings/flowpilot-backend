import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import InstitutionRepository
from src.infrastructure.external_services.interswitch.payouts import PayoutClient

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_institution(institution) -> dict:
    return {
        "institutionCode": institution.institution_code,
        "institutionName": institution.institution_name,
        "isActive": institution.is_active,
        "lastSyncedAt": institution.last_synced_at.isoformat() if institution.last_synced_at else None,
    }


def _normalize_institution_rows(payload: dict) -> list[dict]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload.get("data"), list):
        records = payload["data"]
    elif isinstance(payload.get("institutions"), list):
        records = payload["institutions"]
    else:
        records = []

    last_synced_at = datetime.now(timezone.utc)
    rows: list[dict] = []
    for record in records:
        institution_code = (
            record.get("institutionCode")
            or record.get("institution_code")
            or record.get("code")
        )
        institution_name = (
            record.get("institutionName")
            or record.get("institution_name")
            or record.get("name")
        )
        if not institution_code or not institution_name:
            continue
        rows.append(
            {
                "institution_code": institution_code,
                "institution_name": institution_name,
                "is_active": bool(record.get("isActive", record.get("is_active", True))),
                "last_synced_at": last_synced_at,
            }
        )
    return rows


@router.get("/institutions")
async def list_institutions(session: AsyncSession = Depends(get_db_session)):
    institution_repo = InstitutionRepository(session)

    cached = await institution_repo.get_all_active()
    if cached:
        return {
            "count": len(cached),
            "source": "cache",
            "data": [_serialize_institution(institution) for institution in cached],
        }

    try:
        client = PayoutClient()
        result = await client.get_receiving_institutions()
        rows = _normalize_institution_rows(result)
        if rows:
            await institution_repo.upsert_batch(rows)
            await session.commit()
        cached = await institution_repo.get_all_active()
        return {
            "count": len(cached),
            "source": "api",
            "data": [_serialize_institution(institution) for institution in cached],
        }
    except Exception as e:
        logger.error(f"Failed to fetch institutions: {e}")
        raise HTTPException(status_code=502, detail=f"Interswitch API error: {str(e)}")
