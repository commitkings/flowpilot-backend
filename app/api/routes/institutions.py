import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import InstitutionRepository

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_institution(institution) -> dict:
    return {
        "institutionCode": institution.institution_code,
        "institutionName": institution.institution_name,
        "shortName": institution.short_name,
        "nipCode": institution.nip_code,
        "cbnCode": institution.cbn_code,
        "isActive": institution.is_active,
        "lastSyncedAt": institution.last_synced_at.isoformat() if institution.last_synced_at else None,
    }


@router.get("/institutions")
async def list_institutions(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    institution_repo = InstitutionRepository(session)

    cached = await institution_repo.get_all_active()
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="No institutions found. Seed the institution table with CBN/NIP bank codes.",
        )

    return {
        "count": len(cached),
        "source": "database",
        "data": [_serialize_institution(institution) for institution in cached],
    }
