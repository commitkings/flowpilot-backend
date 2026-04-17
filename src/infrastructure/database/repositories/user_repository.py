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
        email_verified_at: Optional[datetime] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> UserModel:
        """Create or update a user from an OAuth provider callback.

        Lookup order:
        1. By external_id  — returning Google user, just refresh fields.
        2. By email        — existing email/password account; link Google to it
                             so the user can use either sign-in method going forward.
        3. Neither found   — new user; INSERT with ON CONFLICT on external_id to
                             handle concurrent first-logins atomically.

        email_verified_at is only set when the existing record has not yet been
        verified (Google proving ownership is sufficient proof).
        external_provider is always written so GET /auth/connections is accurate.
        """
        now = datetime.now(timezone_mod.utc)
        normalized_email = email.strip().lower()

        # ── 1. Returning Google user ──────────────────────────────────────────
        user = await self.get_by_external_id(external_id)
        if user is not None:
            user.email = normalized_email
            user.display_name = display_name
            user.external_provider = "google"
            if avatar_url:
                user.avatar_url = avatar_url
            user.last_login_at = now
            if email_verified_at and user.email_verified_at is None:
                user.email_verified_at = email_verified_at
            await self._s.flush()
            await self._s.commit()
            return user

        # ── 2. Existing email/password account — link Google ──────────────────
        user = await self.get_by_email(normalized_email)
        if user is not None:
            user.external_id = external_id
            user.external_provider = "google"
            user.last_login_at = now
            # Don't overwrite an avatar the user already chose
            if avatar_url and not user.avatar_url:
                user.avatar_url = avatar_url
            # Google confirms the email — mark verified if not already
            if email_verified_at and user.email_verified_at is None:
                user.email_verified_at = email_verified_at
            await self._s.flush()
            await self._s.commit()
            return user

        # ── 3. Truly new user — atomic upsert handles concurrent first-logins ─
        insert_values: dict = {
            "external_id": external_id,
            "external_provider": "google",
            "email": normalized_email,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "last_login_at": now,
            "is_active": True,
        }
        if first_name:
            insert_values["first_name"] = first_name
        if last_name:
            insert_values["last_name"] = last_name
        if email_verified_at is not None:
            insert_values["email_verified_at"] = email_verified_at

        stmt = (
            pg_insert(UserModel)
            .values(**insert_values)
            .on_conflict_do_update(
                index_elements=["external_id"],
                set_={
                    "email": normalized_email,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "last_login_at": now,
                    "external_provider": "google",
                    **(
                        {"email_verified_at": email_verified_at}
                        if email_verified_at is not None
                        else {}
                    ),
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
        has_taken_tour: Optional[bool] = None,
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
            ("has_taken_tour", has_taken_tour),
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



