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

Domain Aliases
--------------
Both providers apply alias normalization from
backend/app/config/domain_aliases.yaml before matching.
This maps informal/abbreviated terms to canonical schedule terms so that
"WPA" → "Well Pad A", "ROW" → "Right of Way Clearance", "excvation" → "Excavation Work", etc.

Confidence Design
-----------------
Confidence is computed as a COMPOSITE of:
  - LLM semantic match confidence (40%)
  - Deterministic multi-signal shortlist score (30%)
  - LLM extraction clarity confidence (20%)
  - Location agreement bonus (10%)

This prevents blind trust in the LLM's self-reported score and grounds
confidence in objective, auditable signals.

Tiers (aligned with ai_processor.py):
  HIGH  >= 80.0 → auto-link, update schedule actuals
  MEDIUM 50.0–79.9 → planner review
  LOW   < 50.0 → unmatched review (matched_task_id set to None)

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain alias system
# ---------------------------------------------------------------------------

def _default_aliases() -> dict[str, dict[str, list[str]]]:
    """Return an empty alias structure as a safe fallback."""
    return {"activity_aliases": {}, "location_aliases": {}}


def load_domain_aliases(
    config_path: str | Path | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Load domain aliases from YAML config.

    Falls back to an empty alias dict if PyYAML is not installed or the
    config file is missing.  This makes the alias system purely additive:
    the core matching logic always works even without aliases.

    Parameters
    ----------
    config_path:
        Explicit path to the YAML file.  If None, resolves relative to
        this source file: ../config/domain_aliases.yaml
    """
    if config_path is None:
        config_path = Path(__file__).parent / "config" / "domain_aliases.yaml"
    config_path = Path(config_path)
    if not config_path.exists():
        logger.debug("domain_aliases.yaml not found at %s — aliases disabled.", config_path)
        return _default_aliases()
    try:
        import yaml  # type: ignore[import]
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            return _default_aliases()
        return {
            "activity_aliases": data.get("activity_aliases") or {},
            "location_aliases": data.get("location_aliases") or {},
        }
    except ImportError:
        logger.debug("PyYAML not installed — domain aliases disabled.")
        return _default_aliases()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load domain_aliases.yaml: %s", exc)
        return _default_aliases()


def _build_alias_map(
    alias_dict: dict[str, list[str]],
) -> list[tuple[str, str]]:
    """Build a sorted (longest-first) list of (alias_lower, canonical) pairs.

    Longest-first ensures a more specific alias matches before a shorter one
    that might be a substring of it (e.g., "ROW corridor" before "ROW").
    """
    pairs: list[tuple[str, str]] = []
    for canonical, aliases in alias_dict.items():
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if alias and isinstance(alias, str):
                pairs.append((alias.lower(), canonical))
    # Sort by alias length descending so longer matches take priority
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def normalize_with_aliases(
    text: str,
    alias_pairs: list[tuple[str, str]],
) -> str:
    """Apply alias normalization to text.

    For each (alias, canonical) pair (sorted longest-first), replace whole-word
    occurrences of the alias with the canonical term.  Case-insensitive.

    Returns the normalized text.
    """
    if not text or not alias_pairs:
        return text
    result = text
    for alias_lower, canonical in alias_pairs:
        # Use word-boundary match; escape special regex chars in alias
        pattern = r"(?<!\w)" + re.escape(alias_lower) + r"(?!\w)"
        try:
            result = re.sub(pattern, canonical, result, flags=re.IGNORECASE)
        except re.error:
            # Malformed alias pattern — skip silently
            continue
    return result


# Module-level cached aliases (loaded once at import time)
_DOMAIN_ALIASES: dict[str, dict[str, list[str]]] = load_domain_aliases()
_ACTIVITY_ALIAS_PAIRS: list[tuple[str, str]] = _build_alias_map(
    _DOMAIN_ALIASES.get("activity_aliases", {})
)
_LOCATION_ALIAS_PAIRS: list[tuple[str, str]] = _build_alias_map(
    _DOMAIN_ALIASES.get("location_aliases", {})
)
_ALL_ALIAS_PAIRS: list[tuple[str, str]] = _build_alias_map(
    {**_DOMAIN_ALIASES.get("activity_aliases", {}), **_DOMAIN_ALIASES.get("location_aliases", {})}
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ActivityObservation:
    """A single activity observation extracted from a site update.

    Used for multi-activity extraction.  One site update may produce multiple
    observations (e.g., "pipeline laid + welding done at WPA").

    Phase 1: These are stored in model_response.additional_observations.
    Phase 2: Each observation can be persisted as a separate DB row.
    """
    activity_description: str           # What the update says about this activity
    location_mentioned: str | None = None      # Location as mentioned (may be alias)
    progress_percent: float | None = None      # Extracted progress (FACT only)
    status: str | None = None                  # Extracted status (INFERENCE ok)
    delay_days: int | None = None              # Delay in days (FACT only)
    delay_reason: str | None = None            # Delay reason (INFERENCE ok)
    extraction_confidence: float = 75.0        # How clearly this observation was stated (0–100)
    candidate_source_task_id: str | None = None  # Best candidate task for this observation
    actual_start_date: str | None = None       # Start date if explicitly stated (FACT only)
    actual_end_date: str | None = None         # End date if explicitly stated (FACT only)
    match_confidence: float | None = None      # Task matching confidence (0–100)
    is_ambiguous: bool = False                 # Ambiguity flag for this observation
    reasoning: str | None = None               # Audit reasoning for this observation
    raw_json: dict[str, Any] | None = None     # Raw model response/evidence for this observation
    model_response: dict[str, Any] | None = None  # Full model response dict if available



@dataclass
class AnalysisResult:
    """All fields that map directly to the ai_processed_updates table columns.

    None means UNKNOWN – value not stated in or safely inferable from the text.

    Confidence Design:
    - confidence_score is a COMPOSITE, not the raw LLM score.
    - matched_task_id is None when confidence < 50.0 (no false match stored).
    - model_response contains: reasoning, confidence_tier, is_ambiguous,
      extraction_confidence, match_confidence, deterministic_score,
      and additional_observations for multi-activity cases.
    """
    matched_task_id: str | None          # UUID of schedule_tasks row, or None
    progress_percent: float | None       # 0–100; None = UNKNOWN
    status: str | None                   # one of the allowed DB enum values; None = UNKNOWN
    delay_days: int | None               # non-negative; None = UNKNOWN
    delay_reason: str | None             # free text; None = UNKNOWN
    actual_start_date: str | None        # ISO date string; None = UNKNOWN
    actual_end_date: str | None          # ISO date string; None = UNKNOWN
    confidence_score: float              # 0–100 COMPOSITE score
    model_name: str
    model_response: dict[str, Any]       # full model output for audit trail
    additional_observations: list[ActivityObservation] = field(default_factory=list)


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
                          These are the shortlisted candidates from ai_processor.

        Returns
        -------
        AnalysisResult with only FACT or INFERENCE values populated.
        Anything that cannot be determined is left as None.
        matched_task_id is None when confidence < 50.0.
        """


# ---------------------------------------------------------------------------
# Composite confidence computation
# ---------------------------------------------------------------------------

def compute_composite_confidence(
    llm_confidence: float,
    llm_extraction_confidence: float,
    deterministic_score: float,
    location_match_score: int,
) -> float:
    """Compute a composite confidence score from multiple independent signals.

    This prevents blind trust in the LLM's self-reported confidence number.
    The formula combines:
    - LLM semantic match confidence (40%): how certain the model is of the match
    - Deterministic shortlist score (30%): keyword/location/date signals
    - LLM extraction clarity (20%): how clearly the update was stated
    - Location agreement bonus (10%): explicit location signal

    Parameters
    ----------
    llm_confidence:             LLM's reported match confidence (0–100).
    llm_extraction_confidence:  LLM's reported extraction confidence (0–100).
    deterministic_score:        Score from deterministic shortlisting (0–100).
    location_match_score:       0=none, 1=partial, 2=alias, 3=exact (scaled to 0–100).

    Returns
    -------
    Composite score in [0.0, 100.0].
    """
    _LOC_NORMALIZED = {0: 0.0, 1: 33.0, 2: 67.0, 3: 100.0}
    location_normalized = _LOC_NORMALIZED.get(location_match_score, 0.0)
    composite = (
        0.40 * max(0.0, min(100.0, llm_confidence))
        + 0.30 * max(0.0, min(100.0, deterministic_score))
        + 0.20 * max(0.0, min(100.0, llm_extraction_confidence))
        + 0.10 * location_normalized
    )
    return round(min(100.0, max(0.0, composite)), 2)


# ---------------------------------------------------------------------------
# Mock / deterministic provider
# ---------------------------------------------------------------------------

_STATUS_KEYWORDS: dict[str, str] = {
    # IMPORTANT: Check longer/negation/qualification phrases BEFORE shorter positive substrings.
    # "not start" must come before "started"; "yet to start" before "start".
    "not start": "not_started",
    "yet to start": "not_started",
    # Vague progress qualifications must map to in_progress (progress_percent stays None)
    "almost complete": "in_progress",
    "almost done": "in_progress",
    "almost finish": "in_progress",
    "nearly complete": "in_progress",
    "nearly done": "in_progress",
    "complet": "completed",
    "finish": "completed",
    "done": "completed",
    "signed off": "completed",
    "sign off": "completed",
    "handed over": "completed",
    "in progress": "in_progress",
    "ongoing": "in_progress",
    "started": "in_progress",
    "underway": "in_progress",
    "continuing": "in_progress",
    "proceeding": "in_progress",
    "delay": "delayed",
    "behind": "delayed",
    "overdue": "delayed",
    "block": "blocked",
    "halt": "blocked",
    "stopp": "blocked",
    "suspend": "blocked",
    "paused": "blocked",
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
    "complete", "completed", "progress", "due", "site", "pad", "work",
    "aaj", "kal", "hai", "kar", "raha", "karenge", "karna",  # Hinglish
})

_ACTIVITY_SYNONYMS: dict[str, list[str]] = {
    "excavat": ["excavation", "excavating", "excavated", "excavate", "earthwork", "digging", "trenched", "trenching", "excvation", "exvacation"],
    "clear": ["clearing", "clearance", "cleared", "site prep", "site preparation", "grubbing", "grading"],
    "cast": ["casting", "cast", "concreting", "concrete", "pour", "pouring", "poured"],
    "foundat": ["foundation", "foundations", "substructure", "footing", "footings"],
    "install": ["installation", "install", "installed", "installing", "erection", "erecting", "mount", "mounting"],
    "equip": ["equipment", "machinery", "skid", "generator", "pump", "compressor", "vessel"],
    "pipe": ["piping", "pipeline", "pipe", "pipe laying", "pipework", "flowline", "stringing", "pipe stringing"],
    "weld": ["welding", "weld", "welded", "tie-in", "joint", "jointing"],
    "commiss": ["commissioning", "commission", "commissioned", "pre-commissioning", "handover"],
    "test": ["testing", "test", "tested", "hydrotest", "hydrotesting", "pressure test"],
    "row": ["row", "row clearing", "row clearance", "right of way", "corridor clearing"],
    "way": ["row", "row clearing", "row clearance", "right of way", "corridor clearing"],
}



def _location_match_score(task_loc: str, loc_lower: str, raw_lower: str) -> int:
    """Return a location match score: 3=exact, 2=alias-normalized, 1=partial, 0=none.

    task_loc:  The task's canonical location (from schedule DB), already lowercase.
    loc_lower: The update's reported location, already lowercase.
    raw_lower: The raw update text, already lowercase (original, un-normalized).

    We normalize the INCOMING text (loc_lower, raw_lower) with aliases so that
    informal terms like "WPA" map to "Well Pad A" for comparison.  We do NOT
    re-normalize task_loc because it is already the canonical schedule value.
    """
    if not task_loc:
        return 0

    # Normalize the incoming location/text with aliases (informal → canonical)
    # so "WPA" → "well pad a" for comparison
    norm_update_loc = normalize_with_aliases(loc_lower, _LOCATION_ALIAS_PAIRS).lower()
    norm_raw = normalize_with_aliases(raw_lower, _LOCATION_ALIAS_PAIRS).lower()
    # task_loc is already canonical — compare directly (no re-normalization)
    task_loc_norm = task_loc  # already lowercase, already canonical

    # Exact match: reported location == task location (after alias expansion)
    if task_loc_norm == norm_update_loc:
        return 3
    # Task location appears in the normalized raw update text
    if task_loc_norm in norm_raw:
        return 2
    # Task location verbatim in the original raw text (direct mention)
    if task_loc_norm in raw_lower:
        return 2
    # Partial: update location is a substring of the task location
    if loc_lower and loc_lower in task_loc_norm:
        return 1
    # Partial: task location is a substring of the update location
    if task_loc_norm in loc_lower:
        return 1

    return 0


def score_candidate_task(
    task: dict[str, Any],
    raw_update: str,
    location: str,
) -> float:
    """Calculate keyword-based match score (0.0 to 100.0) between a task and site update.

    Applies alias normalization before scoring so that "excvation" and "ROW"
    are recognized as matching schedule activities containing "excavation" and
    "Right of Way".

    If base activity keyword score is 0 but location match is strong (score >= 2),
    a location-only base score of 40.0 is used so that vague updates at a known
    location can still be tentatively matched (for planner review).
    """
    # Apply alias normalization to both raw update and task activity
    norm_raw = normalize_with_aliases(raw_update, _ALL_ALIAS_PAIRS)
    raw_lower = norm_raw.lower()

    task_act_orig = str(task.get("activity", "")).strip()
    task_act = normalize_with_aliases(task_act_orig, _ALL_ALIAS_PAIRS).strip().lower()
    task_loc = str(task.get("location", "")).strip().lower()
    loc_lower = location.strip().lower()

    # Extract distinctive activity tokens (from normalized task activity)
    act_words = [w for w in re.findall(r"\w+", task_act) if len(w) > 2]
    distinctive = [w for w in act_words if w not in _STOP_WORDS]
    if not distinctive:
        distinctive = act_words

    # Location agreement bonus: 0=none, 1=partial(+3), 2=alias/text(+7), 3=exact(+10)
    loc_score = _location_match_score(task_loc, loc_lower, raw_update.lower())
    _LOC_BONUS = {0: 0.0, 1: 3.0, 2: 7.0, 3: 10.0}
    location_bonus = _LOC_BONUS[loc_score]

    if not distinctive:
        # No activity tokens at all (e.g., activity="Work" with all stop words).
        # Use location-only score if location matches.
        if loc_score >= 2:
            return min(100.0, 40.0 + location_bonus)
        return 0.0

    # Check exact phrase or cleaned phrase (normalized)
    clean_phrase = " ".join(distinctive)
    if task_act in raw_lower or clean_phrase in raw_lower:
        base_score = 100.0
    else:
        matched_tokens = 0
        for tok in distinctive:
            # direct token in update
            if re.search(r"\b" + re.escape(tok) + r"\b", raw_lower):
                matched_tokens += 1
                continue
            # stem/synonym in update (check normalized synonyms with word boundaries)
            found_stem = False
            for root, syns in _ACTIVITY_SYNONYMS.items():
                if tok.startswith(root) or any(s in tok for s in syns):
                    if any(re.search(r"\b" + re.escape(s) + r"\b", raw_lower) for s in syns):
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
            # No keyword match — but strong location match can provide a fallback score
            if loc_score >= 2:
                base_score = 40.0  # location-only tentative match → score: 40+7=47 or 40+10=50
            else:
                base_score = 0.0


    if base_score == 0.0:
        return 0.0

    return min(100.0, base_score + location_bonus)


def evaluate_task_matches(
    candidate_tasks: list[dict[str, Any]],
    raw_update: str,
    location: str,
) -> tuple[str | None, float, dict[str, Any]]:
    """Match update to candidate tasks and return (matched_task_id, confidence_score, metadata).

    Tiers (aligned with ai_processor.py auto-link logic):
    - High confidence (>= 80.0): single clear candidate, eligible for auto-link.
    - Medium confidence (50.0 – 79.9): tentative or ambiguous match, planner review.
    - Low confidence (< 50.0): no reliable match → matched_task_id = None.

    IMPORTANT: Low-confidence results return matched_task_id = None to prevent
    false matches being stored in the database.  The suggested task is preserved
    in the metadata under 'suggested_source_task_id' for planner reference.
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

    norm_raw = normalize_with_aliases(raw_update, _ALL_ALIAS_PAIRS)
    raw_lower = norm_raw.lower()

    scored: list[tuple[float, bool, dict[str, Any]]] = []
    for t in candidate_tasks:
        score = score_candidate_task(t, raw_update, location)
        task_act = normalize_with_aliases(str(t.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()
        act_words = [w for w in re.findall(r"\w+", task_act) if len(w) > 2]
        distinctive = [w for w in act_words if w not in _STOP_WORDS] or act_words
        clean_phrase = " ".join(distinctive)
        exact = bool(task_act and (task_act in raw_lower or clean_phrase in raw_lower))
        scored.append((score, exact, t))

    # Sort descending by score, exact match preference, planned_start
    scored.sort(key=lambda x: (-x[0], not x[1], x[2].get("planned_start") or ""))
    top_score, top_exact, top_task = scored[0]

    if top_score >= 50.0:
        # Check ambiguity against second candidate
        if len(scored) > 1:
            second_score, second_exact, second_task = scored[1]
            top_act = normalize_with_aliases(str(top_task.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()
            sec_act = normalize_with_aliases(str(second_task.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()
            top_words = set([w for w in re.findall(r"\w+", top_act) if len(w) > 2 and w not in _STOP_WORDS])
            sec_words = set([w for w in re.findall(r"\w+", sec_act) if len(w) > 2 and w not in _STOP_WORDS])

            # A candidate whose tokens are a subset of top_task or top_task is exact and second is not, is subsumed
            is_subsumed = (bool(sec_words) and sec_words.issubset(top_words)) or (top_exact and not second_exact)
            if second_score >= 55.0 and (top_score - second_score < 20.0) and not is_subsumed:
                # Competing candidates → Medium tier (ambiguous match for planner review)
                return (
                    top_task["id"],
                    60.0,
                    {
                        "confidence_tier": "medium",
                        "matched_source_task_id": top_task.get("source_task_id"),
                        "is_ambiguous": True,
                        "ambiguity_competing_task_id": second_task.get("source_task_id"),
                        "reasoning": (
                            f"Ambiguous match between {top_task.get('source_task_id')} "
                            f"and {second_task.get('source_task_id')} "
                            f"(close keyword scores). Requires planner review."
                        ),
                    },
                )

        if top_score >= 80.0:
            # Unambiguous high confidence match
            return (
                top_task["id"],
                top_score,
                {
                    "confidence_tier": "high",
                    "matched_source_task_id": top_task.get("source_task_id"),
                    "is_ambiguous": False,
                    "reasoning": (
                        f"High-confidence match to {top_task.get('source_task_id')} "
                        f"({top_task.get('activity')}) with {top_score:.1f}% score."
                    ),
                },
            )

        # Medium confidence match → Planner review
        return (
            top_task["id"],
            top_score,
            {
                "confidence_tier": "medium",
                "matched_source_task_id": top_task.get("source_task_id"),
                "is_ambiguous": False,
                "reasoning": (
                    f"Medium-confidence tentative match to {top_task.get('source_task_id')} "
                    f"({top_task.get('activity')}). Planner review recommended."
                ),
            },
        )

    # Low confidence: suggest the best available candidate but do NOT set matched_task_id.
    # Returning None for matched_task_id prevents a false match being stored in the DB.
    # Return a small confidence (25.0) that is higher than the no-candidate fallback (20.0)
    # but clearly stays in the LOW tier (< 50.0).
    date_sorted = sorted(
        candidate_tasks,
        key=lambda t: (t.get("planned_end") or "", t.get("planned_start") or ""),
    )
    suggested_task = date_sorted[0]
    # Use 25.0 when candidates exist (vs 20.0 for no-candidates), indicating
    # "schedule was searched but no keyword match found"
    low_conf = max(25.0, min(top_score, 45.0)) if top_score > 0.0 else 25.0
    return (
        None,  # NO false match stored
        low_conf,
        {
            "confidence_tier": "low",
            "matched_source_task_id": None,
            "suggested_source_task_id": suggested_task.get("source_task_id"),
            "suggested_activity": suggested_task.get("activity"),
            "is_ambiguous": False,
            "reasoning": (
                f"No reliable match found (top keyword score: {top_score:.1f}%). "
                f"Nearest candidate by date: {suggested_task.get('source_task_id')} "
                f"({suggested_task.get('activity')}). Requires unmatched review."
            ),
        },
    )


def _has_activity_keyword_match(task: dict[str, Any], text: str) -> bool:
    """Check if text contains any distinctive activity keyword from the task."""
    task_act = normalize_with_aliases(str(task.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()
    act_words = [w for w in re.findall(r"\w+", task_act) if len(w) > 2]
    distinctive = [w for w in act_words if w not in _STOP_WORDS] or act_words
    clean_phrase = " ".join(distinctive)
    text_lower = text.lower()
    if clean_phrase in text_lower or task_act in text_lower:
        return True
    for w in distinctive:
        if re.search(r"\b" + re.escape(w) + r"\b", text_lower):
            return True
        for root, syns in _ACTIVITY_SYNONYMS.items():
            if w.startswith(root) or any(s in w for s in syns):
                if any(re.search(r"\b" + re.escape(s) + r"\b", text_lower) for s in syns):
                    return True
    return False


def _split_into_activity_clauses(text: str) -> list[str]:
    """Split raw update text into distinct clauses or sentences."""
    if not text:
        return []
    # Split by major sentence delimiters: newlines, periods, exclamation/question marks, semicolons
    # (?<!\d)[.!?;\n]+(?!\d) avoids splitting on decimals (e.g. 60.5%) or dates (e.g. 2026.09.21)
    parts = [s.strip() for s in re.split(r"(?<!\d)[.!?;\n]+(?!\d)", text) if s.strip()]
    return parts


def _parse_activity_clause(
    clause: str,
    location: str,
    candidate_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Parse a single activity clause or sentence into an observation dict."""
    norm_clause = normalize_with_aliases(clause, _ALL_ALIAS_PAIRS)
    c_lower = norm_clause.lower()
    matched_id, raw_conf, meta = evaluate_task_matches(candidate_tasks, norm_clause, location)

    if matched_id:
        matched_t = next((x for x in candidate_tasks if x["id"] == matched_id), None)
        if matched_t and not _has_activity_keyword_match(matched_t, norm_clause):
            # Clause matched on location/date only without mentioning activity keywords
            matched_id = None
            meta["matched_source_task_id"] = None
            meta["confidence_tier"] = "low"
            raw_conf = 25.0

    pct_m = re.search(r"(\d+(?:\.\d+)?)\s*%", clause)
    has_explicit_pct = pct_m is not None
    progress: float | None = None
    if has_explicit_pct and pct_m:
        candidate_pct = float(pct_m.group(1))
        if 0.0 <= candidate_pct <= 100.0:
            progress = candidate_pct

    status: str | None = None
    for kw, mapped in _STATUS_KEYWORDS.items():
        if kw in c_lower:
            status = mapped
            break

    if progress is None and status == "completed":
        progress = 100.0
    if progress is not None and progress < 100.0 and status == "completed":
        status = "in_progress"

    delay_days = _extract_delay_days(clause)
    delay_reason: str | None = None
    if delay_days is not None and delay_days > 0:
        if progress is None or progress < 100.0:
            status = "delayed"
    elif "delay" in c_lower or "behind" in c_lower:
        if progress is None or progress < 100.0:
            status = "delayed"

    if status in ("delayed", "blocked"):
        for kw in ("delay", "behind", "block", "halt", "stopp", "rainfall", "rain", "weather", "breakdown"):
            if kw in c_lower:
                delay_reason = clause.strip()
                break

    start_d = _extract_start_date(clause)
    end_d = _extract_end_date(clause)

    has_stat_kw = status is not None
    has_cands = len(candidate_tasks) > 0
    if not has_cands:
        ext_conf = 15.0
    elif has_explicit_pct and has_stat_kw:
        ext_conf = 95.0
    elif has_explicit_pct or has_stat_kw:
        ext_conf = 85.0
    elif len(clause.strip()) < 20:
        ext_conf = 40.0
    else:
        ext_conf = 70.0

    loc_score = 0
    if matched_id:
        t = next((x for x in candidate_tasks if x["id"] == matched_id), None)
        if t:
            loc_score = _location_match_score(
                t.get("location", "").lower(),
                location.lower(),
                clause.lower(),
            )

    comp_conf = compute_composite_confidence(
        llm_confidence=raw_conf,
        llm_extraction_confidence=ext_conf,
        deterministic_score=raw_conf,
        location_match_score=loc_score,
    )
    if meta.get("is_ambiguous"):
        comp_conf = min(comp_conf, 60.0)

    is_ambiguous = meta.get("is_ambiguous", False)
    tier = meta.get("confidence_tier", "low")
    matched_src = meta.get("matched_source_task_id")

    if comp_conf < 50.0:
        matched_id = None
        matched_src = None
        tier = "low"

    return {
        "clause_text": clause,
        "matched_task_id": matched_id,
        "matched_source_task_id": matched_src,
        "progress_percent": progress,
        "status": status,
        "delay_days": delay_days,
        "delay_reason": delay_reason,
        "actual_start_date": start_d,
        "actual_end_date": end_d,
        "confidence_score": comp_conf,
        "match_confidence": raw_conf,
        "extraction_confidence": ext_conf,
        "is_ambiguous": is_ambiguous,
        "confidence_tier": tier,
        "reasoning": meta.get("reasoning", ""),
        "location_match_score": loc_score,
    }


class MockAIProvider(AIProvider):
    """Deterministic keyword and activity-similarity analyser used when no real API is configured.

    Implements calibrated confidence tiers:
    - High confidence (>= 80.0): auto-link eligible
    - Medium confidence (50.0 – 79.9): planner review with suggested match / ambiguity flag
    - Low confidence (< 50.0): unmatched review; matched_task_id = None

    Applies domain alias normalization before matching, so "WPA", "ROW", "excvation", etc.
    are recognized correctly.
    """

    def analyse(
        self,
        raw_update: str,
        location: str,
        reported_on: str,
        candidate_tasks: list[dict[str, Any]],
    ) -> AnalysisResult:
        # Check if update contains multiple distinct activity clauses / sentences
        clauses = _split_into_activity_clauses(raw_update)
        tasks_with_activity_match = [
            t["id"] for t in candidate_tasks
            if _has_activity_keyword_match(t, raw_update)
        ]

        if len(clauses) > 1 and len(tasks_with_activity_match) >= 2:
            parsed_clauses = [
                _parse_activity_clause(c, location, candidate_tasks)
                for c in clauses
            ]
            activity_clauses = [
                p for p in parsed_clauses
                if p["matched_task_id"] is not None
                or p["progress_percent"] is not None
                or (p["status"] is not None and len(p["clause_text"]) > 10)
            ]
            matched_tasks = [p["matched_task_id"] for p in activity_clauses if p["matched_task_id"] is not None]
            unique_tasks = set(matched_tasks)

            if len(unique_tasks) >= 2 or len(activity_clauses) >= 2:
                p0 = activity_clauses[0]
                additional_obs: list[ActivityObservation] = []
                for p in activity_clauses[1:]:
                    obs = ActivityObservation(
                        activity_description=p["clause_text"],
                        location_mentioned=location,
                        progress_percent=p["progress_percent"],
                        status=p["status"],
                        delay_days=p["delay_days"],
                        delay_reason=p["delay_reason"],
                        extraction_confidence=p["extraction_confidence"],
                        candidate_source_task_id=p["matched_source_task_id"],
                        actual_start_date=p["actual_start_date"],
                        actual_end_date=p["actual_end_date"],
                        match_confidence=p["match_confidence"],
                        is_ambiguous=p["is_ambiguous"],
                        reasoning=p["reasoning"],
                        raw_json=p,
                        model_response=p,
                    )
                    additional_obs.append(obs)

                raw_add_obs = [
                    {
                        "candidate_source_task_id": o.candidate_source_task_id,
                        "activity": o.activity_description,
                        "location": o.location_mentioned,
                        "keyword_score": o.match_confidence,
                        "progress_percent": o.progress_percent,
                        "status": o.status,
                        "extraction_confidence": o.extraction_confidence,
                        "match_confidence": o.match_confidence,
                        "is_ambiguous": o.is_ambiguous,
                        "reasoning": o.reasoning,
                    }
                    for o in additional_obs
                ]

                model_response: dict[str, Any] = {
                    "provider": _MOCK_MODEL_NAME,
                    "text_analysed": raw_update[:500],
                    "matched_source_task_id": p0["matched_source_task_id"],
                    "confidence_tier": p0["confidence_tier"],
                    "is_ambiguous": p0["is_ambiguous"],
                    "reasoning": p0["reasoning"],
                    "extraction_confidence": p0["extraction_confidence"],
                    "match_confidence": p0["match_confidence"],
                    "composite_confidence": p0["confidence_score"],
                    "location_match_score": p0["location_match_score"],
                    "additional_observations": raw_add_obs,
                    "note": "Deterministic multi-activity clause matching and confidence tiering.",
                }

                return AnalysisResult(
                    matched_task_id=p0["matched_task_id"],
                    progress_percent=p0["progress_percent"],
                    status=p0["status"],
                    delay_days=p0["delay_days"],
                    delay_reason=p0["delay_reason"],
                    actual_start_date=p0["actual_start_date"],
                    actual_end_date=p0["actual_end_date"],
                    confidence_score=p0["confidence_score"],
                    model_name=_MOCK_MODEL_NAME,
                    model_response=model_response,
                    additional_observations=additional_obs,
                )

        # Normalize the raw update text with both activity and location aliases
        norm_update = normalize_with_aliases(raw_update, _ALL_ALIAS_PAIRS)
        text_lower = norm_update.lower()

        # --- Task matching with confidence tiers ---
        matched_task_id, raw_confidence, match_meta = evaluate_task_matches(
            candidate_tasks=candidate_tasks,
            raw_update=norm_update,  # use normalized text for matching
            location=location,
        )

        # Compute location match score for composite confidence
        loc_lower = location.strip().lower()
        loc_score = 0
        if matched_task_id:
            matched_task = next((t for t in candidate_tasks if t["id"] == matched_task_id), None)
            if matched_task:
                loc_score = _location_match_score(
                    matched_task.get("location", "").lower(),
                    loc_lower,
                    raw_update.lower(),
                )

        # Estimate extraction confidence: higher if update has explicit % or clear keywords
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_update)
        has_explicit_percent = pct_match is not None
        has_status_keyword = any(k in text_lower for k in _STATUS_KEYWORDS)
        has_candidates = len(candidate_tasks) > 0

        if not has_candidates:
            # No tasks in schedule at all — very low extraction usefulness
            extraction_conf = 15.0
        elif has_explicit_percent and has_status_keyword:
            extraction_conf = 90.0
        elif has_explicit_percent or has_status_keyword:
            extraction_conf = 75.0
        elif len(raw_update.strip()) < 20:
            extraction_conf = 40.0
        else:
            extraction_conf = 70.0

        composite_confidence = compute_composite_confidence(
            llm_confidence=raw_confidence,
            llm_extraction_confidence=extraction_conf,
            deterministic_score=raw_confidence,   # for mock, these are the same
            location_match_score=loc_score,
        )

        # Enforce tier consistency: if composite falls below 50, clear matched_task_id
        if composite_confidence < 50.0:
            matched_task_id = None
            match_meta["matched_source_task_id"] = None
            match_meta["confidence_tier"] = "low"

        # --- Status (INFERENCE from keywords) ---
        status: str | None = None
        for keyword, mapped_status in _STATUS_KEYWORDS.items():
            if keyword in text_lower:
                status = mapped_status
                break

        # --- Progress percent (FACT: only from explicit "X%" pattern) ---
        progress_percent: float | None = None
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

        # --- Detect multiple activities mentioned (multi-activity readiness) ---
        additional_observations = _detect_additional_activities(raw_update, candidate_tasks, location)
        typed_additional = _dicts_to_activity_observations(additional_observations)

        # --- Build result ---
        model_response: dict[str, Any] = {
            "provider": _MOCK_MODEL_NAME,
            "text_analysed": raw_update[:500],
            "normalized_text": norm_update[:500] if norm_update != raw_update else None,
            "matched_source_task_id": match_meta.get("matched_source_task_id"),
            "confidence_tier": match_meta.get("confidence_tier"),
            "is_ambiguous": match_meta.get("is_ambiguous", False),
            "reasoning": match_meta.get("reasoning", ""),
            "extraction_confidence": extraction_conf,
            "match_confidence": raw_confidence,
            "composite_confidence": composite_confidence,
            "location_match_score": loc_score,
            "matched_keywords": {
                "status_keyword": next(
                    (k for k in _STATUS_KEYWORDS if k in text_lower), None
                ),
                "percent_pattern_found": has_explicit_percent,
                "delay_found": delay_days is not None,
                "start_date_found": actual_start_date is not None,
                "end_date_found": actual_end_date is not None,
            },
            "additional_observations": additional_observations,
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
            confidence_score=composite_confidence,
            model_name=_MOCK_MODEL_NAME,
            model_response=model_response,
            additional_observations=typed_additional,
        )


def _detect_additional_activities(
    raw_update: str,
    candidate_tasks: list[dict[str, Any]],
    location: str,
) -> list[dict[str, Any]]:
    """Detect mentions of additional activities beyond the primary match.

    Used for multi-activity extraction readiness (Phase 1).
    Returns a list of activity observations that scored >= 50.0 but were
    not selected as the primary match.

    This is a best-effort heuristic scan; the Gemini provider does this
    more accurately via the structured prompt.
    """
    if len(candidate_tasks) < 2:
        return []

    norm_raw = normalize_with_aliases(raw_update, _ALL_ALIAS_PAIRS)
    scored: list[tuple[float, dict[str, Any]]] = []
    for t in candidate_tasks:
        score = score_candidate_task(t, norm_raw, location)
        # Require score >= 55.0 so location-only fallback scores (<= 50.0 without
        # keyword overlap) are not falsely treated as additional activity mentions
        if score >= 55.0:
            scored.append((score, t))


    if len(scored) < 2:
        return []

    # Sort and skip the top (primary) match; return the rest as observations
    scored.sort(key=lambda x: -x[0])
    top_task = scored[0][1]
    top_act = normalize_with_aliases(str(top_task.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()
    top_words = set([w for w in re.findall(r"\w+", top_act) if len(w) > 2 and w not in _STOP_WORDS])

    additional: list[dict[str, Any]] = []
    for score, task in scored[1:]:
        sec_act = normalize_with_aliases(str(task.get("activity", "")), _ALL_ALIAS_PAIRS).strip().lower()
        sec_words = set([w for w in re.findall(r"\w+", sec_act) if len(w) > 2 and w not in _STOP_WORDS])
        if sec_words and sec_words.issubset(top_words):
            continue
        # Check for progress in raw_update (simple scan)
        pct_m = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_update)
        obs: dict[str, Any] = {
            "candidate_source_task_id": task.get("source_task_id"),
            "activity": task.get("activity"),
            "location": task.get("location"),
            "keyword_score": round(score, 1),
            "progress_percent": float(pct_m.group(1)) if pct_m else None,
            "note": "Secondary activity mention detected by keyword scan.",
        }
        additional.append(obs)

    return additional


# Allowed status values as per the DB CHECK constraint
_ALLOWED_STATUSES = frozenset(
    {"not_started", "in_progress", "completed", "delayed", "blocked"}
)


def _dicts_to_activity_observations(
    obs_dicts: list[dict[str, Any]],
) -> list[ActivityObservation]:
    """Convert raw observation dicts (Mock or Gemini) to typed ActivityObservation list.

    Phase 2 bridge: the untyped dicts are kept in model_response for audit trail;
    this typed list is stored on AnalysisResult for independent persistence.
    Tolerates missing/malformed fields - a bad entry is skipped safely.
    """
    result: list[ActivityObservation] = []
    for d in obs_dicts:
        if not isinstance(d, dict):
            continue
        # Handle field name differences between Mock and Gemini providers.
        activity_desc = (
            _safe_str(d.get("activity_description"))
            or _safe_str(d.get("activity"))
            or "Unknown activity"
        )
        candidate_id = _safe_str(
            d.get("candidate_source_task_id") or d.get("source_task_id")
        )
        progress = _safe_float(d.get("progress_percent"))
        status_raw = _safe_str(d.get("status"))
        status_val = status_raw if status_raw in _ALLOWED_STATUSES else None
        delay = _safe_int(d.get("delay_days"))
        delay_r = _safe_str(d.get("delay_reason"))
        ext_conf_raw = d.get("extraction_confidence") or d.get("keyword_score") or 50.0
        ext_conf = max(0.0, min(100.0, float(ext_conf_raw)))
        loc = _safe_str(d.get("location") or d.get("location_mentioned"))

        start_d = _safe_iso_date(d.get("actual_start_date"))
        end_d = _safe_iso_date(d.get("actual_end_date"))
        match_c = _safe_float(d.get("match_confidence"))
        if match_c is not None:
            match_c = max(0.0, min(100.0, match_c))
        is_amb = bool(d.get("is_ambiguous", False))
        reas = _safe_str(d.get("reasoning"))

        result.append(
            ActivityObservation(
                activity_description=activity_desc,
                location_mentioned=loc,
                progress_percent=progress,
                status=status_val,
                delay_days=delay if delay is not None and delay >= 0 else None,
                delay_reason=delay_r,
                extraction_confidence=ext_conf,
                candidate_source_task_id=candidate_id,
                actual_start_date=start_d,
                actual_end_date=end_d,
                match_confidence=match_c,
                is_ambiguous=is_amb,
                reasoning=reas,
                raw_json=dict(d),
                model_response=dict(d),
            )
        )
    return result


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

_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

_SYSTEM_PROMPT = """\
You are an expert construction-project analyst specialising in oil & gas pipeline, well pad, and compressor station projects.

You will be given:
1. A raw site update from a field supervisor or engineer (may be informal, abbreviated, or contain typos).
2. A list of planned schedule tasks with their IDs, activity names, locations, and dates.

Your job is to:
PHASE 1 — EXTRACT what the update says (extraction).
PHASE 2 — MATCH it to the most appropriate schedule task (matching).

Return a single JSON object with ALL of the following fields:

=== EXTRACTION FIELDS ===

- progress_percent (number 0-100 or null):
    FACT ONLY. Extract if the update explicitly states a percentage for the work.
    Approximation qualifiers like "around", "approximately", "roughly", "about" are ACCEPTABLE
    (e.g., "around 40% complete" → 40, "roughly half done" → 50).
    "almost done" or "nearly complete" → null (no number stated).
    Do NOT estimate or invent a number. Set to null when no percentage figure is mentioned.
    Exception: if status is "completed" or "signed off" with no percentage stated, set to 100.

- status (string or null):
    One of ONLY: "not_started", "in_progress", "completed", "delayed", "blocked".
    INFERENCE from the language of the update is acceptable here.
    Examples: "guys finished" → "completed", "delayed by rain" → "delayed", "paused" → "blocked".
    Set to null if you cannot determine with reasonable confidence.

- delay_days (integer >= 0 or null):
    FACT ONLY. Set only if the update explicitly states a number of days of delay.
    "two-day delay" → 2. "delayed by rain" → null (no count stated). Do NOT estimate.

- delay_reason (string or null):
    INFERENCE acceptable. Short phrase describing why the delay/blockage occurred.
    Set to null if status is not "delayed" or "blocked", or no reason can be inferred.

- actual_start_date (string "YYYY-MM-DD" or null):
    FACT ONLY. Set only if the update explicitly states that work started on a specific date.
    "started today" or "started yesterday" → null (relative dates are not extractable as facts).

- actual_end_date (string "YYYY-MM-DD" or null):
    FACT ONLY. Set only if the update explicitly states completion on a specific date.

- extraction_confidence (number 0-100):
    How clearly and explicitly the update stated extractable facts.
    90-100: explicit percentages, dates, and status clearly stated.
    60-89: status inferred from clear language, some facts stated.
    30-59: vague language, significant inference needed.
    0-29: update is ambiguous, unrelated, or unintelligible.

=== MATCHING FIELDS ===

- matched_source_task_id (string or null):
    The source_task_id of the planned task this update most likely refers to.
    Use ALL available signals: activity type, location match, date proximity, activity state.
    Set to null if no task is a plausible match (e.g., "lunch break at 1pm").

- match_confidence (number 0-100):
    Your confidence specifically in the task identification match.
    90-100: unambiguous match — exact activity name match, or strong activity and location alignment.
    70-89: clear match with minor uncertainty (e.g., location inferred from text, synonym used).
    50-69: plausible match but some signals are missing or ambiguous.
    20-49: weak match, only one signal aligns.
    0-19: no real match found.

- confidence_score (number 0-100):
    Overall confidence: set this equal to min(extraction_confidence, match_confidence).
    Both extraction AND matching must be confident for this to be high.

- reasoning (string):
    1-3 sentences explaining your match decision and what facts/inferences you made.
    Be explicit: state which signals (activity keywords, location, date) drove the decision.
    Always mark extractions as FACT or INFERENCE.

=== MULTI-ACTIVITY FIELD ===

- additional_observations (array or null):
    If the update mentions more than one distinct schedule activity, list the additional
    observations here (beyond the primary matched one).
    The FIRST mentioned activity in the update MUST be the primary match above;
    any subsequent activities mentioned in the text are listed here in order.
    For each additional activity:
    {
        "candidate_source_task_id": "...",  // best candidate task ID, or null if unmatched
        "activity_description": "...",       // exact phrase or clause from the update
        "progress_percent": <number 0-100 or null>,
        "status": <string or null>,         // "not_started", "in_progress", "completed", "delayed", "blocked"
        "actual_start_date": <string "YYYY-MM-DD" or null>,
        "actual_end_date": <string "YYYY-MM-DD" or null>,
        "extraction_confidence": <number 0-100>,
        "match_confidence": <number 0-100>,
        "reasoning": "..."                  // 1 sentence explaining the match and facts extracted
    }
    Set to null or empty array if only one activity is discussed.

=== CRITICAL RULES ===
1. NEVER invent dates, percentages, or delay counts.
2. If a value is UNKNOWN, set it to null. Do not guess.
3. Return ONLY valid JSON. No markdown fences, no extra text outside the JSON.
4. actual_start_date and actual_end_date must be in YYYY-MM-DD format or null.
5. Do NOT assume one update is about only one activity if multiple are mentioned.
6. "almost done", "nearly complete", "almost finished" → status="in_progress", progress_percent=null.
7. If the update is completely unrelated to any construction activity (e.g., "lunch break"), set matched_source_task_id=null and confidence_score=5.

=== DOMAIN VOCABULARY ===
Common abbreviations and informal terms (apply contextually, not blindly):
- ROW / RoW → Right of Way Clearance
- WPA / WP-A → Well Pad A location
- WPB / WP-B → Well Pad B location
- CSB → Compressor Station B
- NDT → Non-Destructive Testing (weld inspection)
- hydro test / hydrotest → Hydrostatic Pressure Test
- excvation / exvacation → Excavation Work (typo)
- site prep → Site Preparation
- pipe lay / stringing → Pipeline Laying
- signed off / sign off → completed status

=== FEW-SHOT EXAMPLES ===

Example 1 — Informal language, completed:
Update: "guys finished the welding today at well pad A"
Expected output:
{
  "matched_source_task_id": "<welding task ID at Well Pad A>",
  "progress_percent": 100,
  "status": "completed",
  "delay_days": null,
  "delay_reason": null,
  "actual_start_date": null,
  "actual_end_date": null,
  "extraction_confidence": 80,
  "match_confidence": 85,
  "confidence_score": 80,
  "reasoning": "INFERENCE: 'finished' indicates completed status, 100% assumed. Location 'well pad A' directly matches. Activity 'welding' is an unambiguous match to the Welding task.",
  "additional_observations": null
}

Example 2 — Abbreviations + partial progress:
Update: "ROW clearing 80% done, WPA"
Expected output:
{
  "matched_source_task_id": "<ROW task ID>",
  "progress_percent": 80,
  "status": "in_progress",
  "delay_days": null,
  "delay_reason": null,
  "actual_start_date": null,
  "actual_end_date": null,
  "extraction_confidence": 85,
  "match_confidence": 80,
  "confidence_score": 80,
  "reasoning": "FACT: 80% explicitly stated. INFERENCE: in_progress from partial completion. ROW → Right of Way Clearance; WPA → Well Pad A location.",
  "additional_observations": null
}

Example 3 — Typo, completed:
Update: "excvation work complet at WPA"
Expected output:
{
  "matched_source_task_id": "<excavation task ID at Well Pad A>",
  "progress_percent": 100,
  "status": "completed",
  "delay_days": null,
  "delay_reason": null,
  "actual_start_date": null,
  "actual_end_date": null,
  "extraction_confidence": 75,
  "match_confidence": 82,
  "confidence_score": 75,
  "reasoning": "INFERENCE: 'excvation' is a typo for excavation, 'complet' indicates completed. Location WPA → Well Pad A. Matched to Excavation task at that location.",
  "additional_observations": null
}

Example 4 — Delay with count:
Update: "ROW clearing delayed by 2 days due to rain"
Expected output:
{
  "matched_source_task_id": "<ROW task ID>",
  "progress_percent": null,
  "status": "delayed",
  "delay_days": 2,
  "delay_reason": "rain",
  "actual_start_date": null,
  "actual_end_date": null,
  "extraction_confidence": 90,
  "match_confidence": 80,
  "confidence_score": 80,
  "reasoning": "FACT: 2-day delay explicitly stated. FACT: rain is the stated reason. ROW → Right of Way Clearance. No progress percentage stated.",
  "additional_observations": null
}

Example 5 — Unrelated input (no match):
Update: "lunch break at 1pm"
Expected output:
{
  "matched_source_task_id": null,
  "progress_percent": null,
  "status": null,
  "delay_days": null,
  "delay_reason": null,
  "actual_start_date": null,
  "actual_end_date": null,
  "extraction_confidence": 5,
  "match_confidence": 0,
  "confidence_score": 5,
  "reasoning": "Update contains no reference to any scheduled construction activity. Cannot be matched.",
  "additional_observations": null
}

Example 6 — Multi-activity:
Update: "Foundation reinforcement completed at 100%. Equipment foundation concrete reached 60% completion."
Expected output:
{
  "matched_source_task_id": "<foundation reinforcement task ID>",
  "progress_percent": 100,
  "status": "completed",
  "delay_days": null,
  "delay_reason": null,
  "actual_start_date": null,
  "actual_end_date": null,
  "extraction_confidence": 95,
  "match_confidence": 95,
  "confidence_score": 95,
  "reasoning": "FACT: Foundation reinforcement completed at 100%. Unambiguous exact match to Foundation Reinforcement task.",
  "additional_observations": [
    {
      "candidate_source_task_id": "<equipment foundation concrete task ID>",
      "activity_description": "Equipment foundation concrete reached 60% completion",
      "progress_percent": 60,
      "status": "in_progress",
      "actual_start_date": null,
      "actual_end_date": null,
      "extraction_confidence": 95,
      "match_confidence": 95,
      "reasoning": "FACT: 60% completion stated. Exact match to Equipment Foundation Concrete task."
    }
  ]
}
"""


class GeminiAIProvider(AIProvider):
    """AI provider backed by Google Gemini via the google-genai SDK.

    Reads GEMINI_API_KEY from the environment.
    Requests structured JSON output from the model.
    Never invents values: unknown fields are left as None.

    Improvements over baseline:
    - Applies domain alias normalization before building the prompt.
    - Improved system prompt with few-shot examples, dual confidence schema,
      multi-activity extraction, and explicit FACT/INFERENCE/UNKNOWN guidance.
    - Computes composite confidence from LLM + deterministic signals.
    - Returns matched_task_id = None when composite confidence < 50.0.
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
        # Apply alias normalization to update text and location before prompt
        norm_update = normalize_with_aliases(raw_update, _ALL_ALIAS_PAIRS)
        norm_location = normalize_with_aliases(location, _LOCATION_ALIAS_PAIRS)

        # Build the user message
        if candidate_tasks:
            task_lines = "\n".join(
                f"  - source_task_id={t['source_task_id']}, "
                f"activity={normalize_with_aliases(t['activity'], _ACTIVITY_ALIAS_PAIRS)}, "
                f"location={normalize_with_aliases(t.get('location',''), _LOCATION_ALIAS_PAIRS)}, "
                f"planned_start={t.get('planned_start', '?')}, "
                f"planned_end={t.get('planned_end', '?')}"
                for t in candidate_tasks
            )
            tasks_section = f"CANDIDATE SCHEDULE TASKS (shortlisted by location/activity/date):\n{task_lines}"
        else:
            tasks_section = "CANDIDATE SCHEDULE TASKS: none found for this location/update"

        user_message = (
            f"SITE UPDATE\n"
            f"Location (reported): {location}\n"
            f"Location (normalized): {norm_location}\n"
            f"Reported on: {reported_on}\n"
            f"Original update text:\n{raw_update}\n"
            f"Normalized update text:\n{norm_update}\n\n"
            f"{tasks_section}"
        )

        # Build source_task_id → id map for resolving the matched task UUID
        task_id_map: dict[str, str] = {
            t["source_task_id"]: t["id"] for t in candidate_tasks
        }
        # Deterministic score for composite confidence (use original location, not re-normalized)
        task_det_scores: dict[str, float] = {
            t["source_task_id"]: score_candidate_task(t, norm_update, location)
            for t in candidate_tasks
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
                    max_output_tokens=3000,  # increased for multi-activity + reasoning
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

        # --- Step 3: resolve task UUID ---
        matched_source = _safe_str(parsed.get("matched_source_task_id"))
        matched_task_uuid: str | None = None
        if matched_source and matched_source in task_id_map:
            matched_task_uuid = task_id_map[matched_source]

        # --- Step 4: validate extracted fields ---
        raw_status = _safe_str(parsed.get("status"))
        status: str | None = raw_status if raw_status in _ALLOWED_STATUSES else None

        progress = _safe_float(parsed.get("progress_percent"))
        if progress is not None and not (0.0 <= progress <= 100.0):
            progress = None

        delay_days = _safe_int(parsed.get("delay_days"))
        if delay_days is None:
            delay_days = _extract_delay_days(raw_update)
        elif delay_days < 0:
            delay_days = None

        if delay_days and delay_days > 0 and status is None:
            status = "delayed"

        actual_start = _safe_iso_date(parsed.get("actual_start_date"))
        actual_end = _safe_iso_date(parsed.get("actual_end_date"))

        # --- Step 5: compute composite confidence ---
        llm_confidence = _safe_float(parsed.get("confidence_score")) or 0.0
        llm_confidence = max(0.0, min(100.0, llm_confidence))

        llm_extraction_conf = _safe_float(parsed.get("extraction_confidence")) or llm_confidence
        llm_extraction_conf = max(0.0, min(100.0, llm_extraction_conf))

        llm_match_conf = _safe_float(parsed.get("match_confidence")) or llm_confidence
        llm_match_conf = max(0.0, min(100.0, llm_match_conf))

        # Deterministic score for the matched task (or best available)
        det_score = 0.0
        loc_match_score = 0
        if matched_source:
            det_score = task_det_scores.get(matched_source, 0.0)
            if matched_task_uuid:
                matched_task = next((t for t in candidate_tasks if t["id"] == matched_task_uuid), None)
                if matched_task:
                    # Pass raw location (not re-normalized) to avoid double-normalization bug
                    loc_match_score = _location_match_score(
                        matched_task.get("location", "").lower(),
                        location.lower(),        # original, not norm_location
                        raw_update.lower(),      # original raw text
                    )
        elif task_det_scores:
            # No match from LLM — use deterministic fallback as hint
            det_score = 0.0


        composite = compute_composite_confidence(
            llm_confidence=llm_match_conf,
            llm_extraction_confidence=llm_extraction_conf,
            deterministic_score=det_score,
            location_match_score=loc_match_score,
        )

        # Enforce tier: if composite < 50, no confirmed match
        if composite < 50.0:
            matched_task_uuid = None
            matched_source = None

        # --- Step 6: multi-activity additional observations ---
        additional_obs_raw = parsed.get("additional_observations")
        if not isinstance(additional_obs_raw, list):
            additional_obs_raw = []
        typed_additional = _dicts_to_activity_observations(additional_obs_raw)

        return AnalysisResult(
            matched_task_id=matched_task_uuid,
            progress_percent=progress,
            status=status,
            delay_days=delay_days,
            delay_reason=_safe_str(parsed.get("delay_reason")),
            actual_start_date=actual_start,
            actual_end_date=actual_end,
            confidence_score=composite,
            model_name=_GEMINI_MODEL,
            model_response={
                "raw_json": parsed,
                "matched_source_task_id": matched_source,
                "reasoning": _safe_str(parsed.get("reasoning")),
                "extraction_confidence": llm_extraction_conf,
                "match_confidence": llm_match_conf,
                "composite_confidence": composite,
                "deterministic_score": det_score,
                "location_match_score": loc_match_score,
                "additional_observations": additional_obs_raw,
            },
            additional_observations=typed_additional,
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
