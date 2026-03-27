"""
User repository — upsert-on-first-login for external auth providers.
"""

import uuid
from datetime import datetime, timezone as timezone_mod
from typing import Optional

from sqlalchemy import func, select
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
        now = datetime.now(timezone_mod.utc)
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

    async def get_by_email(self, email: str) -> Optional[UserModel]:
        normalized_email = email.strip().lower()
        result = await self._s.execute(
            select(UserModel).where(func.lower(UserModel.email) == normalized_email)
        )
        return result.scalar_one_or_none()

    async def get_memberships(self, user_id: uuid.UUID) -> list[BusinessMemberModel]:
        result = await self._s.execute(
            select(BusinessMemberModel).where(
                BusinessMemberModel.user_id == user_id
            )
        )
        return list(result.scalars().all())

    async def update_profile(
        self,
        user_id: uuid.UUID,
        *,
        display_name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        job_title: Optional[str] = None,
        phone: Optional[str] = None,
        timezone: Optional[str] = None,
        department: Optional[str] = None,
    ) -> Optional[UserModel]:
        """Update mutable profile fields. Only non-None values are applied."""
        user = await self.get_by_id(user_id)
        if user is None:
            return None
        for field, value in [
            ("display_name", display_name),
            ("avatar_url", avatar_url),
            ("first_name", first_name),
            ("last_name", last_name),
            ("job_title", job_title),
            ("phone", phone),
            ("timezone", timezone),
            ("department", department),
        ]:
            if value is not None:
                setattr(user, field, value)
        user.updated_at = datetime.now(timezone_mod.utc)
        await self._s.flush()
        return user

    async def clear_last_login(self, user_id: uuid.UUID) -> None:
        """Set last_login_at to None (used on explicit logout)."""
        user = await self.get_by_id(user_id)
        if user is not None:
            user.last_login_at = None
            await self._s.flush()

    async def set_password(
        self, user_id: uuid.UUID, password_hash: str
    ) -> Optional[UserModel]:
        user = await self.get_by_id(user_id)
        if user is None:
            return None

        now = datetime.now(timezone_mod.utc)
        user.password_hash = password_hash
        user.password_changed_at = now
        user.updated_at = now
        await self._s.flush()
        return user



