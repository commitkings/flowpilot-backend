"""
Intent Classification Service — World-Class Multi-Layered Classification.

Three-tier classification pipeline:
  Tier 1 (regex, 0ms)  → Greetings, farewells, acknowledgements
  Tier 2 (keyword, 0ms) → Pre-filter skips LLM for non-financial messages
  Tier 3 (LLM, ~200ms) → Few-shot prompt with confidence gating

Inspired by proven production patterns. Designed for:
- Sub-100ms classification for 60%+ of messages (Tier 1-2)
- High accuracy on financial intents via exhaustive few-shot examples
- Graceful degradation: LLM failure → 'unclear' (never crashes)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from groq import AsyncGroq

from src.config.settings import Settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Intent Taxonomy
# ─────────────────────────────────────────────────────────────────────

class FlowPilotIntent(str, Enum):
    """All recognized user intents for FlowPilot."""

    CREATE_PAYOUT_RUN = "create_payout_run"
    CHECK_RUN_STATUS = "check_run_status"
    REVIEW_CANDIDATES = "review_candidates"
    APPROVE_REJECT = "approve_reject"
    EXPLAIN_SYSTEM = "explain_system"
    VIEW_AUDIT = "view_audit"
    MODIFY_CONFIG = "modify_config"
    GREETING = "greeting"
    FAREWELL = "farewell"
    ACKNOWLEDGEMENT = "acknowledgement"
    UNCLEAR = "unclear"


# Backward-compatible mapping to the legacy intent strings used by IntentAgent
_INTENT_TO_LEGACY: dict[FlowPilotIntent, str] = {
    FlowPilotIntent.CREATE_PAYOUT_RUN: "create_payout_run",
    FlowPilotIntent.CHECK_RUN_STATUS: "check_run_status",
    FlowPilotIntent.REVIEW_CANDIDATES: "check_run_status",   # routes to status for now
    FlowPilotIntent.APPROVE_REJECT: "create_payout_run",     # approval context
    FlowPilotIntent.EXPLAIN_SYSTEM: "explain_system",
    FlowPilotIntent.VIEW_AUDIT: "check_run_status",          # status-adjacent
    FlowPilotIntent.MODIFY_CONFIG: "modify_config",
    FlowPilotIntent.GREETING: "greeting",
    FlowPilotIntent.FAREWELL: "farewell",
    FlowPilotIntent.ACKNOWLEDGEMENT: "greeting",             # respond warmly
    FlowPilotIntent.UNCLEAR: "unclear",
}


@dataclass
class IntentResult:
    """Result of intent classification."""

    intent: FlowPilotIntent
    confidence: float
    reasoning: str = ""
    tier: int = 0          # 1 = regex, 2 = keyword-filter, 3 = LLM
    raw_response: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        """High-confidence intent that can drive a flow."""
        return self.intent != FlowPilotIntent.UNCLEAR and self.confidence >= 0.75

    @property
    def legacy_intent(self) -> str:
        """Map to the original 7-intent strings for backward compatibility."""
        return _INTENT_TO_LEGACY.get(self.intent, "unclear")


# ─────────────────────────────────────────────────────────────────────
# Tier 1: Deterministic Fast-Path Patterns
# ─────────────────────────────────────────────────────────────────────

_GREETING_PATTERNS: set[str] = {
    "hi", "hello", "hey", "howdy", "greetings", "yo", "sup", "wassup",
    "good morning", "good afternoon", "good evening", "good night",
    "hi there", "hey there", "hello there", "morning", "afternoon",
    "how are you", "how you dey", "how far", "e kaaro", "e kaasan",
    "sannu", "nnoo", "kedu", "whats up", "hiya",
    "hey dude", "hey man", "hey bro", "hey fam", "hey boss",
    "hi dude", "hi man", "hi bro", "hi fam", "hi boss",
    "hello there friend", "hello fam",
    "how are you doing", "how you doing", "hows it going", "how is it going",
    "how goes it", "how do you do",
}

_GREETING_PREFIXES: tuple[str, ...] = (
    "hey ",
    "hi ",
    "hello ",
    "howdy ",
    "good morning",
    "good afternoon",
    "good evening",
    "how are you",
    "how you doing",
    "how you dey",
    "hows it going",
    "how is it going",
    "whats up",
    "yo ",
    "sup ",
    "hiya ",
)

_FAREWELL_PATTERNS: set[str] = {
    "bye", "goodbye", "good bye", "see you", "later", "gotta go",
    "take care", "ciao", "cheers", "peace", "adios", "see ya",
    "talk later", "catch you later", "i'm done", "that's all",
    "thanks bye", "thank you bye", "bye bye", "ok bye",
}

_ACKNOWLEDGEMENT_PATTERNS: set[str] = {
    # English
    "ok", "okay", "alright", "got it", "i see", "hmm", "hmm i see",
    "thanks", "thank you", "cool", "nice", "great", "good", "understood",
    "makes sense", "i understand", "noted", "sure", "right", "yes", "yep",
    "ah", "ah ok", "ah i see", "oh", "oh ok", "oh i see", "interesting",
    "perfect", "exactly", "indeed", "true", "fair enough", "yeah", "yea",
    "fine", "wonderful", "awesome", "incredible", "brilliant",
    # Nigerian Pidgin
    "oya", "oya nah", "na so", "e clear", "i don hear", "i understand am",
    "e good", "na true", "correct", "sharp", "sharp sharp", "no wahala",
    "okay na", "alright na", "na him be dat",
}

# First-word triggers for short acknowledgement detection (≤4 words)
_ACK_FIRST_WORDS: set[str] = {
    "ok", "okay", "hmm", "ah", "oh", "thanks", "cool", "nice", "great",
    "good", "yes", "yep", "right", "oya", "sharp", "correct", "noted",
    "alright", "sure", "perfect", "exactly", "indeed", "true", "fine",
    "awesome", "brilliant", "wonderful", "yeah", "yea", "fair",
}


def _normalize(text: str) -> str:
    """Strip punctuation and normalize whitespace."""
    return re.sub(r"[!?.,;:\-\"']+", "", text.lower()).strip()


def _tier1_classify(message: str) -> Optional[IntentResult]:
    """Tier 1: Deterministic regex/set matching. Returns None if no match."""
    clean = _normalize(message)
    if not clean:
        return IntentResult(
            intent=FlowPilotIntent.UNCLEAR,
            confidence=0.5,
            reasoning="Empty message",
            tier=1,
        )

    # Exact match checks
    if clean in _GREETING_PATTERNS:
        return IntentResult(
            intent=FlowPilotIntent.GREETING,
            confidence=0.95,
            reasoning=f"Matched greeting pattern: '{clean}'",
            tier=1,
        )

    # Prefix-based greeting detection (catches "hey dude", "how you doing, it's weekend...", etc.)
    if any(clean.startswith(prefix) for prefix in _GREETING_PREFIXES):
        return IntentResult(
            intent=FlowPilotIntent.GREETING,
            confidence=0.90,
            reasoning=f"Greeting prefix detected in: '{clean[:40]}'",
            tier=1,
        )

    if clean in _FAREWELL_PATTERNS:
        return IntentResult(
            intent=FlowPilotIntent.FAREWELL,
            confidence=0.95,
            reasoning=f"Matched farewell pattern: '{clean}'",
            tier=1,
        )

    if clean in _ACKNOWLEDGEMENT_PATTERNS:
        return IntentResult(
            intent=FlowPilotIntent.ACKNOWLEDGEMENT,
            confidence=0.95,
            reasoning=f"Matched acknowledgement pattern: '{clean}'",
            tier=1,
        )

    # Short message + first word match → acknowledgement
    words = clean.split()
    if len(words) <= 4 and words and words[0] in _ACK_FIRST_WORDS:
        return IntentResult(
            intent=FlowPilotIntent.ACKNOWLEDGEMENT,
            confidence=0.85,
            reasoning=f"Short message starting with acknowledgement word: '{words[0]}'",
            tier=1,
        )

    return None


# ─────────────────────────────────────────────────────────────────
# Tier 0: Flow-Aware Slot-Fill Detection (bypass LLM entirely)
# ─────────────────────────────────────────────────────────────────

# Date-like pattern: YYYY-MM-DD, DD/MM/YYYY, or natural like "March 2025"
# Strict: only date string, used for pure date-only messages
_DATE_PATTERN = re.compile(
    r"^\s*(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?"
    r")\s*$",
    re.IGNORECASE,
)

# Date-in-context: message contains a date even with surrounding words
# Matches "use April 1st 2026", "maybe April 30th sha", "let's do 01/04/2026"
_DATE_IN_CONTEXT_PATTERN = re.compile(
    r"(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)(?:\s*,?\s*\d{4})?"
    r"|\d{1,2}(?:st|nd|rd|th)\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?"
    r")",
    re.IGNORECASE,
)

# Amount-like pattern: "500k", "1.5m", "₦200,000", "50000", "naira 500000"
_AMOUNT_PATTERN = re.compile(
    r"^\s*(?:₦|ngn|naira)?\s*\d[\d,]*(?:\.\d+)?\s*[kKmMbB]?\s*$",
    re.IGNORECASE,
)

# Name-like pattern: 2-5 capitalized words ("MICHAEL JOHN DOE", "Jane Doe")
_NAME_PATTERN = re.compile(
    r"^\s*[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){1,5}\s*$"
)

# Bank/institution mention
_BANK_KEYWORDS = {
    "bank", "gtbank", "gtb", "access", "zenith", "uba", "firstbank",
    "first bank", "fidelity", "sterling", "wema", "stanbic", "polaris",
    "keystone", "fcmb", "union", "ecobank", "heritage", "jaiz",
    "providus", "titan", "globus", "suntrust", "citi", "standard",
    "guaranty", "guarantee",
}

# Risk tolerance pattern: "0.35", "use 0.45", "set it to 0.5"
_RISK_TOLERANCE_PATTERN = re.compile(
    r"^\s*(?:use\s+|set\s+(?:it\s+)?(?:to\s+)?|try\s+)?(?:0\.\d+|1\.0|0|1)\s*$",
    re.IGNORECASE,
)

# Account number pattern: exactly 10 digits possibly with leading text
_ACCOUNT_NUMBER_PATTERN = re.compile(
    r"(?:^|\s)\d{10}(?:\s|$)"
)

# "No budget cap" or similar negation
_NO_BUDGET_PATTERN = re.compile(
    r"\b(?:no|none|skip|without|don'?t\s+(?:need|want|set))\b.*\b(?:budget|cap|limit)\b",
    re.IGNORECASE,
)

# Explicit flow-breaking intents that should NOT be caught by Tier 0
_FLOW_BREAK_PATTERNS = re.compile(
    r"\b(?:status|how\s+does|what\s+is|explain|cancel|stop|nevermind|never\s+mind)\b",
    re.IGNORECASE,
)

# Mid–payout-run corrections (Tier 0b) — avoids Tier 2 "no keywords" dead-end
_PAYOUT_FLOW_CORRECTION_PATTERN = re.compile(
    r"\b(?:sorry|actually|i\s+meant|my\s+bad|correction|correct\s+that|"
    r"wrong\s+name|change\s+(?:it\s+)?to|spell(?:ing)?|the\s+name\s+should\s+be)\b",
    re.IGNORECASE,
)


def _tier0_slot_fill_detect(message: str) -> Optional[IntentResult]:
    """
    Tier 0: When the user is in an active payout flow, detect obvious slot-fill
    messages and bypass classification entirely.

    Returns IntentResult(CREATE_PAYOUT_RUN, tier=0) if the message looks like
    a slot fill, None otherwise.
    """
    stripped = message.strip()
    if not stripped:
        return None

    # Don't intercept flow-breaking messages
    if _FLOW_BREAK_PATTERNS.search(stripped):
        return None

    # Date fill
    if _DATE_PATTERN.match(stripped):
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.95,
            reasoning=f"Tier 0: Date slot fill detected: '{stripped[:30]}'",
            tier=0,
        )

    # Date-in-context fill ("use April 1st 2026", "maybe April 30th sha")
    if _DATE_IN_CONTEXT_PATTERN.search(stripped):
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.90,
            reasoning=f"Tier 0: Date slot fill detected in context: '{stripped[:40]}'",
            tier=0,
        )

    # Amount fill
    if _AMOUNT_PATTERN.match(stripped):
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.95,
            reasoning=f"Tier 0: Amount slot fill detected: '{stripped[:30]}'",
            tier=0,
        )

    # Risk tolerance fill
    if _RISK_TOLERANCE_PATTERN.match(stripped):
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.90,
            reasoning=f"Tier 0: Risk tolerance slot fill: '{stripped[:30]}'",
            tier=0,
        )

    # Account number fill (10-digit number in message)
    if _ACCOUNT_NUMBER_PATTERN.search(stripped) and len(stripped) < 60:
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.90,
            reasoning=f"Tier 0: Account number fill detected: '{stripped[:30]}'",
            tier=0,
        )

    # "No budget cap" or similar
    if _NO_BUDGET_PATTERN.search(stripped):
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.90,
            reasoning=f"Tier 0: Budget cap negation: '{stripped[:30]}'",
            tier=0,
        )

    # Bank/institution mention ("use gt bank", "access bank", etc.)
    normalized_words = set(_normalize(stripped).split())
    if normalized_words & _BANK_KEYWORDS and len(normalized_words) <= 6:
        return IntentResult(
            intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
            confidence=0.90,
            reasoning=f"Tier 0: Bank/institution slot fill: '{stripped[:30]}'",
            tier=0,
        )

    # Name-like pattern ("MICHAEL JOHN DOE", "Jane Doe") — only if short
    if _NAME_PATTERN.match(stripped) and len(stripped) < 80:
        # Additional check: not a common phrase or greeting
        lower = stripped.lower()
        if lower not in _GREETING_PATTERNS and lower not in _FAREWELL_PATTERNS:
            return IntentResult(
                intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
                confidence=0.85,
                reasoning=f"Tier 0: Name slot fill detected: '{stripped[:30]}'",
                tier=0,
            )

    # Short objective-like text (1-6 words with financial keywords)
    words = stripped.split()
    if 1 <= len(words) <= 6:
        lower_words = {w.lower() for w in words}
        objective_keywords = {
            "salary", "salaries", "payroll", "vendor", "vendors",
            "contractor", "contractors", "payment", "payments",
            "settlement", "settlements", "commission", "commissions",
            "bonus", "bonuses", "stipend", "stipends", "wages",
            "reimbursement", "disbursement",
        }
        if lower_words & objective_keywords:
            return IntentResult(
                intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
                confidence=0.85,
                reasoning=f"Tier 0: Objective slot fill: '{stripped[:30]}'",
                tier=0,
            )

    return None


# ─────────────────────────────────────────────────────────────────────
# Tier 2: Keyword Pre-Filter (skip LLM for non-financial messages)
# ─────────────────────────────────────────────────────────────────────

_FINANCIAL_KEYWORDS: set[str] = {
    # Payout/payment
    "pay", "payout", "payouts", "payment", "salary", "salaries",
    "disburse", "disbursement", "transfer", "remit", "remittance",
    "vendor", "beneficiary", "beneficiaries", "candidate", "candidates",
    "execute", "settlement", "commission", "bonus", "wage", "wages",
    "invoice", "refund", "reimbursement", "stipend",
    # Run management
    "run", "runs", "pipeline", "status", "approve", "reject", "approval",
    "review", "score", "risk", "audit", "reconcile", "reconciliation",
    "execute", "execution", "batch",
    # System
    "flowpilot", "flow pilot", "how does", "what is", "explain", "help",
    "configure", "config", "setting", "settings", "threshold", "tolerance",
    "budget", "cap", "limit", "merchant",
    # Financial
    "account", "bank", "institution", "interswitch", "transaction",
    "transactions", "amount", "naira", "ngn", "balance", "wallet",
    "ledger", "anomaly", "fraud", "duplicate",
    # Date/time words (so date replies don't get rejected by T2)
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "date", "start", "end", "from", "to", "period", "month", "year",
    # Confirmation/flow words
    "default", "proceed", "yes", "no", "none", "skip",
}


def _tier2_has_financial_keywords(message: str) -> bool:
    """Check if message contains any financial/system keywords."""
    words = set(_normalize(message).split())
    return bool(words & _FINANCIAL_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────
# Tier 3: LLM Classification with Few-Shot Examples
# ─────────────────────────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """You are the intent classifier for FlowPilot, a multi-agent fintech payout automation platform built on Interswitch APIs.

Given a user message and optional conversation history, classify into EXACTLY ONE intent.

## Intent Definitions and Examples

### create_payout_run — User wants to create, set up, or configure a NEW payout run
✅ "Pay March salaries for the engineering team"
✅ "I need to disburse funds to 50 vendors"
✅ "Set up a payout run with a budget cap of 5 million naira"
✅ "Send money to these beneficiaries"
✅ "Process payroll for February"
✅ "I want to make some payments"
✅ "Create a new run for vendor settlements"
✅ "Pay 500,000 to account 0123456789 at GTBank"
❌ "How do payouts work?" → explain_system
❌ "What happened with the last payment run?" → check_run_status

### check_run_status — User is asking about an EXISTING run's progress or results
✅ "What's the status of my last run?"
✅ "Did the payout complete?"
✅ "How many candidates passed risk scoring?"
✅ "Show me the results of run #7"
✅ "Is the pipeline still running?"
✅ "What happened with the last payment run?"
❌ "Set up a new run" → create_payout_run

### review_candidates — User wants to SEE or EXAMINE payout candidates
✅ "Show me the flagged candidates"
✅ "Who are the high-risk beneficiaries?"
✅ "List all candidates pending approval"
✅ "Which candidates were blocked?"
✅ "What candidates are in this batch?"
❌ "Approve all candidates" → approve_reject

### approve_reject — User wants to APPROVE or REJECT candidates
✅ "Approve all low-risk candidates"
✅ "Reject the flagged ones"
✅ "I approve candidate #3 and #5"
✅ "Block the suspicious transactions"
✅ "Go ahead and approve them"
✅ "Don't process candidate 4"
❌ "Show me who's pending" → review_candidates

### explain_system — User wants to understand HOW FlowPilot works
✅ "How does the risk scoring work?"
✅ "What is the pipeline?"
✅ "Explain the reconciliation process"
✅ "What happens after approval?"
✅ "Tell me about the audit agent"
✅ "How do payouts work?"
✅ "What agents are in the system?"
❌ "Set up a payout" → create_payout_run

### view_audit — User wants to see a PAST audit report or analysis
✅ "Show me the audit report"
✅ "What did the audit find?"
✅ "Were there any compliance issues?"
✅ "Show me the cost analysis from the last run"
✅ "What were the recommendations?"

### modify_config — User wants to CHANGE settings, thresholds, or business configuration
✅ "Set my risk tolerance to 0.5"
✅ "Change the budget cap to 10 million"
✅ "Update the default merchant ID"
✅ "Lower the risk threshold"
❌ "What is risk tolerance?" → explain_system

### greeting — User is saying hello or making small talk
✅ "Hey", "Hello", "Good morning", "How are you?"

### farewell — User is ending the conversation
✅ "Bye", "Thanks, goodbye", "See you later"

### acknowledgement — Short follow-up acknowledging previous response (1-4 words)
✅ "Got it", "OK", "Makes sense", "Thanks", "I see"

### unclear — Cannot determine intent, message is ambiguous or off-topic
✅ Random gibberish, unrelated questions, very ambiguous input

## Key Disambiguation Rules
1. "pay" / "send money" / "disburse" → create_payout_run (action to CREATE)
2. "how does X work?" / "what is X?" / "explain X" → explain_system (learning)
3. "show me" / "list" / "who are" + candidates → review_candidates
4. "approve" / "reject" / "block" + candidates → approve_reject
5. "status" / "progress" / "results" / "what happened" → check_run_status
6. "audit" / "report" / "compliance" / "recommendations" → view_audit
7. Short (≤4 words) positive response after AI reply → acknowledgement

## Context
Conversation history:
{history}

User message: "{message}"

Respond with ONLY valid JSON:
{{"intent": "<intent_name>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}}"""


# ─────────────────────────────────────────────────────────────────────
# Intent Service
# ─────────────────────────────────────────────────────────────────────

class IntentService:
    """
    World-class multi-layered intent classification for FlowPilot.

    Usage::

        service = IntentService()
        result = await service.classify("Pay March salaries", history=[...])
        print(result.intent, result.confidence, result.tier)
    """

    CONFIDENCE_THRESHOLD = 0.75

    def __init__(self) -> None:
        self._client: Optional[AsyncGroq] = None

    def _get_client(self) -> AsyncGroq:
        if self._client is None:
            self._client = AsyncGroq(api_key=Settings.GROQ_API_KEY)
        return self._client

    async def classify(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        active_intent: Optional[str] = None,
    ) -> IntentResult:
        """
        Classify user intent through the 4-tier pipeline.

        Args:
            message: The user's raw message text.
            history: Optional conversation history (list of {role, content} dicts).
            active_intent: Current active intent from the chat session (e.g. "create_payout_run").
                          When set, Tier 0 can bypass classification for obvious slot fills.

        Returns:
            IntentResult with intent, confidence, tier, and reasoning.
        """
        # ── Tier 0: Flow-aware slot-fill detection (bypass everything) ──
        if active_intent == "create_payout_run":
            tier0 = _tier0_slot_fill_detect(message)
            if tier0 is not None:
                logger.info(
                    f"Intent [T0]: {tier0.intent.value} "
                    f"(conf={tier0.confidence:.2f}) for: '{message[:50]}'"
                )
                return tier0
            stripped = message.strip()
            if (
                stripped
                and len(stripped) <= 160
                and _PAYOUT_FLOW_CORRECTION_PATTERN.search(stripped)
            ):
                correction = IntentResult(
                    intent=FlowPilotIntent.CREATE_PAYOUT_RUN,
                    confidence=0.88,
                    reasoning="Tier 0b: payout-flow clarification or name correction",
                    tier=0,
                )
                logger.info(
                    f"Intent [T0b]: {correction.intent.value} "
                    f"(conf={correction.confidence:.2f}) for: '{message[:50]}'"
                )
                return correction

        # ── Tier 1: Deterministic fast-path ──
        tier1 = _tier1_classify(message)
        if tier1 is not None:
            logger.info(
                f"Intent [T1]: {tier1.intent.value} "
                f"(conf={tier1.confidence:.2f}) for: '{message[:50]}'"
            )
            return tier1

        # ── Tier 2: Keyword pre-filter ──
        if not _tier2_has_financial_keywords(message):
            result = IntentResult(
                intent=FlowPilotIntent.UNCLEAR,
                confidence=0.6,
                reasoning="No financial/system keywords detected — skipping LLM",
                tier=2,
            )
            logger.info(
                f"Intent [T2]: unclear (no keywords) for: '{message[:50]}'"
            )
            return result

        # ── Tier 3: LLM classification ──
        return await self._llm_classify(message, history or [])

    async def _llm_classify(
        self,
        message: str,
        history: list[dict],
    ) -> IntentResult:
        """Tier 3: LLM-based classification with few-shot prompt."""
        history_text = self._format_history(history, max_turns=6)

        prompt = CLASSIFICATION_PROMPT.format(
            history=history_text,
            message=message,
        )

        default = IntentResult(
            intent=FlowPilotIntent.UNCLEAR,
            confidence=0.5,
            reasoning="LLM classification failed — fallback",
            tier=3,
        )

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=80,
            )

            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                logger.warning(f"Intent [T3]: empty LLM response for '{message[:40]}'")
                return default

            result = self._parse_llm_response(raw)
            logger.info(
                f"Intent [T3]: {result.intent.value} "
                f"(conf={result.confidence:.2f}) for: '{message[:50]}'"
            )
            return result

        except Exception as e:
            logger.warning(f"Intent [T3] LLM call failed: {e}")
            return default

    def _parse_llm_response(self, raw: str) -> IntentResult:
        """Parse the LLM JSON response into an IntentResult."""
        try:
            # Strip markdown code fences if present
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
                clean = clean.strip()

            data = json.loads(clean)

            intent_str = data.get("intent", "unclear").lower()
            confidence = float(data.get("confidence", 0.5))
            reasoning = data.get("reasoning", "")

            # Map string to enum
            try:
                intent = FlowPilotIntent(intent_str)
            except ValueError:
                logger.warning(f"Unknown intent from LLM: '{intent_str}'")
                intent = FlowPilotIntent.UNCLEAR
                confidence = min(confidence, 0.5)

            # Apply confidence gating
            if confidence < self.CONFIDENCE_THRESHOLD:
                logger.debug(
                    f"Intent confidence {confidence:.2f} below threshold "
                    f"{self.CONFIDENCE_THRESHOLD} — downgrading to unclear"
                )
                intent = FlowPilotIntent.UNCLEAR

            return IntentResult(
                intent=intent,
                confidence=confidence,
                reasoning=reasoning,
                tier=3,
                raw_response=raw,
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse LLM intent response: {e}, raw: {raw[:200]}")
            return IntentResult(
                intent=FlowPilotIntent.UNCLEAR,
                confidence=0.5,
                reasoning=f"Parse error: {e}",
                tier=3,
                raw_response=raw,
            )

    @staticmethod
    def _format_history(history: list[dict], max_turns: int = 6) -> str:
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
