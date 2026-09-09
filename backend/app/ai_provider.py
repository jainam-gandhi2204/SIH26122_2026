"""AI provider interface, mock implementation, and Gemini implementation.

Architecture
------------
AIProvider is an abstract interface.  Any real model (Gemini, GPT-4, etc.)
can be plugged in later by subclassing AIProvider and implementing `analyse`.

MockAIProvider is the deterministic implementation used when no real API key
is configured.  It uses simple keyword heuristics applied to the raw_update
text.  It never hallucinates: values it cannot determine are left as None
(UNKNOWN).

GeminiAIProvider calls the Gemini API via google-genai and requests
structured JSON output.  It reads GEMINI_API_KEY from the environment and
falls back to MockAIProvider behaviour if the model returns unparseable data.

Usage
-----
Call `get_provider()` to obtain the configured provider.  It reads
AI_PROVIDER from the environment (default: "mock").
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class AnalysisResult:
    """All fields that map directly to the ai_processed_updates table columns.

    None means UNKNOWN – value not stated in or safely inferable from the text.
    """
    matched_task_id: str | None          # UUID of schedule_tasks row, or None
    progress_percent: float | None       # 0–100; None = UNKNOWN
    status: str | None                   # one of the allowed DB enum values; None = UNKNOWN
    delay_days: int | None               # non-negative; None = UNKNOWN
    delay_reason: str | None             # free text; None = UNKNOWN
    actual_start_date: str | None        # ISO date string; None = UNKNOWN
    actual_end_date: str | None          # ISO date string; None = UNKNOWN
    confidence_score: float              # 0–100
    model_name: str
    model_response: dict[str, Any]       # full model output for audit trail


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class AIProvider(ABC):
    """Abstract AI provider.  Subclass this to add a real model backend."""

    @abstractmethod
    def analyse(
        self,
        raw_update: str,
        location: str,
        reported_on: str,
        candidate_tasks: list[dict[str, Any]],
    ) -> AnalysisResult:
        """Analyse a site update and return a structured result.

        Parameters
        ----------
        raw_update:       The raw text of the site update.
        location:         Location reported in the site update.
        reported_on:      ISO date string (YYYY-MM-DD) of the update.
        candidate_tasks:  List of schedule task dicts with keys:
                          id, source_task_id, activity, location,
                          planned_start (ISO str), planned_end (ISO str).

        Returns
        -------
        AnalysisResult with only FACT or INFERENCE values populated.
        Anything that cannot be determined is left as None.
        """


# ---------------------------------------------------------------------------
# Mock / deterministic provider
# ---------------------------------------------------------------------------

_STATUS_KEYWORDS: dict[str, str] = {
    "complet": "completed",
    "finish": "completed",
    "done": "completed",
    "in progress": "in_progress",
    "ongoing": "in_progress",
    "started": "in_progress",
    "underway": "in_progress",
    "delay": "delayed",
    "behind": "delayed",
    "not start": "not_started",
    "yet to start": "not_started",
    "block": "blocked",
    "halt": "blocked",
    "stopp": "blocked",
}

_PROGRESS_PATTERNS: list[tuple[re.Pattern[str], float | None]] = [
    (re.compile(r"(\d+)\s*%\s*(?:complete|done|finish|progress)", re.I), None),
    (re.compile(r"(?:complete|done|finish)[^\d]*(\d+)\s*%", re.I), None),
]

_MOCK_MODEL_NAME = "mock-keyword-v1"

_MONTHS_PATTERN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

_DATE_TOKEN_PATTERN = (
    r"(?:"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?[-/\s]+{_MONTHS_PATTERN}[-/\s,]+\d{{4}}"
    rf"|{_MONTHS_PATTERN}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    r")"
)

_START_DATE_RE = re.compile(
    rf"(?<!planned\s)(?<!target\s)\b(?:started|commenced|began|start\s*date|commencement\s*date|actual\s*start(?:\s*date)?)\b\s*(?:on|:|at|-|\bas of\b)?\s*(?P<date>{_DATE_TOKEN_PATTERN})",
    re.IGNORECASE,
)

_END_DATE_RE = re.compile(
    rf"(?<!planned\s)(?<!target\s)\b(?:completed|finished|ended|completion\s*date|end\s*date|actual\s*end(?:\s*date)?)\b\s*(?:on|:|at|-|\bas of\b)?\s*(?P<date>{_DATE_TOKEN_PATTERN})",
    re.IGNORECASE,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DATE_PARSE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b, %Y",
    "%d %B, %Y",
)


def _parse_explicit_date(date_str: str) -> str | None:
    """Parse an explicitly stated date string into ISO YYYY-MM-DD format.

    Returns None if the date cannot be parsed or represents an invalid
    calendar date (e.g. Feb 31).
    """
    if not date_str:
        return None
    cleaned = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", date_str.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if _ISO_DATE_RE.match(cleaned):
        try:
            return datetime.date.fromisoformat(cleaned).isoformat()
        except ValueError:
            return None

    for fmt in _DATE_PARSE_FORMATS:
        try:
            dt = datetime.datetime.strptime(cleaned, fmt)
            return dt.date().isoformat()
        except ValueError:
            continue
    return None


def _extract_start_date(text: str) -> str | None:
    """Extract actual start date from text if explicitly stated, else None."""
    matches = list(_START_DATE_RE.finditer(text))
    if not matches:
        return None
    for m in matches:
        if "actual" in m.group(0).lower():
            parsed = _parse_explicit_date(m.group("date"))
            if parsed:
                return parsed
    for m in matches:
        parsed = _parse_explicit_date(m.group("date"))
        if parsed:
            return parsed
    return None


def _extract_end_date(text: str) -> str | None:
    """Extract actual completion / end date from text if explicitly stated, else None."""
    matches = list(_END_DATE_RE.finditer(text))
    if not matches:
        return None
    for m in matches:
        if "actual" in m.group(0).lower():
            parsed = _parse_explicit_date(m.group("date"))
            if parsed:
                return parsed
    for m in matches:
        parsed = _parse_explicit_date(m.group("date"))
        if parsed:
            return parsed
    return None


# ---------------------------------------------------------------------------
# Number words & delay extraction helpers
# ---------------------------------------------------------------------------

_WORD_TO_NUM: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "twenty": 20, "thirty": 30,
}


def _extract_delay_days(text: str) -> int | None:
    """Extract delay magnitude in days from text.

    Supports digits ('3 days delay', '2-day delay') and word numbers
    ('two-day delay', 'three days behind').
    """
    # 1. Matches: "2-day delay", "two-day delay", "three days behind", "1 day delay"
    pattern1 = re.search(
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fourteen)[-\s]+day[s]?(?:\s+(?:delay|behind|late|overdue|slip))?\b",
        text,
        re.I,
    )
    if pattern1:
        raw_val = pattern1.group(1).lower()
        if raw_val.isdigit():
            return int(raw_val)
        if raw_val in _WORD_TO_NUM:
            return _WORD_TO_NUM[raw_val]

    # 2. Matches: "delayed by 2 days", "delayed by two days", "slip of 3 days"
    pattern2 = re.search(
        r"\b(?:delay(?:ed)?\s*(?:by|of)?|behind\s*(?:by)?|late\s*(?:by)?|slip\s*(?:of)?)\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fourteen)\s*day[s]?\b",
        text,
        re.I,
    )
    if pattern2:
        raw_val = pattern2.group(1).lower()
        if raw_val.isdigit():
            return int(raw_val)
        if raw_val in _WORD_TO_NUM:
            return _WORD_TO_NUM[raw_val]

    # 3. Matches: "delayed by 2"
    pattern3 = re.search(r"\bdelayed\s+by\s+(\d+)\b", text, re.I)
    if pattern3:
        return int(pattern3.group(1))

    return None


# ---------------------------------------------------------------------------
# Activity matching, normalization, and confidence scoring
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "the", "at", "is", "of", "and", "in", "to", "for", "with", "a", "an",
    "on", "by", "from", "as", "about", "around", "approximately", "roughly",
    "complete", "completed", "progress", "due", "site", "pad", "work"
})

_ACTIVITY_SYNONYMS: dict[str, list[str]] = {
    "excavat": ["excavation", "excavating", "excavated", "excavate", "earthwork", "digging", "trenched", "trenching"],
    "clear": ["clearing", "clearance", "cleared", "site prep", "site preparation", "grubbing", "grading"],
    "cast": ["casting", "cast", "concreting", "concrete", "pour", "pouring", "poured"],
    "foundat": ["foundation", "foundations", "substructure", "footing", "footings"],
    "install": ["installation", "install", "installed", "installing", "erection", "erecting", "mount", "mounting"],
    "equip": ["equipment", "machinery", "skid", "generator", "pump", "compressor", "vessel"],
    "pipe": ["piping", "pipeline", "pipe", "pipe laying", "pipework", "flowline"],
    "weld": ["welding", "weld", "welded", "tie-in", "joint", "jointing"],
    "commiss": ["commissioning", "commission", "commissioned", "pre-commissioning", "handover"],
    "test": ["testing", "test", "tested", "hydrotest", "hydrotesting", "pressure test"],
}


def score_candidate_task(
    task: dict[str, Any],
    raw_update: str,
    location: str,
) -> float:
    """Calculate match score (0.0 to 100.0) between a task and site update."""
    raw_lower = raw_update.lower()
    task_act = str(task.get("activity", "")).strip().lower()
    task_loc = str(task.get("location", "")).strip().lower()
    loc_lower = location.strip().lower()

    # Extract distinctive activity tokens
    act_words = [w for w in re.findall(r"\w+", task_act) if len(w) > 2]
    distinctive = [w for w in act_words if w not in _STOP_WORDS]
    if not distinctive:
        distinctive = act_words

    if not distinctive:
        return 0.0

    # Check exact phrase or cleaned phrase
    clean_phrase = " ".join(distinctive)
    if task_act in raw_lower or clean_phrase in raw_lower:
        base_score = 95.0
    else:
        matched_tokens = 0
        for tok in distinctive:
            # direct token in update
            if re.search(r"\b" + re.escape(tok) + r"\b", raw_lower):
                matched_tokens += 1
                continue
            # stem/synonym in update
            found_stem = False
            for root, syns in _ACTIVITY_SYNONYMS.items():
                if tok.startswith(root) or any(s in tok for s in syns):
                    if any(re.search(r"\b" + re.escape(s) + r"\b", raw_lower) or s in raw_lower for s in syns):
                        matched_tokens += 1
                        found_stem = True
                        break
            if found_stem:
                continue

        if matched_tokens == len(distinctive):
            base_score = 90.0
        elif matched_tokens > 0:
            ratio = matched_tokens / len(distinctive)
            base_score = 50.0 + (ratio * 35.0)
        else:
            base_score = 0.0

    if base_score == 0.0:
        return 0.0

    # Location agreement bonus (+5)
    location_bonus = 0.0
    if task_loc and (task_loc == loc_lower or task_loc in raw_lower or loc_lower in task_loc):
        location_bonus = 5.0

    return min(100.0, base_score + location_bonus)


def evaluate_task_matches(
    candidate_tasks: list[dict[str, Any]],
    raw_update: str,
    location: str,
) -> tuple[str | None, float, dict[str, Any]]:
    """Match update to candidate tasks and return (matched_task_id, confidence_score, metadata).

    Tiers:
    - High confidence (>= 75.0): single clear candidate, auto-link.
    - Medium confidence (50.0 - 74.9): ambiguous match or tentative keyword overlap, requires planner review.
    - Low confidence (< 50.0): no candidate matches, requires unmatched planner review.
    """
    if not candidate_tasks:
        return (
            None,
            20.0,
            {
                "confidence_tier": "low",
                "matched_source_task_id": None,
                "is_ambiguous": False,
                "reasoning": "No candidate tasks available to match.",
            },
        )

    scored: list[tuple[float, dict[str, Any]]] = []
    for t in candidate_tasks:
        score = score_candidate_task(t, raw_update, location)
        scored.append((score, t))

    # Sort descending by score, then planned_start
    scored.sort(key=lambda x: (-x[0], x[1].get("planned_start") or ""))
    top_score, top_task = scored[0]

    if top_score >= 75.0:
        # Check ambiguity against second candidate
        if len(scored) > 1:
            second_score, second_task = scored[1]
            if second_score >= 50.0 and (top_score - second_score < 20.0):
                # Competing candidates -> Medium tier (ambiguous match for planner review)
                return (
                    top_task["id"],
                    60.0,
                    {
                        "confidence_tier": "medium",
                        "matched_source_task_id": top_task.get("source_task_id"),
                        "is_ambiguous": True,
                        "ambiguity_competing_task_id": second_task.get("source_task_id"),
                        "reasoning": f"Ambiguous match between {top_task.get('source_task_id')} and {second_task.get('source_task_id')} (close keyword scores). Requires planner review.",
                    },
                )
        # Unambiguous high confidence match
        return (
            top_task["id"],
            top_score,
            {
                "confidence_tier": "high",
                "matched_source_task_id": top_task.get("source_task_id"),
                "is_ambiguous": False,
                "reasoning": f"High-confidence match to {top_task.get('source_task_id')} ({top_task.get('activity')}) with {top_score:.1f}% confidence score.",
            },
        )

    if top_score >= 50.0:
        # Medium confidence match -> Planner review
        return (
            top_task["id"],
            top_score,
            {
                "confidence_tier": "medium",
                "matched_source_task_id": top_task.get("source_task_id"),
                "is_ambiguous": False,
                "reasoning": f"Medium-confidence tentative match to {top_task.get('source_task_id')} ({top_task.get('activity')}). Planner review recommended.",
            },
        )

    # Low confidence fallback (candidate exists by date, but keyword score is low or absent)
    date_sorted = sorted(
        candidate_tasks,
        key=lambda t: (t.get("planned_end") or "", t.get("planned_start") or ""),
    )
    fallback_task = date_sorted[0]
    return (
        fallback_task["id"],
        40.0,
        {
            "confidence_tier": "low",
            "matched_source_task_id": fallback_task.get("source_task_id"),
            "suggested_source_task_id": fallback_task.get("source_task_id"),
            "is_ambiguous": False,
            "reasoning": f"Tentative date-based fallback to {fallback_task.get('source_task_id')} ({fallback_task.get('activity')}) with low confidence (40%). Requires planner review.",
        },
    )


class MockAIProvider(AIProvider):
    """Deterministic keyword and activity-similarity analyser used when no real API is configured.

    Implements calibrated confidence tiers:
    - High confidence (>= 75.0): auto-link
    - Medium confidence (50.0 - 74.9): planner review with suggested match / ambiguity flag
    - Low confidence (< 50.0): unmatched planner review
    """

    def analyse(
        self,
        raw_update: str,
        location: str,
        reported_on: str,
        candidate_tasks: list[dict[str, Any]],
    ) -> AnalysisResult:
        text_lower = raw_update.lower()

        # --- Task matching with confidence tiers ---
        matched_task_id, confidence, match_meta = evaluate_task_matches(
            candidate_tasks=candidate_tasks,
            raw_update=raw_update,
            location=location,
        )

        # --- Status (INFERENCE from keywords) ---
        status: str | None = None
        for keyword, mapped_status in _STATUS_KEYWORDS.items():
            if keyword in text_lower:
                status = mapped_status
                break

        # --- Progress percent (FACT: only from explicit "X%" pattern) ---
        progress_percent: float | None = None
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_update)
        if pct_match:
            candidate = float(pct_match.group(1))
            if 0.0 <= candidate <= 100.0:
                progress_percent = candidate
        # If status is "completed" and no explicit %, infer 100 (INFERENCE)
        if progress_percent is None and status == "completed":
            progress_percent = 100.0

        # --- Delay (FACT: digits or word numbers near delay keywords) ---
        delay_days = _extract_delay_days(raw_update)
        delay_reason: str | None = None

        # A task reporting < 100% progress cannot be 'completed'
        if progress_percent is not None and progress_percent < 100.0 and status == "completed":
            status = "in_progress"

        if delay_days is not None and delay_days > 0:
            if progress_percent is None or progress_percent < 100.0:
                status = "delayed"
        elif "delay" in text_lower or "behind" in text_lower:
            if progress_percent is None or progress_percent < 100.0:
                status = "delayed"

        # Extract reason (INFERENCE: sentence containing delay keyword)
        if status in ("delayed", "blocked"):
            sentences = re.split(r"[.!?\n]", raw_update)
            for sentence in sentences:
                sl = sentence.lower()
                if any(k in sl for k in ("delay", "behind", "block", "halt", "stopp", "rainfall", "rain", "weather", "breakdown")):
                    reason = sentence.strip()
                    if reason:
                        delay_reason = reason
                    break

        # --- Actual dates (FACT: only when explicitly stated in text) ---
        actual_start_date: str | None = _extract_start_date(raw_update)
        actual_end_date: str | None = _extract_end_date(raw_update)

        # --- Build result ---
        model_response: dict[str, Any] = {
            "provider": _MOCK_MODEL_NAME,
            "text_analysed": raw_update[:500],
            "matched_source_task_id": match_meta.get("matched_source_task_id"),
            "confidence_tier": match_meta.get("confidence_tier"),
            "is_ambiguous": match_meta.get("is_ambiguous", False),
            "reasoning": match_meta.get("reasoning", ""),
            "matched_keywords": {
                "status_keyword": next(
                    (k for k in _STATUS_KEYWORDS if k in text_lower), None
                ),
                "percent_pattern_found": pct_match is not None,
                "delay_found": delay_days is not None,
                "start_date_found": actual_start_date is not None,
                "end_date_found": actual_end_date is not None,
            },
            "note": (
                "Deterministic matching and confidence tiering. "
                "Values marked UNKNOWN are None. "
                "Replace with a real AI provider by setting AI_PROVIDER=gemini "
                "and GEMINI_API_KEY in .env."
            ),
        }

        return AnalysisResult(
            matched_task_id=matched_task_id,
            progress_percent=progress_percent,
            status=status,
            delay_days=delay_days,
            delay_reason=delay_reason,
            actual_start_date=actual_start_date,
            actual_end_date=actual_end_date,
            confidence_score=confidence,
            model_name=_MOCK_MODEL_NAME,
            model_response=model_response,
        )


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def get_provider() -> AIProvider:
    """Return the configured AI provider.

    Reads AI_PROVIDER from the environment (default: 'mock').
    Supported values:
      - 'mock'   — deterministic keyword heuristics, no API key needed.
      - 'gemini' — calls Gemini via google-genai; requires GEMINI_API_KEY.
    """
    provider_name = os.getenv("AI_PROVIDER", "mock").lower().strip()
    if provider_name == "mock":
        return MockAIProvider()
    if provider_name == "gemini":
        return GeminiAIProvider()
    raise ValueError(
        f"Unknown AI_PROVIDER '{provider_name}'. "
        "Supported values: 'mock', 'gemini'."
    )


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

# Allowed status values as per the DB CHECK constraint
_ALLOWED_STATUSES = frozenset(
    {"not_started", "in_progress", "completed", "delayed", "blocked"}
)

_GEMINI_MODEL = "gemini-3.6-flash"

_SYSTEM_PROMPT = """\
You are a construction-project analyst.
You will be given a raw site update report and a list of planned schedule tasks.

Your job is to analyse the update and return a JSON object with these fields:

- matched_source_task_id (string or null):
    The source_task_id of the planned task that this update most likely refers to.
    Base this on the location and activity keywords in the update.
    Set to null if the update cannot be matched to any task.

- progress_percent (number 0-100 or null):
    FACT only. Extract this if the update explicitly states a percentage for
    the relevant work, even with approximation qualifiers such as "around",
    "approximately", "roughly", or "about"
    (e.g. "75% complete", "around 40% complete", "roughly half done" → 50).
    The percentage must be clearly stated by the reporter — do not estimate or
    invent one. Set to null only when no percentage figure is mentioned at all.
    Exception: if status is "completed" with no percentage stated, set to 100.

- status (string or null):
    One of: "not_started", "in_progress", "completed", "delayed", "blocked".
    INFERENCE is acceptable here based on the language of the update.
    Set to null if you cannot determine this with reasonable confidence.

- delay_days (integer >= 0 or null):
    FACT only. Set only if the update explicitly states a number of days of delay
    (e.g. "delayed by 3 days"). Do not estimate. Set to null otherwise.

- delay_reason (string or null):
    INFERENCE acceptable. A short phrase describing why the delay or blockage
    occurred. Set to null if status is not "delayed" or "blocked", or if no
    reason can be inferred.

- actual_start_date (string "YYYY-MM-DD" or null):
    FACT only. Set only if the update explicitly states that work has started
    on a specific date. Do not infer. Set to null otherwise.

- actual_end_date (string "YYYY-MM-DD" or null):
    FACT only. Set only if the update explicitly states that work was completed
    on a specific date. Do not infer. Set to null otherwise.

- confidence_score (number 0-100):
    Your confidence in the overall analysis. Use:
    80-100 for very clear updates with explicit facts.
    50-79 for updates where some inference was needed.
    20-49 for vague updates with significant uncertainty.
    0-19 for updates you could not meaningfully interpret.

- reasoning (string):
    A one or two sentence explanation of why you made these choices.
    This is stored for audit purposes.

CRITICAL RULES:
1. Never invent dates, percentages, or delay counts.
2. If a value is UNKNOWN, set it to null — do not guess.
3. Return only valid JSON. No markdown fences, no extra text.
4. actual_start_date and actual_end_date must be in YYYY-MM-DD format or null.
"""


class GeminiAIProvider(AIProvider):
    """AI provider backed by Google Gemini via the google-genai SDK.

    Reads GEMINI_API_KEY from the environment.
    Requests structured JSON output from the model.
    Never invents values: unknown fields are left as None.
    """

    def __init__(self, api_key: str | None = None) -> None:
        # Import here so that MockAIProvider tests never need google-genai
        try:
            import google.genai as genai
            from google.genai import types as genai_types
        except ImportError as exc:
            raise ImportError(
                "google-genai is required for GeminiAIProvider. "
                "Run: pip install 'google-genai>=1.0,<2.0'"
            ) from exc

        resolved_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. "
                "Add GEMINI_API_KEY=<your-key> to backend/.env, "
                "or set AI_PROVIDER=mock to use the keyword heuristic."
            )

        self._client = genai.Client(api_key=resolved_key)
        self._types = genai_types

    def analyse(
        self,
        raw_update: str,
        location: str,
        reported_on: str,
        candidate_tasks: list[dict[str, Any]],
    ) -> AnalysisResult:
        # Build the user message
        if candidate_tasks:
            task_lines = "\n".join(
                f"  - source_task_id={t['source_task_id']}, "
                f"activity={t['activity']}, "
                f"location={t['location']}, "
                f"planned_start={t.get('planned_start', '?')}, "
                f"planned_end={t.get('planned_end', '?')}"
                for t in candidate_tasks
            )
            tasks_section = f"PLANNED TASKS AT THIS LOCATION:\n{task_lines}"
        else:
            tasks_section = "PLANNED TASKS AT THIS LOCATION: none found"

        user_message = (
            f"SITE UPDATE\n"
            f"Location: {location}\n"
            f"Reported on: {reported_on}\n"
            f"Update text:\n{raw_update}\n\n"
            f"{tasks_section}"
        )

        # Build source_task_id → id map for resolving the matched task UUID
        task_id_map: dict[str, str] = {
            t["source_task_id"]: t["id"] for t in candidate_tasks
        }

        raw_response_text: str = ""
        parsed: dict[str, Any] = {}

        # --- Step 1: call the API (network / auth errors surface here) ---
        try:
            response = self._client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=user_message,
                config=self._types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.1,   # low temperature → more deterministic output
                    max_output_tokens=2048,  # raised from 1024; reasoning can be long
                ),
            )
            raw_response_text = response.text or ""
        except Exception as exc:  # network, auth, quota errors
            logger.warning(
                "GeminiAIProvider: API call failed (%s: %s). "
                "Returning a low-confidence unknown result.",
                type(exc).__name__,
                str(exc)[:200],
            )
            return _unknown_result(_GEMINI_MODEL, str(exc), raw="")

        # --- Step 2: parse the model output (decode / format errors surface here) ---
        try:
            parsed = _extract_json(raw_response_text)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "GeminiAIProvider: JSON parse failed (%s: %s). "
                "Raw response (first 500 chars): %r",
                type(exc).__name__,
                str(exc),
                raw_response_text[:500],
            )
            return _unknown_result(_GEMINI_MODEL, str(exc), raw=raw_response_text)

        # Resolve matched_task_id: model returns source_task_id; we need the DB UUID
        matched_source = _safe_str(parsed.get("matched_source_task_id"))
        matched_task_uuid: str | None = None
        if matched_source and matched_source in task_id_map:
            matched_task_uuid = task_id_map[matched_source]

        # Validate status against DB CHECK constraint
        raw_status = _safe_str(parsed.get("status"))
        status: str | None = raw_status if raw_status in _ALLOWED_STATUSES else None

        # progress_percent: must be 0-100
        progress = _safe_float(parsed.get("progress_percent"))
        if progress is not None and not (0.0 <= progress <= 100.0):
            progress = None

        # delay_days: must be non-negative int, fallback to text parsing if model missed it
        delay_days = _safe_int(parsed.get("delay_days"))
        if delay_days is None:
            delay_days = _extract_delay_days(raw_update)
        elif delay_days < 0:
            delay_days = None

        if delay_days and delay_days > 0 and status is None:
            status = "delayed"

        # confidence_score: must be 0-100
        raw_conf = _safe_float(parsed.get("confidence_score"))
        if raw_conf is not None:
            confidence = max(0.0, min(100.0, raw_conf))
        else:
            _, eval_conf, _ = evaluate_task_matches(
                candidate_tasks=candidate_tasks,
                raw_update=raw_update,
                location=location,
            )
            confidence = eval_conf

        # date fields: must match YYYY-MM-DD
        actual_start = _safe_iso_date(parsed.get("actual_start_date"))
        actual_end = _safe_iso_date(parsed.get("actual_end_date"))

        return AnalysisResult(
            matched_task_id=matched_task_uuid,
            progress_percent=progress,
            status=status,
            delay_days=delay_days,
            delay_reason=_safe_str(parsed.get("delay_reason")),
            actual_start_date=actual_start,
            actual_end_date=actual_end,
            confidence_score=confidence,
            model_name=_GEMINI_MODEL,
            model_response={
                "raw_json": parsed,
                "matched_source_task_id": matched_source,
                "reasoning": _safe_str(parsed.get("reasoning")),
            },
        )


# ---------------------------------------------------------------------------
# Parsing helpers (used by GeminiAIProvider only)
# ---------------------------------------------------------------------------

# Matches an optional ```json … ``` or ``` … ``` fence around a JSON object/array.
_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_json(text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from a model response string.

    Handles three common Gemini output shapes:
    1. Plain JSON  – `{"key": "value"}`
    2. Fenced JSON – ` ```json\n{...}\n``` ` (model ignores MIME type hint)
    3. Fenced JSON without language tag – ` ```\n{...}\n``` `

    Raises
    ------
    ValueError       – if the text is empty or no JSON object can be found.
    json.JSONDecodeError – if a candidate string is found but is not valid JSON.
    """
    if not text or not text.strip():
        raise ValueError("Model returned an empty response.")

    # Try 1: direct parse (fastest path; works when MIME type is honoured)
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Try 2: extract from a markdown fence
    fence_match = _FENCE_RE.search(stripped)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Try 3: find the first '{' and last '}' and try to parse that substring
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(stripped[start : end + 1])

    raise ValueError(
        f"No JSON object found in model response (first 200 chars): {stripped[:200]!r}"
    )


def _unknown_result(model_name: str, error: str, raw: str) -> AnalysisResult:
    """Return a zero-confidence AnalysisResult for use in error/fallback paths."""
    return AnalysisResult(
        matched_task_id=None,
        progress_percent=None,
        status=None,
        delay_days=None,
        delay_reason=None,
        actual_start_date=None,
        actual_end_date=None,
        confidence_score=0.0,
        model_name=model_name,
        model_response={
            "error": error[:500],
            "raw": raw[:500],
            "note": "API call or parse failed; all fields UNKNOWN.",
        },
    )


def _safe_str(value: Any) -> str | None:
    """Return a stripped non-empty string, or None."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s and s.lower() not in ("null", "none", "") else None


def _safe_float(value: Any) -> float | None:
    """Return a float, or None if conversion fails."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    """Return an int, or None if conversion fails."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_iso_date(value: Any) -> str | None:
    """Return a YYYY-MM-DD string if it matches that format and is a valid calendar date, or None."""
    s = _safe_str(value)
    if s and _ISO_DATE_RE.match(s):
        try:
            return datetime.date.fromisoformat(s).isoformat()
        except ValueError:
            return None
    return None

