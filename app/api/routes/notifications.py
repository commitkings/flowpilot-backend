"""User-facing notification routes (Gap 4)."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.notification_repository import (
    NotificationRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class CreateNotificationRequest(BaseModel):
    title: str
    message: str
    type: str = "info"
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None


def _serialize_notification(n) -> dict:
    return {
        "id": str(n.id),
        "title": n.title,
        "message": n.message,
        "type": n.type,
        "resource_type": n.resource_type,
        "resource_id": n.resource_id,
        "is_read": n.is_read,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "created_at": n.created_at.isoformat(),
    }


@router.get("/notifications")
async def list_notifications(
    is_read: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    repo = NotificationRepository(session)
    notifications, total = await repo.list_for_user(
        user_id=current_user.id,
        is_read=is_read,
        limit=limit,
        offset=offset,
    )
    unread = await repo.unread_count(current_user.id)
    return {
        "notifications": [_serialize_notification(n) for n in notifications],
        "total": total,
        "unread_count": unread,
        "limit": limit,
        "offset": offset,
    }


@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    try:
        nid = uuid.UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notification ID")
    repo = NotificationRepository(session)
    success = await repo.mark_read(nid, current_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "ok"}


@router.post("/notifications/read-all")
async def mark_all_read(
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    repo = NotificationRepository(session)
    count = await repo.mark_all_read(current_user.id)
    return {"marked_read": count}


@router.delete("/notifications/{notification_id}")
async def delete_notification(
    notification_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    try:
        nid = uuid.UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notification ID")
    repo = NotificationRepository(session)
    success = await repo.delete(nid, current_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "deleted"}
