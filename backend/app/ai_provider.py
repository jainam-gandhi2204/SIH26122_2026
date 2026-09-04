"""AI provider interface and mock implementation.

Architecture
------------
AIProvider is an abstract interface.  Any real model (Gemini, GPT-4, etc.)
can be plugged in later by subclassing AIProvider and implementing `analyse`.

MockAIProvider is the deterministic implementation used when no real API key
is configured.  It uses simple keyword heuristics applied to the raw_update
text.  It never hallucinates: values it cannot determine are left as None
(UNKNOWN).

Usage
-----
Call `get_provider()` to obtain the configured provider.  It reads
AI_PROVIDER from the environment (default: "mock").
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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

_PROGRESS_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"(\d+)\s*%\s*(?:complete|done|finish|progress)", re.I), None),
    (re.compile(r"(?:complete|done|finish)[^\d]*(\d+)\s*%", re.I), None),
]

_MOCK_MODEL_NAME = "mock-keyword-v1"


class MockAIProvider(AIProvider):
    """Deterministic keyword-based analyser used when no real API is configured.

    Confidence is intentionally low (40) because this is a heuristic, not a model.
    Values are populated only when they can be directly read from the text (FACT)
    or safely inferred from the schedule context (INFERENCE).
    """

    def analyse(
        self,
        raw_update: str,
        location: str,
        reported_on: str,
        candidate_tasks: list[dict[str, Any]],
    ) -> AnalysisResult:
        text_lower = raw_update.lower()

        # --- Task matching (INFERENCE: same location, earliest planned_end first) ---
        matched_task_id: str | None = None
        if candidate_tasks:
            sorted_tasks = sorted(
                candidate_tasks,
                key=lambda t: (t.get("planned_end") or "", t.get("planned_start") or ""),
            )
            # Prefer task whose activity keywords appear in the update text
            for task in sorted_tasks:
                activity_words = task.get("activity", "").lower().split()
                if any(w in text_lower for w in activity_words if len(w) > 3):
                    matched_task_id = task["id"]
                    break
            # Fall back to first task by date
            if matched_task_id is None:
                matched_task_id = sorted_tasks[0]["id"]

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

        # --- Delay (FACT: only from explicit "X day(s)" near "delay/behind") ---
        delay_days: int | None = None
        delay_reason: str | None = None
        delay_match = re.search(
            r"(\d+)\s*day[s]?\s*(?:delay|behind|late|overdue)", raw_update, re.I
        )
        if delay_match:
            delay_days = int(delay_match.group(1))
        if status == "delayed" and delay_days is None:
            # delay acknowledged but magnitude not stated
            delay_days = None
        # Extract reason (INFERENCE: sentence containing delay keyword)
        if status in ("delayed", "blocked"):
            sentences = re.split(r"[.!?\n]", raw_update)
            for sentence in sentences:
                sl = sentence.lower()
                if any(k in sl for k in ("delay", "behind", "block", "halt", "stopp")):
                    reason = sentence.strip()
                    if reason:
                        delay_reason = reason
                    break

        # --- Actual dates (UNKNOWN unless explicitly stated – hard to parse reliably) ---
        actual_start_date: str | None = None
        actual_end_date: str | None = None

        # --- Build result ---
        model_response: dict[str, Any] = {
            "provider": _MOCK_MODEL_NAME,
            "text_analysed": raw_update[:500],
            "matched_keywords": {
                "status_keyword": next(
                    (k for k in _STATUS_KEYWORDS if k in text_lower), None
                ),
                "percent_pattern_found": pct_match is not None,
            },
            "note": (
                "Deterministic keyword heuristic. "
                "Values marked UNKNOWN are None. "
                "Replace with a real AI provider by setting AI_PROVIDER=gemini "
                "and GEMINI_API_KEY in .env."
            ),
        }

        confidence: float = 40.0 if matched_task_id else 20.0

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

    Reads AI_PROVIDER from the environment.
    Currently only 'mock' is implemented.  Set AI_PROVIDER=gemini when ready
    to plug in the real model, and implement a GeminiAIProvider subclass.
    """
    provider_name = os.getenv("AI_PROVIDER", "mock").lower().strip()
    if provider_name == "mock":
        return MockAIProvider()
    raise ValueError(
        f"Unknown AI_PROVIDER '{provider_name}'. "
        "Currently supported: 'mock'. "
        "To add a real provider, subclass AIProvider in app/ai_provider.py."
    )
