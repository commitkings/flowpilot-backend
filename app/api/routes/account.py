"""
Account actions — data export.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.user_repository import UserRepository

router = APIRouter(prefix="/account", tags=["account"])


@router.post("/export")
async def export_account_data(
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Generate a JSON export of all user data (GDPR-style)."""
    repo = UserRepository(session)
    memberships = await repo.get_memberships(current_user.id)

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "id": str(current_user.id),
            "email": current_user.email,
            "display_name": current_user.display_name,
            "first_name": current_user.first_name,
            "last_name": current_user.last_name,
            "job_title": current_user.job_title,
            "phone": current_user.phone,
            "timezone": current_user.timezone,
            "department": current_user.department,
            "avatar_url": current_user.avatar_url,
            "is_active": current_user.is_active,
            "created_at": (
                current_user.created_at.isoformat()
                if current_user.created_at
                else None
            ),
            "last_login_at": (
                current_user.last_login_at.isoformat()
                if current_user.last_login_at
                else None
            ),
        },
        "memberships": [
            {
                "business_id": str(m.business_id),
                "role": m.role,
                "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            }
            for m in memberships
        ],
    }

    content = json.dumps(export, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="flowpilot-export-{current_user.id}.json"'
        },
    )
