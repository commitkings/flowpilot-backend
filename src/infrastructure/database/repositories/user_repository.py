"""
User repository — upsert-on-first-login for external auth providers.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    UserModel,
)


class UserRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_from_oauth(
        self,
        *,
        external_id: str,
        email: str,
        display_name: str,
        avatar_url: Optional[str] = None,
    ) -> UserModel:
        """Create or update a user from an OAuth provider callback.

        On conflict (external_id already exists) we update profile fields
        and bump last_login_at.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(UserModel)
            .values(
                external_id=external_id,
                email=email,
                display_name=display_name,
                avatar_url=avatar_url,
                last_login_at=now,
            )
            .on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "email": email,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "last_login_at": now,
                },
            )
            .returning(UserModel)
        )
        result = await self._s.execute(stmt)
        user = result.scalar_one()
        await self._s.commit()
        return user

    async def get_by_id(self, user_id: uuid.UUID) -> Optional[UserModel]:
        result = await self._s.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_external_id(self, external_id: str) -> Optional[UserModel]:
        result = await self._s.execute(
            select(UserModel).where(UserModel.external_id == external_id)
        )
        return result.scalar_one_or_none()

    async def get_memberships(self, user_id: uuid.UUID) -> list[BusinessMemberModel]:
        result = await self._s.execute(
            select(BusinessMemberModel).where(
                BusinessMemberModel.user_id == user_id
            )
        )
        return list(result.scalars().all())
