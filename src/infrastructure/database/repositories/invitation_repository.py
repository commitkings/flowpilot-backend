"""Repository for InvitationModel — pending team invitations."""

import secrets
import uuid
from datetime import datetime, timedelta, timezone as tz
from typing import Optional

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.database.flowpilot_models import InvitationModel


def _generate_invite_token() -> str:
    return secrets.token_urlsafe(32)


def _invite_expires_at(days: int = 7) -> datetime:
    return datetime.now(tz.utc) + timedelta(days=days)


class InvitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        business_id: uuid.UUID,
        invited_email: str,
        role: str,
        invited_by_user_id: uuid.UUID,
    ) -> InvitationModel:
        token = _generate_invite_token()
        invite = InvitationModel(
            business_id=business_id,
            invited_email=invited_email.lower().strip(),
            role=role,
            invited_by_user_id=invited_by_user_id,
            token=token,
            status="pending",
            expires_at=_invite_expires_at(),
        )
        self._session.add(invite)
        await self._session.flush()
        await self._session.refresh(invite)
        return invite

    async def get_by_token(self, token: str) -> Optional[InvitationModel]:
        result = await self._session.execute(
            select(InvitationModel)
            .options(
                selectinload(InvitationModel.business),
                selectinload(InvitationModel.invited_by),
            )
            .where(InvitationModel.token == token)
        )
        return result.scalars().first()

    async def get_pending_by_email(self, email: str) -> list[InvitationModel]:
        """Return all pending, non-expired invites for the given email."""
        result = await self._session.execute(
            select(InvitationModel).where(
                and_(
                    InvitationModel.invited_email == email.lower().strip(),
                    InvitationModel.status == "pending",
                    InvitationModel.expires_at > datetime.now(tz.utc),
                )
            )
        )
        return list(result.scalars().all())

    async def get_pending_for_business(
        self, business_id: uuid.UUID, email: str
    ) -> Optional[InvitationModel]:
        """Check if there's already a pending invite for this email in this business."""
        result = await self._session.execute(
            select(InvitationModel).where(
                and_(
                    InvitationModel.business_id == business_id,
                    InvitationModel.invited_email == email.lower().strip(),
                    InvitationModel.status == "pending",
                    InvitationModel.expires_at > datetime.now(tz.utc),
                )
            )
        )
        return result.scalars().first()

    async def mark_accepted(self, invite: InvitationModel) -> None:
        invite.status = "accepted"
        await self._session.flush()

    async def mark_expired(self, invite: InvitationModel) -> None:
        invite.status = "expired"
        await self._session.flush()

    async def mark_revoked(self, invite: InvitationModel) -> None:
        invite.status = "revoked"
        await self._session.flush()

    async def get_by_id(self, invite_id: uuid.UUID) -> Optional[InvitationModel]:
        result = await self._session.execute(
            select(InvitationModel).where(InvitationModel.id == invite_id)
        )
        return result.scalars().first()

    async def refresh_token_and_expiry(self, invite: InvitationModel) -> str:
        """Generate a new token and reset the expiry. Returns the new token."""
        new_token = _generate_invite_token()
        invite.token = new_token
        invite.expires_at = _invite_expires_at()
        invite.status = "pending"
        await self._session.flush()
        return new_token
