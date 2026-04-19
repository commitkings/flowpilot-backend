"""Append-only user audit events (CBN AML / security)."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import UserAuditEventModel


async def log_user_audit_event(
    session: AsyncSession,
    *,
    event_type: str,
    user_id: Optional[uuid.UUID] = None,
    business_id: Optional[uuid.UUID] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[uuid.UUID] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    row = UserAuditEventModel(
        user_id=user_id,
        business_id=business_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata_=metadata,
    )
    session.add(row)
