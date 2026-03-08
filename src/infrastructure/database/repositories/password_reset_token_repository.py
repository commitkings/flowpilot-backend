import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import PasswordResetTokenModel


class PasswordResetTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
    ) -> PasswordResetTokenModel:
        token = PasswordResetTokenModel(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._s.add(token)
        await self._s.flush()
        return token

    async def get_active_by_token_hash(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> Optional[PasswordResetTokenModel]:
        current_time = now or datetime.now(timezone.utc)
        result = await self._s.execute(
            select(PasswordResetTokenModel).where(
                PasswordResetTokenModel.token_hash == token_hash,
                PasswordResetTokenModel.used_at.is_(None),
                PasswordResetTokenModel.expires_at > current_time,
            )
        )
        return result.scalar_one_or_none()

    async def mark_used(
        self,
        token: PasswordResetTokenModel,
        *,
        used_at: datetime | None = None,
    ) -> PasswordResetTokenModel:
        token.used_at = used_at or datetime.now(timezone.utc)
        await self._s.flush()
        return token

    async def revoke_active_tokens_for_user(
        self,
        user_id: uuid.UUID,
        *,
        now: datetime | None = None,
        exclude_token_id: uuid.UUID | None = None,
    ) -> None:
        current_time = now or datetime.now(timezone.utc)
        stmt = (
            update(PasswordResetTokenModel)
            .where(
                PasswordResetTokenModel.user_id == user_id,
                PasswordResetTokenModel.used_at.is_(None),
                PasswordResetTokenModel.expires_at > current_time,
            )
            .values(used_at=current_time)
        )
        if exclude_token_id is not None:
            stmt = stmt.where(PasswordResetTokenModel.id != exclude_token_id)
        await self._s.execute(stmt)
