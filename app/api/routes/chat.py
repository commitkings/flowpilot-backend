import asyncio
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.agents.intent_agent import IntentAgent
from src.agents.orchestrator import RunOrchestrator
from src.agents.event_publisher import EventPublisher
from src.agents.state import AgentState
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import (
    CandidateRepository,
    ConversationRepository,
    InstitutionRepository,
    RunRepository,
)
from src.infrastructure.memory.redis_working_memory import append_turn

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatSendRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    conversation_id: Optional[str] = Field(
        None,
        description="Existing conversation ID. If null, a new conversation is created.",
    )
    business_id: str = Field(..., description="Business UUID for multi-tenancy scope")


class ChatMessageResponse(BaseModel):
    role: str
    content: str
    intent: Optional[str] = None
    confidence: Optional[float] = None
    extracted_slots: Optional[dict] = None
    created_at: Optional[str] = None


class ChatSendResponse(BaseModel):
    conversation_id: str
    response: str
    intent: str
    confidence: float
    extracted_slots: dict
    merged_slots: dict
    should_confirm: bool
    conversation_status: str
    run_config: Optional[dict] = None


class ConversationSummary(BaseModel):
    id: str
    title: Optional[str] = None
    status: str
    current_intent: Optional[str] = None
    message_count: int
    created_at: str
    updated_at: str


class ConversationDetail(BaseModel):
    id: str
    title: Optional[str] = None
    status: str
    current_intent: Optional[str] = None
    extracted_slots: dict
    resolved_run_config: Optional[dict] = None
    run_id: Optional[str] = None
    message_count: int
    messages: list[ChatMessageResponse]
    created_at: str
    updated_at: str


class ConfirmRunRequest(BaseModel):
    overrides: Optional[dict] = Field(
        None,
        description="Optional parameter overrides to apply before creating the run",
    )


class ConfirmRunResponse(BaseModel):
    conversation_id: str
    run_id: str
    objective: str
    status: str


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}") from exc


@router.post("/chat/send", response_model=ChatSendResponse)
async def chat_send(
    request: ChatSendRequest,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    business_uuid = _parse_uuid(request.business_id, "business_id")
    user_id = current_user.id
    conv_repo = ConversationRepository(session)

    if request.conversation_id:
        conv_uuid = _parse_uuid(request.conversation_id, "conversation_id")
        conv = await conv_repo.get_by_id(conv_uuid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.user_id != user_id:
            raise HTTPException(status_code=403, detail="Not your conversation")
        if conv.status in ("completed", "abandoned"):
            raise HTTPException(
                status_code=400,
                detail=f"Conversation is {conv.status} and cannot accept new messages",
            )
    else:
        conv = await conv_repo.create(
            business_id=business_uuid,
            user_id=user_id,
        )
        await session.commit()

    await conv_repo.add_message(
        conv.id,
        role="user",
        content=request.message,
    )
    await session.commit()
    await append_turn(str(conv.id), "user", request.message)

    messages = await conv_repo.get_messages(conv.id)
    history = [{"role": m.role, "content": m.content} for m in messages[:-1]]

    current_slots = conv.extracted_slots or {}

    agent = IntentAgent()
    try:
        result = await agent.process_message(
            user_message=request.message,
            conversation_history=history,
            current_slots=current_slots,
            business_id=request.business_id,
            user_id=str(user_id),
            db_session=session,
            conversation_id=str(conv.id),
        )
    except Exception as e:
        logger.error(f"IntentAgent failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Agent processing failed: {str(e)}"
        )

    intent = result["intent"]
    confidence = result["confidence"]
    extracted = result["extracted_slots"]
    merged = result["merged_slots"]
    response_text = result["response"]
    should_confirm = result["should_confirm"]

    token_usage = result.get("token_usage")
    token_usage_dict = None
    if token_usage and token_usage.get("entries"):
        total_prompt = sum(
            e.get("token_usage", {}).get("prompt_tokens", 0)
            for e in token_usage["entries"]
        )
        total_completion = sum(
            e.get("token_usage", {}).get("completion_tokens", 0)
            for e in token_usage["entries"]
        )
        token_usage_dict = {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "llm_calls": len(token_usage["entries"]),
        }

    await conv_repo.add_message(
        conv.id,
        role="assistant",
        content=response_text,
        intent_classification=intent,
        extracted_slots=extracted if extracted else None,
        confidence=confidence,
        token_usage=token_usage_dict,
    )

    new_status = conv.status
    if should_confirm and conv.status == "gathering":
        new_status = "confirming"

    run_config = None
    if intent == "create_payout_run" and merged.get("objective"):
        run_config = agent.build_run_config(merged, request.business_id)

    title = conv.title
    if not title and intent == "create_payout_run" and merged.get("objective"):
        objective = merged["objective"]
        title = objective[:100] if len(objective) <= 100 else objective[:97] + "..."

    await conv_repo.update_conversation(
        conv.id,
        status=new_status,
        current_intent=intent,
        extracted_slots=merged,
        resolved_run_config=run_config,
        title=title,
    )
    await session.commit()
    await append_turn(str(conv.id), "assistant", response_text)

    return ChatSendResponse(
        conversation_id=str(conv.id),
        response=response_text,
        intent=intent,
        confidence=confidence,
        extracted_slots=extracted,
        merged_slots=merged,
        should_confirm=should_confirm,
        conversation_status=new_status,
        run_config=run_config,
    )


@router.get("/chat/conversations")
async def list_conversations(
    business_id: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    business_uuid = _parse_uuid(business_id, "business_id")
    conv_repo = ConversationRepository(session)
    convs, total = await conv_repo.list_by_user(
        user_id=current_user.id,
        business_id=business_uuid,
        limit=limit,
        offset=offset,
    )
    return {
        "conversations": [
            ConversationSummary(
                id=str(c.id),
                title=c.title,
                status=c.status,
                current_intent=c.current_intent,
                message_count=c.message_count,
                created_at=c.created_at.isoformat(),
                updated_at=c.updated_at.isoformat(),
            ).model_dump()
            for c in convs
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/chat/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    conv_uuid = _parse_uuid(conversation_id, "conversation_id")
    conv_repo = ConversationRepository(session)
    conv = await conv_repo.get_by_id(conv_uuid, load_messages=True)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your conversation")

    return ConversationDetail(
        id=str(conv.id),
        title=conv.title,
        status=conv.status,
        current_intent=conv.current_intent,
        extracted_slots=conv.extracted_slots or {},
        resolved_run_config=conv.resolved_run_config,
        run_id=str(conv.run_id) if conv.run_id else None,
        message_count=conv.message_count,
        messages=[
            ChatMessageResponse(
                role=m.role,
                content=m.content,
                intent=m.intent_classification,
                confidence=float(m.confidence) if m.confidence is not None else None,
                extracted_slots=m.extracted_slots,
                created_at=m.created_at.isoformat() if m.created_at else None,
            )
            for m in (conv.messages or [])
        ],
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
    )


@router.post(
    "/chat/conversations/{conversation_id}/confirm",
    response_model=ConfirmRunResponse,
)
async def confirm_and_create_run(
    conversation_id: str,
    request: ConfirmRunRequest = ConfirmRunRequest(),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    conv_uuid = _parse_uuid(conversation_id, "conversation_id")
    conv_repo = ConversationRepository(session)
    conv = await conv_repo.get_by_id(conv_uuid)

    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your conversation")
    if conv.status not in ("confirming", "gathering"):
        raise HTTPException(
            status_code=400,
            detail=f"Conversation is in '{conv.status}' state — cannot confirm",
        )

    run_config = conv.resolved_run_config
    if not run_config:
        slots = conv.extracted_slots or {}
        if not slots.get("objective"):
            raise HTTPException(
                status_code=400,
                detail="Cannot create run: no objective has been extracted. Continue chatting to provide details.",
            )
        agent = IntentAgent()
        run_config = agent.build_run_config(slots, str(conv.business_id))

    if request.overrides:
        for key, value in request.overrides.items():
            if key in (
                "objective",
                "date_from",
                "date_to",
                "risk_tolerance",
                "budget_cap",
                "constraints",
                "candidates",
            ):
                run_config[key] = value

    if not run_config.get("objective"):
        raise HTTPException(status_code=400, detail="Run objective is required")

    run_repo = RunRepository(session)
    candidate_repo = CandidateRepository(session)
    institution_repo = InstitutionRepository(session)

    business_uuid = conv.business_id
    operator_id = current_user.id

    risk_tolerance = run_config.get("risk_tolerance", 0.35)
    budget_cap = run_config.get("budget_cap")
    merchant_id = run_config.get("merchant_id") or Settings.INTERSWITCH_MERCHANT_ID

    date_from = None
    if run_config.get("date_from"):
        try:
            date_from = date.fromisoformat(run_config["date_from"])
        except (ValueError, TypeError):
            pass

    date_to = None
    if run_config.get("date_to"):
        try:
            date_to = date.fromisoformat(run_config["date_to"])
        except (ValueError, TypeError):
            pass

    run = await run_repo.create(
        business_id=business_uuid,
        created_by=operator_id,
        objective=run_config["objective"],
        merchant_id=merchant_id,
        constraints=run_config.get("constraints"),
        date_from=date_from,
        date_to=date_to,
        risk_tolerance=Decimal(str(risk_tolerance)),
        budget_cap=Decimal(str(budget_cap)) if budget_cap is not None else None,
    )
    await session.commit()
    await session.refresh(run)
    run_id = str(run.id)

    candidate_dicts: list[dict] = []
    raw_candidates = run_config.get("candidates", [])
    if raw_candidates and isinstance(raw_candidates, list):
        candidate_rows = []
        for idx, c in enumerate(raw_candidates):
            if not isinstance(c, dict):
                continue
            candidate_rows.append(
                {
                    "source_label": f"Chat candidate {idx + 1}",
                    "institution_code": str(c.get("institution_code", "")),
                    "beneficiary_name": str(c.get("beneficiary_name", "")),
                    "account_number": str(c.get("account_number", "")),
                    "amount": Decimal(str(c.get("amount", 0))),
                    "currency": str(c.get("currency", "NGN")),
                    "purpose": c.get("purpose"),
                    "approval_status": "pending",
                    "execution_status": "not_started",
                }
            )

        if candidate_rows:
            from app.api.routes.runs import _normalize_candidate_institutions

            validation_errors = await _normalize_candidate_institutions(
                candidate_rows, institution_repo
            )
            if validation_errors:
                logger.warning(f"Candidate validation errors: {validation_errors[:5]}")

            valid_rows = [
                {k: v for k, v in row.items() if k != "source_label"}
                for row in candidate_rows
                if row.get("institution_code")
            ]
            if valid_rows:
                persisted = await candidate_repo.create_batch(
                    run.id, valid_rows, business_id=business_uuid
                )
                await session.commit()
                candidate_dicts = [
                    {
                        "candidate_id": str(p.id),
                        "institution_code": p.institution_code,
                        "beneficiary_name": p.beneficiary_name,
                        "account_number": p.account_number,
                        "amount": float(p.amount),
                        "currency": p.currency,
                        "purpose": p.purpose,
                    }
                    for p in persisted
                ]

    await conv_repo.update_conversation(
        conv.id,
        status="executing",
        run_id=run.id,
    )

    await conv_repo.add_message(
        conv.id,
        role="system",
        content=f"Run created: {run_id}. Objective: {run_config['objective']}. Pipeline is now executing.",
    )
    await session.commit()

    state: AgentState = {
        "run_id": run_id,
        "business_id": str(business_uuid),
        "objective": run_config["objective"],
        "constraints": run_config.get("constraints"),
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "risk_tolerance": float(risk_tolerance),
        "budget_cap": float(budget_cap) if budget_cap is not None else None,
        "merchant_id": merchant_id,
        "plan_steps": [],
        "transactions": [],
        "reconciled_ledger": {},
        "unresolved_references": [],
        "resolved_references": [],
        "scored_candidates": candidate_dicts,
        "forecast": None,
        "candidate_lookup_results": [],
        "candidate_execution_results": [],
        "batch_details": None,
        "approved_candidate_ids": [],
        "rejected_candidate_ids": [],
        "audit_report": None,
        "current_step": "created",
        "error": None,
        "audit_entries": [],
        "reasoning_log": [],
        "tool_call_log": [],
    }

    async def _run_pipeline():
        from src.infrastructure.database.connection import get_session_factory

        factory = get_session_factory()
        async with factory() as pipeline_session:
            try:
                publisher = EventPublisher(run.id, pipeline_session)
                orchestrator = RunOrchestrator(pipeline_session, publisher=publisher)
                final_state = await orchestrator.execute_run(run.id, state)

                final_status = "completed"
                if final_state.get("current_step") == "awaiting_approval":
                    final_status = "awaiting_approval"
                elif final_state.get("error"):
                    final_status = "failed"

                async with factory() as update_session:
                    update_conv_repo = ConversationRepository(update_session)
                    conv_status = (
                        "awaiting_approval"
                        if final_status == "awaiting_approval"
                        else "completed"
                    )
                    await update_conv_repo.update_conversation(
                        conv.id,
                        status=conv_status,
                    )
                    summary = f"Run {final_status}."
                    if final_state.get("error"):
                        summary += f" Error: {final_state['error']}"
                    await update_conv_repo.add_message(
                        conv.id,
                        role="system",
                        content=summary,
                    )
                    await update_session.commit()

            except Exception as e:
                logger.error(
                    f"Pipeline execution failed for run {run_id}: {e}", exc_info=True
                )
                try:
                    async with factory() as err_session:
                        err_conv_repo = ConversationRepository(err_session)
                        await err_conv_repo.update_conversation(
                            conv.id, status="completed"
                        )
                        await err_conv_repo.add_message(
                            conv.id,
                            role="system",
                            content=f"Run failed with error: {str(e)}",
                        )
                        await err_session.commit()
                except Exception:
                    logger.error("Failed to update conversation after pipeline error")

    asyncio.create_task(_run_pipeline())

    return ConfirmRunResponse(
        conversation_id=str(conv.id),
        run_id=run_id,
        objective=run_config["objective"],
        status="executing",
    )


@router.post("/chat/conversations/{conversation_id}/abandon")
async def abandon_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    conv_uuid = _parse_uuid(conversation_id, "conversation_id")
    conv_repo = ConversationRepository(session)
    conv = await conv_repo.get_by_id(conv_uuid)

    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your conversation")
    if conv.status in ("completed", "abandoned"):
        raise HTTPException(
            status_code=400, detail=f"Conversation already {conv.status}"
        )

    await conv_repo.update_conversation(conv.id, status="abandoned")
    await conv_repo.add_message(
        conv.id,
        role="system",
        content="Conversation abandoned by user.",
    )
    await session.commit()

    return {"conversation_id": str(conv.id), "status": "abandoned"}
