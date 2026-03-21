import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from groq import AsyncGroq

from src.agents.base import BaseAgent
from src.agents.intent_service import IntentService
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry
from src.config.settings import Settings

logger = logging.getLogger(__name__)

VALID_INTENTS = [
    "create_payout_run",
    "check_run_status",
    "review_candidates",
    "approve_reject",
    "explain_system",
    "view_audit",
    "modify_config",
    "greeting",
    "farewell",
    "acknowledgement",
    "unclear",
]

PAYOUT_RUN_SLOTS = {
    "objective": {
        "type": "string",
        "description": "What the payout run is for (e.g., 'Pay March salaries for engineering team')",
        "required": True,
    },
    "date_from": {
        "type": "string",
        "description": "Transaction search start date in ISO format (YYYY-MM-DD)",
        "required": False,
    },
    "date_to": {
        "type": "string",
        "description": "Transaction search end date in ISO format (YYYY-MM-DD)",
        "required": False,
    },
    "risk_tolerance": {
        "type": "number",
        "description": "Risk tolerance threshold from 0.0 (strictest) to 1.0 (most permissive). Default is 0.35",
        "required": False,
    },
    "budget_cap": {
        "type": "number",
        "description": "Maximum total payout amount allowed in this run",
        "required": False,
    },
    "candidates": {
        "type": "array",
        "description": "List of payout beneficiaries. Each needs: institution_code, beneficiary_name, account_number, amount. Optional: currency, purpose",
        "required": False,
    },
}

INTENT_SYSTEM_PROMPT = """You are **FlowPilot** — an intelligent multi-agent payout operations assistant built on Interswitch APIs.

## Who You Are
- You ARE FlowPilot. Speak in first person ("I can help", "I'll set that up").
- You are confident, direct, and financially literate — like a senior fintech ops lead.
- Keep responses to 2-3 sentences max unless the user asks for detail.

## What You Do
- Help users create payout runs by gathering parameters through natural conversation
- Check existing run statuses
- Explain how you work: Plan → Reconcile → Risk Score → Approve → Execute → Audit

## Key Facts
- Payouts are processed via Interswitch APIs
- A "run" flows through: Planning → Reconciliation → Risk Scoring → Human Approval → Execution → Audit
- Users must provide at minimum an objective (what the payout is for)
- Candidates can be added inline or uploaded via CSV later
- Risk tolerance: 0.0-1.0 (default 0.35), lower = stricter
- Budget cap limits total payout amount (optional)

## Conversation Rules
1. Be concise — 2-3 sentences max per response
2. Extract as many parameters as possible from a single message
3. Ask for missing REQUIRED parameters one at a time
4. When you have enough info, summarize briefly and ask for confirmation
5. Never fabricate data — use your tools to look up real info

## Tools
You have tools available. Use them to look up business info, check recent runs, and validate parameters. Always prefer tools over guessing."""


CLASSIFY_SYSTEM_PROMPT = """You are an intent classification engine for FlowPilot, a fintech payout automation platform.

Given a user message and conversation history, classify the user's intent into exactly ONE of these categories:

- create_payout_run: User wants to initiate, set up, or configure a new payout run. This includes messages about paying people, sending money, disbursements, salary payments, vendor payments, etc.
- check_run_status: User is asking about an existing run — its progress, results, whether it completed, etc.
- explain_system: User is asking how FlowPilot works, what features it has, what the pipeline does, etc.
- modify_config: User wants to change business settings, risk appetite, default parameters, etc.
- greeting: User is saying hello, starting a conversation, or making social small talk.
- farewell: User is ending the conversation, saying goodbye.
- unclear: Cannot determine what the user wants. Message is ambiguous, off-topic, or nonsensical.

Respond with ONLY a valid JSON object in this exact format:
{
  "intent": "<one of the intent labels above>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one sentence explaining why you chose this intent>"
}"""


EXTRACT_SYSTEM_PROMPT = """You are a parameter extraction engine for FlowPilot payout run configuration.

Given a user message and conversation context, extract any payout run parameters mentioned. Only extract what is explicitly stated or clearly implied — NEVER fabricate values.

The parameters you can extract are:
- objective (string): What the payout is for. E.g., "March salary payments", "vendor settlements Q1"
- date_from (string, YYYY-MM-DD): Start date for transaction reconciliation period
- date_to (string, YYYY-MM-DD): End date for transaction reconciliation period
- risk_tolerance (number, 0.0-1.0): How strict to be with risk scoring. 0.0 = block everything suspicious, 1.0 = allow everything
- budget_cap (number): Maximum total amount for the entire payout run
- candidates (array of objects): Beneficiary list. Each object should have: institution_code, beneficiary_name, account_number, amount. Optional: currency (default NGN), purpose

Respond with ONLY a valid JSON object:
{
  "extracted": {
    <only include keys where you found a value in the user's message>
  },
  "reasoning": "<brief explanation of what you extracted and why>"
}

If nothing can be extracted, respond with: {"extracted": {}, "reasoning": "No payout parameters found in this message"}"""


RESPONSE_SYSTEM_PROMPT = """You are **FlowPilot** — an intelligent payout operations assistant that helps businesses automate multi-agent payout runs via Interswitch.

## Identity
- You ARE FlowPilot. Say "I" when referring to yourself. Never say "our system" or "the pipeline" as if you are separate from it.
- You are confident, concise, and professional — like a senior fintech ops lead, not a chatbot.
- Keep responses to **2-3 sentences max** unless the user explicitly asks for a detailed explanation.

## Response Rules

For `create_payout_run`:
- If objective is missing, ask for it in ONE natural sentence
- If objective exists but optional params are missing, offer to proceed: "I have what I need — want me to kick off the run, or add more detail (budget cap, risk tolerance, beneficiaries)?"
- When all params are gathered, give a **brief summary** and ask for confirmation

For `check_run_status`:
- Use the get_recent_runs tool, then summarize in 1-2 sentences

For `explain_system`:
- Keep it tight: "I run payouts through a 6-step pipeline: Plan → Reconcile → Risk Score → Approve → Execute → Audit."
- Only elaborate if asked follow-up questions

For `greeting`:
- "Hey! 👋 I'm FlowPilot — I help you run payouts end-to-end. What are we working on today?"

For `farewell`:
- Brief and professional: "Cheers! I'll be here when you need me."

For `acknowledgement`:
- Respond naturally in 1 sentence, referencing what was just discussed

For `unclear`:
- Ask ONE clarifying question

CRITICAL: Your response is sent DIRECTLY to the user. No JSON, no metadata — just the message text. Be concise."""


def _build_intent_tools(
    business_id: str,
    user_id: str,
    db_session,
    conversation_id: Optional[str] = None,
) -> list[Tool]:
    async def get_business_info() -> dict:
        try:
            from src.infrastructure.database.repositories.business_repository import (
                BusinessRepository,
            )
            import uuid

            repo = BusinessRepository(db_session)
            biz = await repo.get_by_id(uuid.UUID(business_id))
            if not biz:
                return {"error": "Business not found"}

            from sqlalchemy import select
            from src.infrastructure.database.flowpilot_models import BusinessConfigModel

            result = await db_session.execute(
                select(BusinessConfigModel).where(
                    BusinessConfigModel.business_id == uuid.UUID(business_id)
                )
            )
            config = result.scalar_one_or_none()

            return {
                "business_name": biz.business_name,
                "business_type": biz.business_type,
                "risk_appetite": config.risk_appetite if config else None,
                "default_risk_tolerance": float(config.default_risk_tolerance)
                if config and config.default_risk_tolerance
                else 0.35,
                "default_budget_cap": float(config.default_budget_cap)
                if config and config.default_budget_cap
                else None,
                "primary_use_cases": config.primary_use_cases if config else None,
                "primary_bank": config.primary_bank if config else None,
            }
        except Exception as e:
            logger.error(f"get_business_info failed: {e}")
            return {"error": str(e)}

    async def get_recent_runs(limit: int = 5) -> dict:
        try:
            from src.infrastructure.database.repositories.run_repository import (
                RunRepository,
            )
            import uuid

            repo = RunRepository(db_session)
            bid = uuid.UUID(business_id)
            runs, total = await repo.list_by_business(
                bid, limit=min(limit, 10), offset=0
            )
            return {
                "total_runs": total,
                "recent_runs": [
                    {
                        "run_id": str(r.id),
                        "objective": r.objective,
                        "status": r.status,
                        "created_at": r.created_at.isoformat()
                        if r.created_at
                        else None,
                    }
                    for r in runs
                ],
            }
        except Exception as e:
            logger.error(f"get_recent_runs failed: {e}")
            return {"error": str(e)}

    async def get_working_memory_turns() -> dict:
        if not conversation_id:
            return {"note": "No conversation scope", "turns": []}
        try:
            from src.infrastructure.memory.redis_working_memory import (
                get_recent_turns,
            )

            turns = await get_recent_turns(conversation_id, limit=24)
            return {"turn_count": len(turns), "turns": turns}
        except Exception as e:
            logger.error(f"get_working_memory_turns failed: {e}")
            return {"error": str(e)}

    async def search_similar_run_memory(search_query: str) -> dict:
        try:
            import uuid as uuid_mod

            from src.infrastructure.database.repositories.run_memory_digest_repository import (
                RunMemoryDigestRepository,
            )

            repo = RunMemoryDigestRepository(db_session)
            bid = uuid_mod.UUID(business_id)
            rows = await repo.search_similar(bid, search_query, limit=5)
            return {"matches": rows}
        except Exception as e:
            logger.error(f"search_similar_run_memory failed: {e}", exc_info=True)
            return {"error": str(e)}

    async def validate_institution(code: str) -> dict:
        try:
            from src.infrastructure.database.repositories.institution_repository import (
                InstitutionRepository,
            )

            repo = InstitutionRepository(db_session)
            institutions = await repo.get_all_active()
            for inst in institutions:
                if inst.institution_code == code or (
                    inst.short_name and inst.short_name.lower() == code.lower()
                ):
                    return {
                        "valid": True,
                        "institution_code": inst.institution_code,
                        "institution_name": inst.institution_name,
                    }
            return {
                "valid": False,
                "message": f"Institution code '{code}' not found",
                "available_count": len(institutions),
            }
        except Exception as e:
            logger.error(f"validate_institution failed: {e}")
            return {"error": str(e)}

    async def list_institutions() -> dict:
        try:
            from src.infrastructure.database.repositories.institution_repository import (
                InstitutionRepository,
            )

            repo = InstitutionRepository(db_session)
            institutions = await repo.get_all_active()
            return {
                "count": len(institutions),
                "institutions": [
                    {
                        "code": inst.institution_code,
                        "name": inst.institution_name,
                        "short_name": inst.short_name,
                    }
                    for inst in institutions[:30]
                ],
            }
        except Exception as e:
            logger.error(f"list_institutions failed: {e}")
            return {"error": str(e)}

    async def get_last_candidates(limit_runs: int = 15) -> dict:
        """Candidates from the most recent run that had payout rows (for chat context)."""
        try:
            import uuid as uuid_mod

            from src.infrastructure.database.repositories.candidate_repository import (
                CandidateRepository,
            )
            from src.infrastructure.database.repositories.run_repository import (
                RunRepository,
            )

            bid = uuid_mod.UUID(business_id)
            run_repo = RunRepository(db_session)
            cand_repo = CandidateRepository(db_session)
            runs, _ = await run_repo.list_by_business(bid, limit=limit_runs, offset=0)
            for run in runs:
                rows = await cand_repo.get_by_run(run.id)
                if not rows:
                    continue
                out = []
                for c in rows[:100]:
                    out.append(
                        {
                            "institution_code": c.institution_code,
                            "beneficiary_name": c.beneficiary_name,
                            "account_number": c.account_number,
                            "amount": float(c.amount) if c.amount is not None else 0.0,
                            "purpose": c.purpose or "",
                        }
                    )
                return {
                    "source_run_id": str(run.id),
                    "objective": run.objective,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "candidate_count": len(rows),
                    "candidates": out,
                }
            return {
                "candidate_count": 0,
                "candidates": [],
                "note": "No previous runs with payout candidates for this business.",
            }
        except Exception as e:
            logger.error(f"get_last_candidates failed: {e}", exc_info=True)
            return {"error": str(e)}

    return [
        Tool(
            name="get_business_info",
            description="Get the current business profile, risk settings, and default configuration",
            parameters=[],
            execute=lambda **_kw: get_business_info(),
        ),
        Tool(
            name="get_recent_runs",
            description="Get recent payout runs for this business to check status or reference",
            parameters=[
                ToolParam(
                    name="limit",
                    param_type=ToolParamType.INTEGER,
                    description="Number of recent runs to fetch (max 10)",
                    required=False,
                    default=5,
                ),
            ],
            execute=lambda **kw: get_recent_runs(limit=kw.get("limit", 5)),
        ),
        Tool(
            name="get_last_candidates",
            description=(
                "Load beneficiary candidates from the most recent run that had payout rows "
                "(count + list for context or re-use suggestions)"
            ),
            parameters=[
                ToolParam(
                    name="limit_runs",
                    param_type=ToolParamType.INTEGER,
                    description="How many recent runs to scan for candidates (default 15)",
                    required=False,
                    default=15,
                ),
            ],
            execute=lambda **kw: get_last_candidates(
                limit_runs=int(kw.get("limit_runs", 15))
            ),
        ),
        Tool(
            name="validate_institution",
            description="Check if a bank/institution code is valid in the system",
            parameters=[
                ToolParam(
                    name="code",
                    param_type=ToolParamType.STRING,
                    description="Institution code or short name to validate",
                    required=True,
                ),
            ],
            execute=lambda **kw: validate_institution(code=kw["code"]),
        ),
        Tool(
            name="list_institutions",
            description="List available banks/institutions for payout destinations",
            parameters=[],
            execute=lambda **_kw: list_institutions(),
        ),
        Tool(
            name="get_working_memory_turns",
            description=(
                "Short-term memory: recent user/assistant turns for this chat "
                "(Redis mirror; use for continuity)"
            ),
            parameters=[],
            execute=lambda **_kw: get_working_memory_turns(),
        ),
        Tool(
            name="search_similar_run_memory",
            description=(
                "Long-term memory: find past completed runs similar to a phrase "
                "(objective + digest summary; pg_trgm similarity)"
            ),
            parameters=[
                ToolParam(
                    name="search_query",
                    param_type=ToolParamType.STRING,
                    description="Natural language: e.g. December payroll, vendor batch",
                    required=True,
                ),
            ],
            execute=lambda **kw: search_similar_run_memory(
                search_query=str(kw.get("search_query", ""))
            ),
        ),
    ]


class IntentAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("IntentAgent")

    async def process_message(
        self,
        user_message: str,
        conversation_history: list[dict],
        current_slots: dict,
        business_id: str,
        user_id: str,
        db_session=None,
        conversation_id: Optional[str] = None,
    ) -> dict:
        self.registry = ToolRegistry()
        history_for_llm = conversation_history
        if conversation_id and db_session:
            from src.infrastructure.memory.redis_working_memory import (
                get_recent_turns,
            )

            stm = await get_recent_turns(conversation_id)
            if stm and len(stm) >= len(conversation_history):
                history_for_llm = stm

        if db_session:
            tools = _build_intent_tools(
                business_id, user_id, db_session, conversation_id=conversation_id
            )
            for tool in tools:
                self.registry.register(tool)

        classification = await self._classify_intent(user_message, history_for_llm)
        intent = classification.get("intent", "unclear")
        confidence = classification.get("confidence", 0.0)

        extracted = {}
        if intent == "create_payout_run":
            extraction = await self._extract_slots(
                user_message, history_for_llm, current_slots
            )
            extracted = extraction.get("extracted", {})

        merged_slots = {**current_slots}
        for key, value in extracted.items():
            if value is not None and value != "" and value != []:
                merged_slots[key] = value

        response_text = await self._generate_response(
            user_message=user_message,
            conversation_history=history_for_llm,
            intent=intent,
            slots=merged_slots,
            business_id=business_id,
        )

        should_confirm = (
            intent == "create_payout_run"
            and merged_slots.get("objective")
            and self._has_sufficient_slots(merged_slots)
        )

        return {
            "intent": intent,
            "confidence": confidence,
            "extracted_slots": extracted,
            "merged_slots": merged_slots,
            "response": response_text,
            "should_confirm": should_confirm,
            "reasoning": classification.get("reasoning", ""),
            "token_usage": {
                "total_entries": len(self._reasoning_entries),
                "entries": self._reasoning_entries,
            },
            "tool_calls": self.registry.call_log if self.registry else [],
        }

    async def _classify_intent(
        self,
        user_message: str,
        conversation_history: list[dict],
    ) -> dict:
        # ── Use the multi-layered IntentService (3-tier pipeline) ──
        try:
            service = IntentService()
            result = await service.classify(user_message, history=conversation_history)

            intent_str = result.legacy_intent
            if intent_str not in VALID_INTENTS:
                intent_str = "unclear"

            return {
                "intent": intent_str,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "_tier": result.tier,
                "_raw_intent": result.intent.value,
            }
        except Exception as e:
            logger.warning(f"[IntentAgent] IntentService failed, falling back to legacy: {e}")

        # ── Fallback: legacy single-shot LLM classification ──
        history_text = self._format_history(conversation_history, max_turns=6)

        user_prompt = f"""Conversation history:
{history_text}

Latest user message: "{user_message}"

Classify the intent of the latest user message."""

        raw = await self.llm_json_call(
            system_prompt=CLASSIFY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
        )

        try:
            result = json.loads(raw)
            if result.get("intent") not in VALID_INTENTS:
                result["intent"] = "unclear"
            return result
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"[IntentAgent] Failed to parse classification: {raw[:200]}")
            return {
                "intent": "unclear",
                "confidence": 0.0,
                "reasoning": "Failed to parse LLM response",
            }

    async def _extract_slots(
        self,
        user_message: str,
        conversation_history: list[dict],
        current_slots: dict,
    ) -> dict:
        history_text = self._format_history(conversation_history, max_turns=4)
        slots_so_far = json.dumps(current_slots, indent=2) if current_slots else "{}"

        user_prompt = f"""Conversation history:
{history_text}

Already extracted parameters:
{slots_so_far}

Latest user message: "{user_message}"

Extract any NEW payout run parameters from the latest message. Do not re-extract parameters already captured unless the user is explicitly changing them."""

        raw = await self.llm_json_call(
            system_prompt=EXTRACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
        )

        try:
            result = json.loads(raw)
            return result
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"[IntentAgent] Failed to parse extraction: {raw[:200]}")
            return {"extracted": {}, "reasoning": "Failed to parse extraction response"}

    async def _generate_response(
        self,
        user_message: str,
        conversation_history: list[dict],
        intent: str,
        slots: dict,
        business_id: str,
    ) -> str:
        history_text = self._format_history(conversation_history, max_turns=8)

        missing = self._get_missing_slots(slots)
        slots_summary = json.dumps(slots, indent=2, default=str) if slots else "{}"
        missing_text = (
            ", ".join(missing) if missing else "None — all key parameters gathered"
        )

        user_prompt = f"""Conversation history:
{history_text}

Latest user message: "{user_message}"

Current intent: {intent}
Parameters gathered so far: {slots_summary}
Missing parameters: {missing_text}

Generate your response to the user. Remember to use tools if you need to look up business info or validate anything."""

        response = await self.reason_and_act(
            system_prompt=RESPONSE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            max_iterations=5,
        )

        return response.strip()

    def _has_sufficient_slots(self, slots: dict) -> bool:
        if not slots.get("objective"):
            return False
        filled_optional = sum(
            1
            for key in [
                "budget_cap",
                "risk_tolerance",
                "date_from",
                "date_to",
                "candidates",
            ]
            if slots.get(key) is not None
        )
        return filled_optional >= 1

    def _get_missing_slots(self, slots: dict) -> list[str]:
        missing = []
        if not slots.get("objective"):
            missing.append("objective (what is this payout for?)")
        if slots.get("budget_cap") is None:
            missing.append("budget_cap (optional: maximum total payout)")
        if slots.get("candidates") is None:
            missing.append(
                "candidates (optional: beneficiary list, can also upload CSV later)"
            )
        return missing

    def _format_history(self, history: list[dict], max_turns: int = 6) -> str:
        if not history:
            return "(no prior messages)"
        recent = history[-max_turns:]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def build_run_config(self, slots: dict, business_id: str) -> dict:
        config = {
            "business_id": business_id,
            "objective": slots.get("objective", ""),
        }
        if slots.get("date_from"):
            config["date_from"] = slots["date_from"]
        if slots.get("date_to"):
            config["date_to"] = slots["date_to"]
        if slots.get("risk_tolerance") is not None:
            config["risk_tolerance"] = float(slots["risk_tolerance"])
        else:
            config["risk_tolerance"] = 0.35
        if slots.get("budget_cap") is not None:
            config["budget_cap"] = float(slots["budget_cap"])
        if slots.get("constraints"):
            config["constraints"] = slots["constraints"]
        if slots.get("candidates"):
            config["candidates"] = slots["candidates"]
        return config
