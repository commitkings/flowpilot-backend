import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.database.flowpilot_models import (
    ConversationModel,
    ConversationMessageModel,
)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        *,
        business_id: uuid.UUID,
        user_id: uuid.UUID,
        title: Optional[str] = None,
    ) -> ConversationModel:
        conv = ConversationModel(
            business_id=business_id,
            user_id=user_id,
            title=title,
            status="gathering",
            extracted_slots={},
            message_count=0,
        )
        self._s.add(conv)
        await self._s.flush()
        return conv

    async def get_by_id(
        self, conversation_id: uuid.UUID, *, load_messages: bool = False
    ) -> Optional[ConversationModel]:
        stmt = select(ConversationModel).where(ConversationModel.id == conversation_id)
        if load_messages:
            stmt = stmt.options(selectinload(ConversationModel.messages))
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        business_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ConversationModel], int]:
        base = (
            select(ConversationModel)
            .where(
                ConversationModel.user_id == user_id,
                ConversationModel.business_id == business_id,
            )
            .order_by(ConversationModel.updated_at.desc())
        )
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._s.execute(count_stmt)).scalar_one()
        rows = (await self._s.execute(base.limit(limit).offset(offset))).scalars().all()
        return list(rows), total

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        *,
        role: str,
        content: str,
        intent_classification: Optional[str] = None,
        extracted_slots: Optional[dict] = None,
        confidence: Optional[float] = None,
        token_usage: Optional[dict] = None,
    ) -> ConversationMessageModel:
        msg = ConversationMessageModel(
            conversation_id=conversation_id,
            role=role,
            content=content,
            intent_classification=intent_classification,
            extracted_slots=extracted_slots,
            confidence=Decimal(str(confidence)) if confidence is not None else None,
            token_usage=token_usage,
        )
        self._s.add(msg)
        await self._s.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(
                message_count=ConversationModel.message_count + 1,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self._s.flush()
        return msg

    async def update_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        status: Optional[str] = None,
        current_intent: Optional[str] = None,
        extracted_slots: Optional[dict] = None,
        resolved_run_config: Optional[dict] = None,
        run_id: Optional[uuid.UUID] = None,
        title: Optional[str] = None,
    ) -> Optional[ConversationModel]:
        conv = await self.get_by_id(conversation_id)
        if conv is None:
            return None
        if status is not None:
            conv.status = status
        if current_intent is not None:
            conv.current_intent = current_intent
        if extracted_slots is not None:
            conv.extracted_slots = extracted_slots
        if resolved_run_config is not None:
            conv.resolved_run_config = resolved_run_config
        if run_id is not None:
            conv.run_id = run_id
        if title is not None:
            conv.title = title
        conv.updated_at = datetime.now(timezone.utc)
        await self._s.flush()
        return conv

    async def get_messages(
        self, conversation_id: uuid.UUID
    ) -> list[ConversationMessageModel]:
        stmt = (
            select(ConversationMessageModel)
            .where(ConversationMessageModel.conversation_id == conversation_id)
            .order_by(ConversationMessageModel.id)
        )
        result = await self._s.execute(stmt)
        return list(result.scalars().all())
