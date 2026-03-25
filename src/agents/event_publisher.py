"""EventPublisher — dual-write event system for real-time agent observability."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import InvalidRequestError, InterfaceError, IllegalStateChangeError

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """All event types emitted during a run."""
    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    STEP_PROGRESS = "step_progress"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    REASONING = "reasoning"
    APPROVAL_GATE = "approval_gate"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


# Global registry of active SSE subscribers: run_id → set of asyncio.Queue
_subscribers: dict[str, set[asyncio.Queue]] = {}


def subscribe(run_id: str) -> asyncio.Queue:
    """Register an SSE client for live events. Returns a queue to await."""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.setdefault(run_id, set()).add(q)
    logger.debug(f"SSE subscriber added for run {run_id} (total: {len(_subscribers[run_id])})")
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    """Remove an SSE client queue."""
    if run_id in _subscribers:
        _subscribers[run_id].discard(q)
        if not _subscribers[run_id]:
            del _subscribers[run_id]


class EventPublisher:
    """Emits structured events for a single run, dual-writing to DB + SSE broadcast."""

    def __init__(self, run_id: UUID, session: AsyncSession) -> None:
        self._run_id = run_id
        self._session = session
        self._sequence: int | None = None  # lazy-initialized from DB

    async def _init_sequence(self) -> None:
        """Initialize sequence counter from the max existing value in DB for this run."""
        from src.infrastructure.database.flowpilot_models import RunEventModel
        result = await self._session.execute(
            select(func.coalesce(func.max(RunEventModel.sequence_num), 0))
            .where(RunEventModel.run_id == self._run_id)
        )
        self._sequence = result.scalar_one()

    @property
    def run_id(self) -> UUID:
        return self._run_id

    def _next_sequence(self) -> int:
        if self._sequence is None:
            self._sequence = 0
        self._sequence += 1
        return self._sequence

    def _broadcast(
        self,
        event_type: EventType | str,
        payload: dict[str, Any],
        step_id: Optional[UUID] = None,
        *,
        seq: Optional[int] = None,
    ) -> None:
        message = {
            "seq": seq,
            "type": event_type.value if isinstance(event_type, EventType) else event_type,
            "step_id": str(step_id) if step_id else None,
            "payload": payload,
        }
        run_key = str(self._run_id)
        for q in list(_subscribers.get(run_key, [])):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning(f"SSE queue full for run {run_key}, dropping event {seq}")

    async def emit(
        self,
        event_type: EventType,
        payload: dict[str, Any],
        step_id: Optional[UUID] = None,
    ) -> None:
        """Emit an event: persist to DB and broadcast to SSE subscribers."""
        if self._sequence is None:
            await self._init_sequence()

        seq = self._next_sequence()

        # Build the event record
        from src.infrastructure.database.flowpilot_models import RunEventModel
        event = RunEventModel(
            run_id=self._run_id,
            step_id=step_id,
            event_type=event_type.value if isinstance(event_type, EventType) else event_type,
            payload=payload,
            sequence_num=seq,
        )
        self._session.add(event)
        try:
            await self._session.flush()
        except (InvalidRequestError, InterfaceError, IllegalStateChangeError) as exc:
            logger.debug(
                "Skipping DB persistence for run event",
                extra={
                    "run_id": str(self._run_id),
                    "sequence_num": seq,
                    "event_type": event_type.value if isinstance(event_type, EventType) else event_type,
                    "error": str(exc),
                },
                exc_info=True,
            )
        finally:
            self._broadcast(event_type, payload, step_id=step_id, seq=seq)

    # -- Convenience helpers for common event patterns --

    async def run_started(self, objective: str) -> None:
        await self.emit(EventType.RUN_STARTED, {"objective": objective})

    async def step_started(
        self, step_name: str, agent_type: str, description: str, step_id: Optional[UUID] = None
    ) -> None:
        await self.emit(
            EventType.STEP_STARTED,
            {"step_name": step_name, "agent_type": agent_type, "description": description},
            step_id=step_id,
        )

    async def step_progress(
        self, agent_type: str, message: str, detail: Optional[dict] = None, step_id: Optional[UUID] = None
    ) -> None:
        payload = {"agent_type": agent_type, "message": message}
        if detail:
            payload["detail"] = detail
        await self.emit(EventType.STEP_PROGRESS, payload, step_id=step_id)

    async def step_completed(
        self, step_name: str, agent_type: str, duration_ms: int, summary: str, step_id: Optional[UUID] = None
    ) -> None:
        await self.emit(
            EventType.STEP_COMPLETED,
            {"step_name": step_name, "agent_type": agent_type, "duration_ms": duration_ms, "summary": summary},
            step_id=step_id,
        )

    async def step_failed(
        self, step_name: str, agent_type: str, error: str, duration_ms: int = 0, step_id: Optional[UUID] = None
    ) -> None:
        await self.emit(
            EventType.STEP_FAILED,
            {"step_name": step_name, "agent_type": agent_type, "error": error, "duration_ms": duration_ms},
            step_id=step_id,
        )

    async def reasoning(
        self, agent_type: str, thinking: str, prompt_summary: Optional[str] = None,
        token_usage: Optional[dict] = None, step_id: Optional[UUID] = None
    ) -> None:
        payload: dict[str, Any] = {"agent_type": agent_type, "thinking": thinking}
        if prompt_summary:
            payload["prompt_summary"] = prompt_summary
        if token_usage:
            payload["token_usage"] = token_usage
        # Reasoning is observability only; don't race the request transaction for DB writes.
        seq = self._next_sequence()
        self._broadcast(EventType.REASONING, payload, step_id=step_id, seq=seq)

    async def approval_gate(self, candidates_summary: dict) -> None:
        await self.emit(EventType.APPROVAL_GATE, candidates_summary)

    async def run_completed(self, summary: str) -> None:
        await self.emit(EventType.RUN_COMPLETED, {"summary": summary})

    async def run_failed(self, error: str) -> None:
        await self.emit(EventType.RUN_FAILED, {"error": error})
