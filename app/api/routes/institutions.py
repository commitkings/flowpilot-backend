import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
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
        "institutionType": institution.institution_type,
        "isActive": institution.is_active,
        "lastSyncedAt": institution.last_synced_at.isoformat() if institution.last_synced_at else None,
    }


@router.get("/institutions")
async def list_institutions(
    search: Optional[str] = Query(None, description="Filter by name or code"),
    institution_type: Optional[str] = Query(None, description="bank | microfinance | mobile_money | other"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    repo = InstitutionRepository(session)
    institutions, total = await repo.get_all_active(
        search=search,
        institution_type=institution_type,
        limit=limit,
        offset=offset,
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [_serialize_institution(i) for i in institutions],
    }
