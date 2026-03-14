"""Repository for user-facing notifications (Gap 4)."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import NotificationModel


class NotificationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        user_id: uuid.UUID,
        title: str,
        message: str,
        type: str = "info",
        business_id: Optional[uuid.UUID] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> NotificationModel:
        notification = NotificationModel(
            user_id=user_id,
            business_id=business_id,
            title=title,
            message=message,
            type=type,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        is_read: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[NotificationModel], int]:
        filters = [NotificationModel.user_id == user_id]
        if is_read is not None:
            filters.append(NotificationModel.is_read == is_read)

        base = select(NotificationModel).where(and_(*filters))

        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(count_stmt)).scalar() or 0

        rows_stmt = (
            base.order_by(NotificationModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self.session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def mark_read(self, notification_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        stmt = (
            update(NotificationModel)
            .where(
                NotificationModel.id == notification_id,
                NotificationModel.user_id == user_id,
            )
            .values(is_read=True, read_at=datetime.now(timezone.utc))
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        stmt = (
            update(NotificationModel)
            .where(
                NotificationModel.user_id == user_id,
                NotificationModel.is_read == False,  # noqa: E712
            )
            .values(is_read=True, read_at=datetime.now(timezone.utc))
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def delete(self, notification_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        from sqlalchemy import delete as sql_delete

        stmt = sql_delete(NotificationModel).where(
            NotificationModel.id == notification_id,
            NotificationModel.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def unread_count(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            NotificationModel.user_id == user_id,
            NotificationModel.is_read == False,  # noqa: E712
        )
        return (await self.session.execute(stmt)).scalar() or 0
