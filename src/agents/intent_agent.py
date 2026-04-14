import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from groq import AsyncGroq

from src.agents.base import BaseAgent
from src.agents.intent_service import IntentService
from src.agents.tools import Tool, ToolParam, ToolParamType, ToolRegistry
from src.config.settings import Settings

logger = logging.getLogger(__name__)

_INSTITUTION_NAME_STOPWORDS = {"bank", "plc", "limited", "ltd", "nigeria", "nigerian"}
_NON_NAME_TOKENS = {
    # Financial / domain terms
    "salary",
    "salaries",
    "vendor",
    "vendors",
    "settlement",
    "settlements",
    "payroll",
    "payment",
    "payments",
    "payout",
    "payouts",
    "budget",
    "risk",
    "threshold",
    "reconcile",
    "reconciliation",
    "bank",
    "account",
    "amount",
    "institution",
    "beneficiary",
    "candidate",
    "transaction",
    "naira",
    # Months
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    # Common verbs and request words (critical: prevents "give me some suggestions" etc.)
    "give",
    "get",
    "show",
    "list",
    "make",
    "run",
    "set",
    "use",
    "try",
    "find",
    "check",
    "send",
    "create",
    "start",
    "help",
    "want",
    "need",
    "tell",
    "update",
    "change",
    "pick",
    "select",
    "choose",
    # Pronouns, determiners, and filler words
    "me",
    "my",
    "i",
    "we",
    "our",
    "you",
    "your",
    "the",
    "a",
    "an",
    "some",
    "any",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    # Common adjectives/adverbs that aren't names
    "new",
    "old",
    "good",
    "bad",
    "please",
    "just",
    "also",
    "more",
    "other",
    "first",
    "last",
    "next",
    "all",
    "each",
    "every",
    # Misc conversational words that could form multi-word phrases
    "suggestions",
    "suggestion",
    "options",
    "option",
    "example",
    "examples",
    "like",
    "about",
    "from",
    "with",
    "for",
    "what",
    "how",
    "which",
    "said",
    "keep",
    "asking",
    "already",
    "gave",
    "told",
    "again",
    "lol",
    "mean",
    "ok",
    "okay",
    "yes",
    "no",
    "not",
    "can",
    "could",
    "would",
    "should",
    "will",
    "shall",
    "do",
    "does",
    "did",
    "done",
    "is",
    "am",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "here",
    "there",
}

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
        "required": True,
    },
    "date_to": {
        "type": "string",
        "description": "Transaction search end date in ISO format (YYYY-MM-DD)",
        "required": True,
    },
    "risk_tolerance": {
        "type": "number",
        "description": "Risk tolerance threshold from 0.0 (strictest) to 1.0 (most permissive). Default is 0.35",
        "required": True,
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

INTENT_SYSTEM_PROMPT = """You are FlowPilot AI, a friendly and knowledgeable payout operations assistant that helps businesses automate payout runs via Interswitch.

Speak in first person. You ARE FlowPilot AI. You're like a helpful colleague who genuinely cares about getting the job done right. Be warm, approachable, and human. Match the user's energy and tone.

Keep responses to 2-3 sentences. Be concise and natural.

NEVER use em dashes (the long dash character). Use commas, periods, or line breaks instead.

What you do:
- Help users create payout runs by gathering parameters conversationally
- Check existing run statuses
- Explain your 6-step pipeline: Plan, Reconcile, Risk Score, Approve, Execute, Audit

Key facts:
- Payouts go through Interswitch APIs
- A payout run is only ready for confirmation after you have the objective, start date, end date, risk tolerance, and at least one beneficiary
- Risk tolerance: 0.0 to 1.0 (default 0.35), lower means stricter
- Budget cap is optional
- Candidates can be added inline or uploaded via CSV

Rules:
1. Be concise. 2-3 sentences max per response.
2. Extract parameters from user messages proactively.
3. Ask for missing required info one at a time.
4. When ready, summarize briefly and ask for confirmation.
5. Use tools to look up real business info, never guess.
6. Sound warm and conversational, not corporate or robotic.
7. If the user asks for suggestions or examples, actually give them helpful suggestions! Don't just repeat the question.
8. If the user makes small talk or is casual, respond warmly and naturally before steering back to work.
9. When the user provides info you already asked for, acknowledge it and move on. Never ask the same question twice."""


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

Given a user message and conversation context, extract any payout run parameters mentioned. Only extract what is explicitly stated or clearly implied. NEVER fabricate values.

The parameters you can extract are:
- objective (string): What the payout is for. E.g., "March salary payments", "vendor settlements Q1"
- date_from (string, YYYY-MM-DD): Start date. Accept ANY date format the user gives ("April 1st 2026", "1/4/2026", "2026-04-01") and convert to YYYY-MM-DD.
- date_to (string, YYYY-MM-DD): End date. Same format rules as date_from.
- risk_tolerance (number, 0.0-1.0): How strict to be with risk scoring. 0.0 = block everything suspicious, 1.0 = allow everything. "use the default" means 0.35.
- budget_cap (number): Maximum total amount for the entire payout run. If user says "no budget cap" or "none", set to null.
- candidates (array of objects): Beneficiary list. Each object should have: institution_code (official bank code from your catalog, e.g. NIP/CBN code like 058 for GTBank), beneficiary_name, account_number, amount. You may also use a temporary key \"bank\" with the human bank name; institution_code is preferred when you know it. Optional: currency (default NGN), purpose

CRITICAL AMOUNT RULES:
- ALWAYS expand shorthand amounts to their full numeric value:
  "250k" = 250000, "1.5m" = 1500000, "500K" = 500000, "2M" = 2000000
- "k" means thousands (x1,000), "m" means millions (x1,000,000)
- NEVER return the abbreviated number (e.g. 250 for "250k" is WRONG, it must be 250000)

CRITICAL DATE RULES:
- Accept any human date format: "April 1st 2026", "1st April 2026", "april 2026", "01/04/2026", "2026-04-01"
- Always convert to YYYY-MM-DD format in your output
- If only month+year given, use the 1st as the day (e.g. "April 2026" = "2026-04-01")
- If the user gives month and day but NO year (e.g. "April 1st", "april 30th"), use the **current calendar year** from the server context, never an old training year like 2022
- Never invent a year before 2000 when the user did not state one

Respond with ONLY a valid JSON object:
{
  "extracted": {
    <only include keys where you found a value in the user's message>
  },
  "reasoning": "<brief explanation of what you extracted and why>"
}

If nothing can be extracted, respond with: {"extracted": {}, "reasoning": "No payout parameters found in this message"}"""


RESPONSE_SYSTEM_PROMPT = """You are FlowPilot AI, a warm, sharp, and genuinely helpful payout operations assistant.

You speak in first person. You ARE FlowPilot AI. Think of yourself as that one colleague everyone loves, you're approachable, you remember context, and you never make people repeat themselves. Match the user's vibe: if they're casual, be casual back. If they're in a hurry, get straight to the point.

STYLE RULES (CRITICAL):
- NEVER use em dashes (the long dash character). Use commas, periods, or semicolons instead.
- Keep responses to 2-3 sentences max. Only go longer if you're listing suggestions the user asked for.
- Be warm, natural, and human. Contractions are good ("I'll", "let's", "you've").
- Use everyday language, not formal business-speak.
- Reference what the user already told you to show you're paying attention.
- If the user seems frustrated or repeats themselves, acknowledge it kindly and fix the issue. Never ignore their frustration.

For create_payout_run:
- If objective is missing: "What's this payout for? Something like 'March salaries' or 'vendor settlements'?"
- If the user asks for SUGGESTIONS or HELP deciding, give 2-3 concrete examples relevant to their business (use get_business_info if needed). E.g. "Here are a few common ones: salary payroll, vendor settlements, commission payouts, or contractor payments. Which fits?"
- Before confirming a payout run, make sure you have the objective, start date, end date, risk tolerance, and at least one beneficiary.
- If objective exists, ask naturally for the next missing piece. Always acknowledge what you've already captured.
- When ready: give a brief summary and ask "Should I go ahead?"
- Never say you are about to create, execute, or submit the payout run inside a normal chat turn. Only the API confirmation flow can do that.
- Never claim a payout run has been created, executed, or validated inside normal chat unless the system has explicitly confirmed that action.
- Never claim bank or beneficiary validation happened unless a tool actually performed it.

For check_run_status:
- Use get_recent_runs tool, then summarize in 1-2 sentences.

For explain_system:
- Keep it simple and friendly: "I process payouts in 6 steps: Plan, Reconcile, Risk Score, Approve, Execute, and Audit."

For greeting:
- Be warm and match the user's energy. If they're casual ("hey dude"), be casual back ("Hey! Good to see you.").
- Always end with a gentle nudge: "What can I help you with today?" or "Ready to get something done?"

For farewell:
- "Catch you later! I'll be here whenever you need me."

For acknowledgement:
- Respond naturally in 1 sentence, building on what was just discussed.

For unclear:
- Ask one friendly clarifying question. If they seem to be providing info for an active payout flow, try to map it to the next missing parameter.

Your response goes DIRECTLY to the user. No JSON, no metadata. Just the message."""


def _normalize_institution_alias(value: str) -> str:
    return "".join(char for char in value.strip().lower() if char.isalnum())


def _institution_alias_variants(value: str) -> set[str]:
    normalized = _normalize_institution_alias(value)
    if not normalized:
        return set()

    variants = {normalized}
    variants.add(normalized.replace("guarantee", "guaranty"))
    variants.add(normalized.replace("guaranty", "guarantee"))

    nickname_map = {
        "gt": {"gtbank", "gtb", "guarantytrustbank", "guaranteetrustbank"},
        "gtbank": {"gtbank", "gtb", "guarantytrustbank", "guaranteetrustbank"},
        "gtb": {"gtbank", "gtb", "guarantytrustbank", "guaranteetrustbank"},
        "guarantytrustbank": {
            "gtbank",
            "gtb",
            "guarantytrustbank",
            "guaranteetrustbank",
        },
        "guaranteetrustbank": {
            "gtbank",
            "gtb",
            "guarantytrustbank",
            "guaranteetrustbank",
        },
    }
    variants.update(nickname_map.get(normalized, set()))
    return {variant for variant in variants if variant}


def _build_institution_alias_map(institutions: list[Any]) -> dict[str, str]:
    alias_map: dict[str, str] = {}

    for institution in institutions:
        raw_aliases = [
            getattr(institution, "institution_code", None),
            getattr(institution, "institution_name", None),
            getattr(institution, "short_name", None),
            getattr(institution, "nip_code", None),
            getattr(institution, "cbn_code", None),
        ]

        for raw_alias in raw_aliases:
            if not raw_alias:
                continue
            for variant in _institution_alias_variants(str(raw_alias)):
                alias_map.setdefault(variant, institution.institution_code)

        institution_name = str(getattr(institution, "institution_name", "") or "")
        tokens = [
            token
            for token in re.split(r"[^a-z0-9]+", institution_name.lower())
            if token
        ]
        acronym = "".join(
            token[0] for token in tokens if token not in _INSTITUTION_NAME_STOPWORDS
        )
        if len(acronym) >= 2:
            alias_map.setdefault(acronym, institution.institution_code)
            if "bank" in tokens:
                alias_map.setdefault(f"{acronym}bank", institution.institution_code)

    return alias_map


def _resolve_institution_code(raw_value: str, institutions: list[Any]) -> Optional[str]:
    if not raw_value:
        return None

    alias_map = _build_institution_alias_map(institutions)
    for variant in _institution_alias_variants(raw_value):
        resolved = alias_map.get(variant)
        if resolved:
            return resolved
    return None


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
            institutions = await repo.get_all_active_rows()
            resolved_code = _resolve_institution_code(code, institutions)
            if resolved_code:
                for inst in institutions:
                    if inst.institution_code == resolved_code:
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
            institutions = await repo.get_all_active_rows()
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
                    "created_at": run.created_at.isoformat()
                    if run.created_at
                    else None,
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
        current_intent: Optional[str] = None,
    ) -> dict:
        self.registry = ToolRegistry()
        current_slots = self._coalesce_legacy_candidate_fields(current_slots)
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

        classification = await self._classify_intent(
            user_message, history_for_llm, current_intent=current_intent
        )
        intent = classification.get("intent", "unclear")
        confidence = classification.get("confidence", 0.0)
        standalone_greeting = (
            intent == "greeting" and self._is_standalone_greeting_message(user_message)
        )

        if self._should_continue_payout_flow(
            user_message=user_message,
            current_slots=current_slots,
            current_intent=current_intent,
            classified_intent=intent,
        ):
            intent = "create_payout_run"
            confidence = max(confidence, 0.85)

        extracted = {}
        if intent == "create_payout_run":
            extraction = await self._extract_slots(
                user_message, history_for_llm, current_slots
            )
            extracted = extraction.get("extracted", {})
            contextual_updates = await self._extract_contextual_slot_updates(
                user_message,
                current_slots,
                extracted,
                db_session=db_session,
            )
            for key, value in contextual_updates.items():
                extracted[key] = value

            extracted = self._coalesce_legacy_candidate_fields(
                extracted,
                prefer_legacy=True,
            )
            # Validate and normalize extracted values deterministically
            extracted = self._validate_and_normalize_slots(extracted)

        merged_slots = {**current_slots}
        for key, value in extracted.items():
            if value is None or value == "" or value == []:
                continue

            if key == "candidates":
                merged_slots[key] = self._merge_candidates_slot(
                    current_slots.get("candidates"),
                    value,
                )
                continue

            merged_slots[key] = value

        merged_slots = self._coalesce_legacy_candidate_fields(merged_slots)

        if intent == "create_payout_run":
            merged_slots = self._snap_stale_payout_years(merged_slots)

        if intent == "create_payout_run" and db_session:
            await self._canonicalize_candidate_institutions(
                merged_slots, user_message, db_session
            )

        should_confirm = intent == "create_payout_run" and self._has_sufficient_slots(
            merged_slots
        )

        response_text = None
        if intent == "create_payout_run" and not self._is_help_or_suggestion_request(
            user_message
        ):
            response_text = self._build_required_slot_prompt(merged_slots)
            if response_text and standalone_greeting:
                response_text = (
                    f"{self._greeting_opener(user_message)} {response_text}"
                ).strip()

        if response_text is None and should_confirm:
            response_text = self.build_confirmation_prompt(merged_slots)

        if response_text is None:
            response_text = await self._generate_response(
                user_message=user_message,
                conversation_history=history_for_llm,
                intent=intent,
                slots=merged_slots,
                business_id=business_id,
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

    def _coalesce_legacy_candidate_fields(
        self,
        slots: dict,
        *,
        prefer_legacy: bool = False,
    ) -> dict:
        """Move stray candidate fields into candidates[0].

        Some model responses incorrectly return beneficiary fields at the top level,
        e.g. ``{"institution_code": "GT"}`` instead of nesting them inside
        ``candidates[0]``. The rest of the payout flow only reads
        ``candidates[].institution_code``, so the conversation gets stuck asking for
        the bank repeatedly.
        """
        if not isinstance(slots, dict):
            return {}

        merged = dict(slots)
        legacy_keys = (
            "beneficiary_name",
            "institution_code",
            "account_number",
            "amount",
            "bank",
            "institution",
            "bank_name",
        )
        legacy_fields = {
            key: merged.pop(key)
            for key in legacy_keys
            if merged.get(key) not in (None, "", [])
        }
        if not legacy_fields:
            return merged

        candidates = merged.get("candidates")
        if (
            isinstance(candidates, list)
            and candidates
            and isinstance(candidates[0], dict)
        ):
            first_candidate = dict(candidates[0])
            for key, value in legacy_fields.items():
                if prefer_legacy or first_candidate.get(key) in (None, "", []):
                    first_candidate[key] = value
            merged["candidates"] = [first_candidate, *candidates[1:]]
            return merged

        merged["candidates"] = [legacy_fields]
        return merged

    async def _classify_intent(
        self,
        user_message: str,
        conversation_history: list[dict],
        current_intent: Optional[str] = None,
    ) -> dict:
        # ── Use the multi-layered IntentService (4-tier pipeline) ──
        try:
            service = IntentService()
            result = await service.classify(
                user_message,
                history=conversation_history,
                active_intent=current_intent,
            )

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
            logger.warning(
                f"[IntentAgent] IntentService failed, falling back to legacy: {e}"
            )

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
            model=Settings.GROQ_LLM_MODEL_FAST,
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
        history_text = self._format_history(conversation_history, max_turns=6)
        slot_preamble = self._build_slot_preamble(current_slots)

        user_prompt = f"""Conversation history:
{history_text}

Parameters already captured:
{slot_preamble}

Latest user message: "{user_message}"

Extract any NEW payout run parameters from the latest message. Do not re-extract parameters already captured unless the user is explicitly changing them."""

        raw = await self.llm_json_call(
            system_prompt=EXTRACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.0,
            model=Settings.GROQ_LLM_MODEL_FAST,
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
        history_text = self._format_history(conversation_history, max_turns=12)

        slot_preamble = self._build_slot_preamble(slots)
        missing = self._get_missing_slots(slots)
        missing_text = (
            ", ".join(missing) if missing else "None. All key parameters are gathered."
        )

        user_prompt = f"""== CONVERSATION SO FAR ==
{history_text}

== PARAMETERS ALREADY GATHERED (do NOT ask for these again) ==
{slot_preamble}

== STILL MISSING ==
{missing_text}

== LATEST USER MESSAGE ==
\"{user_message}\"

Current intent: {intent}

IMPORTANT: The user has ALREADY provided the parameters listed above. Do NOT ask for any of them again. Only ask for missing parameters.
Generate your response to the user. Use tools if you need to look up business info or validate anything."""

        # NOTE: _generate_response uses reason_and_act with tools (ReAct loop).
        # The 8B model cannot handle tool-calling properly — it leaks raw
        # <function=...> syntax into the response. Use 70B here.
        response = await self.reason_and_act(
            system_prompt=RESPONSE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            max_iterations=5,
        )

        return response.strip()

    def _has_sufficient_slots(self, slots: dict) -> bool:
        return len(self._get_required_missing_slots(slots)) == 0

    def _get_missing_slots(self, slots: dict) -> list[str]:
        missing = []
        if not slots.get("objective"):
            missing.append("objective (what is this payout for?)")
        candidate_missing = self._candidate_details_missing(slots)
        if candidate_missing:
            missing.extend(candidate_missing)
        if not slots.get("date_from"):
            missing.append("date_from (required: start date in YYYY-MM-DD format)")
        if not slots.get("date_to"):
            missing.append("date_to (required: end date in YYYY-MM-DD format)")
        if slots.get("risk_tolerance") is None:
            missing.append(
                "risk_tolerance (required: number from 0.0 to 1.0, for example 0.35)"
            )
        if slots.get("budget_cap") is None:
            missing.append("budget_cap (optional: maximum total payout)")
        if not isinstance(slots.get("candidates"), list) or not slots.get("candidates"):
            missing.append(
                "candidates (required: at least one beneficiary with name, bank, account number, and amount)"
            )
        return missing

    def _get_required_missing_slots(self, slots: dict) -> list[str]:
        missing = []
        if not slots.get("objective"):
            missing.append("objective")
        candidate_missing = self._candidate_details_missing(slots)
        if candidate_missing:
            missing.extend(candidate_missing)
        if not slots.get("date_from"):
            missing.append("date_from")
        if not slots.get("date_to"):
            missing.append("date_to")
        if slots.get("risk_tolerance") is None:
            missing.append("risk_tolerance")
        candidates = slots.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            missing.append("candidates")
        return missing

    def _build_required_slot_prompt(self, slots: dict) -> Optional[str]:
        if not slots.get("objective"):
            return "What is this payout for? For example, March salaries or vendor settlements."

        candidate_missing = self._candidate_details_missing(slots)
        if candidate_missing:
            detail = candidate_missing[0]
            if detail.startswith("amount for "):
                label = detail.removeprefix("amount for ")
                return f"What payout amount should I use for {label}?"
            if detail.startswith("institution code for "):
                label = detail.removeprefix("institution code for ")
                return f"Which bank or institution should I use for {label}?"
            if detail.startswith("account number for "):
                label = detail.removeprefix("account number for ")
                return f"What account number should I use for {label}?"
            return f"I still need the {detail}. Can you share that?"

        if not slots.get("date_from"):
            return "What start date should I use for this payout run? Something like 'April 1st' or '01/04/2026' works fine."
        if not slots.get("date_to"):
            return "And what end date? Same format, anything you're comfortable with."
        if slots.get("risk_tolerance") is None:
            return (
                "What risk tolerance should I use, between 0.0 and 1.0? "
                "If you want a balanced setting, 0.35 is a good default."
            )
        if not isinstance(slots.get("candidates"), list) or not slots.get("candidates"):
            return (
                "Who should I pay first? Share the beneficiary's full name, bank, "
                "account number, and amount. You can start with just the name if that is all you have."
            )
        return None

    def _candidate_details_missing(self, slots: dict) -> list[str]:
        candidates = slots.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return []

        missing: list[str] = []
        for idx, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                missing.append(
                    f"candidate {idx} details (beneficiary name, bank, account number, amount)"
                )
                continue

            label = candidate.get("beneficiary_name") or f"candidate {idx}"
            if not candidate.get("beneficiary_name"):
                missing.append(f"beneficiary name for candidate {idx}")
            if not candidate.get("institution_code"):
                missing.append(f"institution code for {label}")
            if not candidate.get("account_number"):
                missing.append(f"account number for {label}")
            amount = candidate.get("amount")
            try:
                amount_value = float(amount)
            except (TypeError, ValueError):
                amount_value = 0.0
            if amount_value <= 0:
                missing.append(f"amount for {label}")
        return missing

    # ------------------------------------------------------------------
    # Slot value validation and normalization
    # ------------------------------------------------------------------

    def _validate_and_normalize_slots(self, extracted: dict) -> dict:
        """Parse and validate extracted slot values deterministically.

        This ensures:
        - Amounts like '500k', '1.5m', '₦200,000' are parsed to numbers
        - Dates are valid YYYY-MM-DD strings
        - Risk tolerance is clamped to [0.0, 1.0]
        - Candidate amounts get the same treatment
        """
        normalized = dict(extracted)

        # Normalize budget_cap
        if "budget_cap" in normalized:
            parsed = self._parse_amount(normalized["budget_cap"])
            if parsed is not None:
                normalized["budget_cap"] = parsed
            else:
                # Remove invalid value so it doesn't overwrite a good one
                del normalized["budget_cap"]

        # Normalize dates
        for date_key in ("date_from", "date_to"):
            if date_key in normalized:
                parsed = self._parse_date(normalized[date_key])
                if parsed:
                    normalized[date_key] = parsed
                else:
                    del normalized[date_key]

        normalized = self._snap_stale_payout_years(normalized)

        # Clamp risk tolerance
        if "risk_tolerance" in normalized:
            try:
                rt = float(normalized["risk_tolerance"])
                normalized["risk_tolerance"] = max(0.0, min(1.0, rt))
            except (TypeError, ValueError):
                del normalized["risk_tolerance"]

        # Normalize candidate amounts
        if "candidates" in normalized and isinstance(normalized["candidates"], list):
            for candidate in normalized["candidates"]:
                if not isinstance(candidate, dict):
                    continue
                raw_amount = candidate.get("amount")
                if raw_amount is not None:
                    parsed = self._parse_amount(raw_amount)
                    if parsed is not None:
                        candidate["amount"] = parsed

        return normalized

    @staticmethod
    def _snap_stale_payout_years(normalized: dict) -> dict:
        """If both bounds share an implausible past year (LLM default), use current year.

        Keeps month and day; only fixes obvious wrong-year pairs such as 2022-04-01
        to 2026-04-01 when the server year is 2026.
        """
        df_s = normalized.get("date_from")
        dt_s = normalized.get("date_to")
        if not isinstance(df_s, str) or not isinstance(dt_s, str):
            return normalized
        try:
            df = date.fromisoformat(df_s)
            dt = date.fromisoformat(dt_s)
        except ValueError:
            return normalized
        if df.year != dt.year:
            return normalized
        ref_y = date.today().year
        if df.year > ref_y - 3:
            return normalized
        try:
            df2 = df.replace(year=ref_y)
            dt2 = dt.replace(year=ref_y)
        except ValueError:
            return normalized
        out = dict(normalized)
        out["date_from"] = df2.isoformat()
        out["date_to"] = dt2.isoformat()
        return out

    @staticmethod
    def _parse_amount(value) -> Optional[float]:
        """Parse a human-friendly amount into a float.

        Handles: 500000, '500k', '1.5m', '₦200,000', '50000.50', 'naira 500k'
        Returns None if unparseable.
        """
        if isinstance(value, (int, float)):
            return float(value) if float(value) > 0 else None

        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        if not cleaned:
            return None

        # Remove currency prefixes
        cleaned = re.sub(r'^[₦$]?\s*', '', cleaned)
        cleaned = re.sub(r'^(?:ngn|naira)\s*', '', cleaned, flags=re.IGNORECASE)

        # Remove commas
        cleaned = cleaned.replace(',', '')

        # Extract multiplier suffix
        multiplier = 1.0
        suffix_match = re.search(r'([kKmMbB])\s*$', cleaned)
        if suffix_match:
            suffix = suffix_match.group(1).lower()
            multiplier = {'k': 1_000, 'm': 1_000_000, 'b': 1_000_000_000}[suffix]
            cleaned = cleaned[:suffix_match.start()].strip()

        try:
            result = float(cleaned) * multiplier
            return result if result > 0 else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_date(value) -> Optional[str]:
        """Parse a date string and normalize to YYYY-MM-DD.

        Handles: '2026-02-01', '01/02/2026', 'February 2026', '1st February 2026',
        'April 1st' (year defaults to today.year), 'april 30th'.
        Returns None if invalid.
        """
        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        if not cleaned:
            return None

        ref = date.today()

        # Already YYYY-MM-DD?
        iso_match = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', cleaned)
        if iso_match:
            try:
                d = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
                return d.isoformat()
            except ValueError:
                return None

        # DD/MM/YYYY or DD-MM-YYYY
        dmy_match = re.fullmatch(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', cleaned)
        if dmy_match:
            try:
                d = date(int(dmy_match.group(3)), int(dmy_match.group(2)), int(dmy_match.group(1)))
                return d.isoformat()
            except ValueError:
                return None

        month_names = {
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'may': 5, 'june': 6, 'july': 7, 'august': 8,
            'september': 9, 'october': 10, 'november': 11, 'december': 12,
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
            'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
        }

        lowered = cleaned.lower()
        # "1st april" -> "1 april" for stable tokenization
        deord = re.sub(r'(\d{1,2})(?:st|nd|rd|th)\b', r'\1', lowered)

        month_num: int | None = None
        for month_str in sorted(month_names.keys(), key=len, reverse=True):
            if month_str in deord:
                month_num = month_names[month_str]
                break
        if month_num is None:
            return None

        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', deord)
        explicit_year: int | None = int(year_match.group(1)) if year_match else None
        year = explicit_year if explicit_year is not None else ref.year

        scratch = deord
        if year_match:
            scratch = (deord[: year_match.start()] + " " + deord[year_match.end() :]).strip()

        day = 1
        day_match = re.search(r'(?<!\d)(\d{1,2})(?!\d)', scratch)
        if day_match:
            cand = int(day_match.group(1))
            if 1 <= cand <= 31:
                day = cand

        try:
            d = date(year, month_num, day)
        except ValueError:
            return None

        # Month+day without explicit year: nudge forward if far in the past
        if explicit_year is None and (d - ref).days < -120:
            try:
                d = date(year + 1, month_num, day)
            except ValueError:
                pass

        return d.isoformat()

    _HELP_REQUEST_PATTERNS = re.compile(
        r"\b(give me|show me|suggest|suggestions?|options?|examples?|help me|what (?:can|should)|recommend)\b",
        re.IGNORECASE,
    )

    def _is_help_or_suggestion_request(self, message: str) -> bool:
        return bool(self._HELP_REQUEST_PATTERNS.search(message))

    _STANDALONE_GREETING_RE = re.compile(
        r"^\s*(?:"
        r"hi+\b|hey+\b|hello+\b|yo+\b|sup\b|howdy\b|"
        r"good\s+(?:morning|afternoon|evening)\b|"
        r"what(?:'s|\s+is)\s+up\b"
        r")(?:\s+[a-z.'-]+){0,3}\s*[!?.]*\s*$",
        re.IGNORECASE,
    )

    def _is_standalone_greeting_message(self, message: str) -> bool:
        stripped = message.strip()
        if not stripped or len(stripped) > 60:
            return False
        if any(ch.isdigit() for ch in stripped):
            return False
        return bool(self._STANDALONE_GREETING_RE.match(stripped))

    @staticmethod
    def _greeting_opener(user_message: str) -> str:
        low = user_message.strip().lower()
        if low.startswith("hello"):
            return "Hello again!"
        if low.startswith("hey"):
            return "Hey!"
        if low.startswith("hi"):
            return "Hi there!"
        if low.startswith("good"):
            return "Good to hear from you!"
        return "Hi!"

    def _should_continue_payout_flow(
        self,
        user_message: str,
        current_slots: dict,
        current_intent: Optional[str],
        classified_intent: str,
    ) -> bool:
        if classified_intent == "create_payout_run":
            return False

        in_payout_flow = current_intent == "create_payout_run" or bool(
            current_slots.get("objective")
        )
        if not in_payout_flow:
            return False

        if classified_intent in ("check_run_status", "explain_system", "modify_config"):
            return False

        normalized = user_message.strip().lower()
        if not normalized:
            return False

        if self._extract_candidate_amount_from_message(user_message) is not None:
            return True

        payout_followup_markers = (
            "amount",
            "bank",
            "account",
            "beneficiary",
            "candidate",
            "budget",
            "risk",
            "salary",
            "salaries",
            "vendor",
            "payment",
            "payout",
        )
        if any(marker in normalized for marker in payout_followup_markers):
            return True

        return bool(self._get_required_missing_slots(current_slots))

    async def _canonicalize_candidate_institutions(
        self,
        slots: dict,
        user_message: str,
        db_session,
    ) -> None:
        """Map bank synonyms to canonical NIP/institution codes and drop invalid guesses.

        The extractor often fills ``bank`` / ``institution`` instead of ``institution_code``,
        or stores a non-canonical label like ``GT``. The UI may show that as satisfied while
        ``_candidate_details_missing`` still treats the beneficiary as incomplete.
        """
        if db_session is None:
            return
        candidates = slots.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return
        try:
            from src.infrastructure.database.repositories.institution_repository import (
                InstitutionRepository,
            )

            institutions = await InstitutionRepository(db_session).get_all_active_rows()
        except Exception as exc:
            logger.warning(
                "Could not load institutions for candidate canonicalization: %s", exc
            )
            return

        valid_codes = {
            getattr(i, "institution_code", None) for i in institutions
        }
        valid_codes.discard(None)

        for cand in candidates:
            if not isinstance(cand, dict):
                continue

            chunks: list[str] = []
            ic = str(cand.get("institution_code", "") or "").strip()
            if ic:
                chunks.append(ic)
            for syn in ("bank", "institution", "bank_name"):
                v = cand.get(syn)
                if v:
                    s = str(v).strip()
                    if s and s not in chunks:
                        chunks.append(s)
            chunks.sort(key=len, reverse=True)

            resolved: Optional[str] = None
            for ch in chunks:
                if ch in valid_codes:
                    resolved = ch
                    break
                r = _resolve_institution_code(ch, institutions)
                if r:
                    resolved = r
                    break

            if not resolved and user_message.strip():
                resolved = await self._extract_candidate_institution_from_message(
                    user_message, db_session=db_session
                )

            if resolved:
                cand["institution_code"] = resolved
            elif ic and ic not in valid_codes:
                cand.pop("institution_code", None)

            for syn in ("bank", "institution", "bank_name"):
                cand.pop(syn, None)

    async def _extract_contextual_slot_updates(
        self,
        user_message: str,
        current_slots: dict,
        extracted: dict,
        db_session=None,
    ) -> dict:
        updates: dict[str, Any] = {}

        if self._is_help_or_suggestion_request(user_message):
            return updates

        candidates = current_slots.get("candidates")
        if candidates is None or not isinstance(candidates, list) or len(candidates) < 1:
            return updates
        if not isinstance(candidates[0], dict):
            return updates

        candidate = dict(candidates[0]) if isinstance(candidates, list) and candidates else {}

        ext_cands = extracted.get("candidates")
        if isinstance(ext_cands, list) and len(ext_cands) == 1 and isinstance(ext_cands[0], dict):
            for key, value in ext_cands[0].items():
                if value is not None and value != "" and value != []:
                    candidate[key] = value

        changed = False

        parsed_name = None
        if not candidate.get("beneficiary_name"):
            parsed_name = self._extract_candidate_name_from_message(user_message)
        elif self._looks_like_name_correction(user_message):
            parsed_name = self._extract_candidate_name_from_message(user_message)
        if parsed_name:
            candidate["beneficiary_name"] = parsed_name
            changed = True

        if not candidate.get("institution_code"):
            parsed_institution = await self._extract_candidate_institution_from_message(
                user_message,
                db_session=db_session,
            )
            if parsed_institution:
                candidate["institution_code"] = parsed_institution
                changed = True

        if not candidate.get("account_number"):
            parsed_account_number = self._extract_candidate_account_number_from_message(
                user_message
            )
            if parsed_account_number:
                candidate["account_number"] = parsed_account_number
                changed = True

        amount = candidate.get("amount")
        try:
            current_amount = float(amount)
        except (TypeError, ValueError):
            current_amount = 0.0

        if current_amount <= 0:
            parsed_amount = self._extract_candidate_amount_from_message(user_message)
            if parsed_amount is not None:
                candidate["amount"] = parsed_amount
                changed = True

        if not changed:
            return updates

        if len(candidates) == 1:
            updates["candidates"] = [candidate]
        else:
            merged_list: list[Any] = []
            for i, c in enumerate(candidates):
                if i == 0:
                    merged_list.append(candidate)
                elif isinstance(c, dict):
                    merged_list.append(dict(c))
                else:
                    merged_list.append(c)
            updates["candidates"] = merged_list
        return updates

    def _extract_candidate_amount_from_message(
        self, user_message: str
    ) -> Optional[float]:
        if self._looks_like_account_number_message(user_message):
            return None

        stripped = user_message.strip()
        # Pure amount message: "250k", "₦500,000", "1.5m"
        pure_amount = re.fullmatch(
            r"(?:₦|ngn|naira)?\s*(\d[\d,]*(?:\.\d+)?)([kKmMbB])?\s*",
            stripped,
            re.IGNORECASE,
        )
        if pure_amount:
            try:
                value = float(pure_amount.group(1).replace(",", ""))
            except ValueError:
                return None
            suffix = (pure_amount.group(2) or "").lower()
            multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
            value *= multiplier
            return value if value > 0 else None

        # Amount embedded in context: "pay him 250k", "send 1.5m to vendor"
        embedded = re.search(
            r"(?:₦|ngn|naira)?\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmMbB])?",
            stripped,
        )
        if embedded:
            normalized = user_message.lower()
            # Only extract if there's a financial context marker
            has_marker = any(
                marker in normalized
                for marker in ("amount", "naira", "ngn", "₦", "k", "m", "thousand", "million", "pay", "send", "disburse")
            )
            has_suffix = embedded.group(2) is not None
            if has_marker or has_suffix:
                try:
                    value = float(embedded.group(1).replace(",", ""))
                except ValueError:
                    return None
                suffix = (embedded.group(2) or "").lower()
                multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
                value *= multiplier
                return value if value > 0 else None

        return None

    def _looks_like_account_number_message(self, user_message: str) -> bool:
        normalized = user_message.strip().lower()
        if not normalized:
            return False

        has_account_marker = any(
            marker in normalized
            for marker in (
                "account number",
                "acct number",
                "account no",
                "acct no",
                "a/c",
            )
        )
        if not has_account_marker:
            return False

        explicit_amount_markers = (
            "amount",
            "naira",
            "ngn",
            "₦",
            "thousand",
            "million",
            "billion",
        )
        if any(marker in normalized for marker in explicit_amount_markers):
            return False

        return bool(re.search(r"\b\d{10}\b", user_message))

    def _merge_candidates_slot(
        self, current_candidates: Any, new_candidates: Any
    ) -> list[dict]:
        """Merge candidate updates without dropping already captured fields."""
        if not isinstance(new_candidates, list) or not new_candidates:
            return current_candidates if isinstance(current_candidates, list) else []

        if (
            isinstance(current_candidates, list)
            and len(current_candidates) == 1
            and isinstance(current_candidates[0], dict)
            and len(new_candidates) == 1
            and isinstance(new_candidates[0], dict)
        ):
            merged = dict(current_candidates[0])
            for key, value in new_candidates[0].items():
                if value is not None and value != "" and value != []:
                    merged[key] = value
            return [merged]

        return new_candidates

    _NAME_CORRECTION_HINT = re.compile(
        r"\b(?:sorry|actually|i\s+meant|my\s+bad|correction|correct\s+that|"
        r"wrong\s+name|change\s+(?:it\s+)?to|spell(?:ing)?|"
        r"the\s+name\s+should\s+be|it(?:'s| is)\s+(?:actually\s+)?)\b",
        re.IGNORECASE,
    )

    def _looks_like_name_correction(self, user_message: str) -> bool:
        return bool(self._NAME_CORRECTION_HINT.search(user_message))

    def _extract_candidate_name_from_message(self, user_message: str) -> Optional[str]:
        patterns = [
            r"(?:beneficiary|recipient|vendor|employee|staff|friend|contractor)(?:'s)?\s+name\s+is\s+([A-Za-z][A-Za-z .'\-]{1,80})",
            r"(?:beneficiary|recipient|vendor|employee|staff|friend|contractor)\s+is\s+([A-Za-z][A-Za-z .'\-]{1,80})",
            r"(?:sorry|actually|i\s+meant|my\s+bad)[,!\s]+(?:the\s+)?name\s+is\s+([A-Za-z][A-Za-z .'\-]{1,80})",
            r"\bname\s+is\s+([A-Za-z][A-Za-z .'\-]{1,80})",
        ]
        for pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                return self._clean_candidate_name(match.group(1))

        fallback = self._clean_candidate_name(user_message)
        if self._looks_like_candidate_name(fallback):
            return fallback
        return None

    def _clean_candidate_name(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip(" \t\n\r.,;:!?")

    def _looks_like_candidate_name(self, value: str) -> bool:
        if not value or any(char.isdigit() for char in value):
            return False
        tokens = [token for token in re.split(r"\s+", value) if token]
        if not 2 <= len(tokens) <= 6:
            return False
        if any(token.lower() in _NON_NAME_TOKENS for token in tokens):
            return False
        return all(re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", token) for token in tokens)

    def _extract_candidate_account_number_from_message(
        self, user_message: str
    ) -> Optional[str]:
        exact_match = re.search(r"\b(\d{10})\b", user_message)
        if exact_match:
            return exact_match.group(1)

        digits_only = re.sub(r"\D", "", user_message)
        if len(digits_only) == 10:
            return digits_only
        return None

    _INSTITUTION_MSG_PREFIXES = re.compile(
        r"^(?:i\s+said\s+|just\s+|please\s+|i\s+want(?:\s+to)?\s+|i\s+mean\s+|"
        r"use\s+|try\s+|select\s+|pick\s+|choose\s+|i\s+said\s+use\s+|"
        r"just\s+use\s+|please\s+use\s+)",
        re.IGNORECASE,
    )

    async def _extract_candidate_institution_from_message(
        self, user_message: str, db_session=None
    ) -> Optional[str]:
        if db_session is None:
            return None

        try:
            from src.infrastructure.database.repositories.institution_repository import (
                InstitutionRepository,
            )

            repo = InstitutionRepository(db_session)
            institutions = await repo.get_all_active_rows()

            phrases: list[str] = []

            msg = user_message.strip()
            cleaned = msg
            for _ in range(3):
                prev = cleaned
                cleaned = self._INSTITUTION_MSG_PREFIXES.sub("", cleaned).strip()
                if cleaned == prev:
                    break
            if cleaned and cleaned != msg:
                phrases.append(cleaned)

            for pattern in (
                r"(?:bank|institution)(?:\s+is)?\s+([A-Za-z][A-Za-z .&'\-]{1,80})",
                r"(?:at|with|from)\s+([A-Za-z][A-Za-z .&'\-]{1,80})",
            ):
                match = re.search(pattern, msg, re.IGNORECASE)
                if match:
                    phrases.append(match.group(1).strip())

            phrases.append(msg)

            for phrase in phrases:
                resolved = _resolve_institution_code(phrase, institutions)
                if resolved:
                    return resolved
        except Exception as exc:
            logger.debug(f"Failed to resolve institution from chat message: {exc}")

        return None

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

    def _build_slot_preamble(self, slots: dict) -> str:
        """Convert raw slot dict into a human-readable summary the LLM can't ignore."""
        if not slots:
            return "Nothing gathered yet."

        labels = {
            "objective": "Objective",
            "budget_cap": "Budget Cap",
            "risk_tolerance": "Risk Tolerance",
            "date_from": "Start Date",
            "date_to": "End Date",
            "candidates": "Candidates",
        }
        lines = []
        for key, label in labels.items():
            val = slots.get(key)
            if val is not None and val != "" and val != []:
                if key == "candidates" and isinstance(val, list):
                    lines.append(f"- {label}: {len(val)} beneficiaries provided")
                elif key == "budget_cap":
                    try:
                        lines.append(f"- {label}: {float(val):,.0f}")
                    except (ValueError, TypeError):
                        lines.append(f"- {label}: {val}")
                else:
                    lines.append(f"- {label}: {val}")

        # Include any extra keys not in the standard labels
        for key, val in slots.items():
            if key not in labels and val is not None and val != "":
                lines.append(f"- {key.replace('_', ' ').title()}: {val}")

        if not lines:
            return "Nothing gathered yet."
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
        if slots.get("budget_cap") is not None:
            config["budget_cap"] = float(slots["budget_cap"])
        if slots.get("constraints"):
            config["constraints"] = slots["constraints"]
        if slots.get("candidates"):
            config["candidates"] = slots["candidates"]
        return config

    def normalize_slots_for_confirmation(self, slots: Optional[dict]) -> dict:
        return self._coalesce_legacy_candidate_fields(slots or {})

    def can_confirm_slots(self, slots: Optional[dict]) -> bool:
        normalized = self.normalize_slots_for_confirmation(slots)
        return self._has_sufficient_slots(normalized)

    def build_confirmation_prompt(self, slots: dict) -> str:
        objective = str(slots.get("objective", "") or "this payout run")
        date_from = str(slots.get("date_from", "") or "")
        date_to = str(slots.get("date_to", "") or "")
        risk = slots.get("risk_tolerance")
        candidates = slots.get("candidates") if isinstance(slots.get("candidates"), list) else []
        beneficiary_count = len(candidates)

        first_name = ""
        first_amount = None
        if candidates and isinstance(candidates[0], dict):
            first_name = str(candidates[0].get("beneficiary_name", "") or "")
            first_amount = candidates[0].get("amount")

        summary_parts = [objective]
        if date_from and date_to:
            summary_parts.append(f"{date_from} to {date_to}")
        if risk is not None:
            summary_parts.append(f"risk tolerance {risk}")

        summary = ", ".join(summary_parts)
        if beneficiary_count == 1 and first_name:
            amount_text = ""
            try:
                if first_amount is not None:
                    amount_text = f" for {float(first_amount):,.0f} NGN"
            except (TypeError, ValueError):
                amount_text = ""
            return (
                f"I've got everything I need for {summary}. "
                f"The beneficiary is {first_name}{amount_text}. Should I go ahead?"
            )

        return (
            f"I've got everything I need for {summary}, with {beneficiary_count} "
            f"beneficiar{'y' if beneficiary_count == 1 else 'ies'}. Should I go ahead?"
        )
