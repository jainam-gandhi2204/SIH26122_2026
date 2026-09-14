"""Comprehensive manual test script running the 8 realistic inputs through the full AI pipeline."""
import sys
import json
sys.path.insert(0, ".")

from app.ai_processor import shortlist_candidates
from app.ai_provider import MockAIProvider

TASKS = [
    {"id": "t1", "source_task_id": "P101", "activity": "Right of Way Clearance",   "location": "Well Pad A",           "planned_start": "2026-08-01", "planned_end": "2026-08-20", "status": "in_progress"},
    {"id": "t2", "source_task_id": "P102", "activity": "Excavation Work",           "location": "Well Pad A",           "planned_start": "2026-08-21", "planned_end": "2026-09-10", "status": "in_progress"},
    {"id": "t3", "source_task_id": "P103", "activity": "Pipeline Laying",           "location": "Well Pad A",           "planned_start": "2026-09-11", "planned_end": "2026-09-30", "status": "not_started"},
    {"id": "t4", "source_task_id": "P104", "activity": "Welding",                  "location": "Well Pad A",           "planned_start": "2026-09-15", "planned_end": "2026-10-05", "status": "not_started"},
    {"id": "t5", "source_task_id": "P105", "activity": "Site Preparation",         "location": "Compressor Station B", "planned_start": "2026-08-15", "planned_end": "2026-09-05", "status": "completed"},
    {"id": "t6", "source_task_id": "P106", "activity": "Foundation Construction",  "location": "Compressor Station B", "planned_start": "2026-09-06", "planned_end": "2026-09-20", "status": "in_progress"},
]

provider = MockAIProvider()
REPORTED_ON = "2026-09-10"

INPUTS = [
    ("1", "guys finished the welding today at well pad A", "Well Pad A"),
    ("2", "ROW clearing 80% done, WPA", "Well Pad A"),
    ("3", "excvation work complet at WPA", "Well Pad A"),
    ("4", "pipeline laid + welding done at WPA section 2", "Well Pad A"),
    ("5", "lunch break at 1pm", ""),
    ("6", "ROW clearing delayed by 2 days due to rain", "Well Pad A"),
    ("7", "aaj excavation almost complete hai, kal bedding start karenge", "Well Pad A"),
    ("8", "excavation is complete, bedding is 80 percent and pipe stringing has started", "Well Pad A"),
]

print("=" * 80)
print("REALISTIC MANUAL TEST EXECUTION RESULTS (ACTUAL)")
print("=" * 80)

for num, text, loc in INPUTS:
    # 1. Candidate shortlisting
    candidates = shortlist_candidates(
        all_tasks=TASKS,
        raw_update=text,
        location=loc,
        reported_on=REPORTED_ON,
    )
    cand_ids = [f"{t['source_task_id']} ({t['activity']})" for t in candidates]

    # 2. Provider analysis
    r = provider.analyse(text, location=loc, reported_on=REPORTED_ON, candidate_tasks=candidates)

    # 3. Match resolution
    selected_match = "NONE"
    if r.matched_task_id:
        t = next((x for x in TASKS if x["id"] == r.matched_task_id), None)
        if t:
            selected_match = f"{t['source_task_id']} - {t['activity']} ({t['location']})"

    tier = r.model_response.get("confidence_tier", "low")
    auto_linked = (r.matched_task_id is not None and r.confidence_score >= 80.0)
    action = "AUTO-LINK & UPDATE SCHEDULE" if auto_linked else ("PLANNER REVIEW" if tier == "medium" else "UNMATCHED REVIEW")
    schedule_updated = "YES" if auto_linked else "NO"

    add_obs = r.model_response.get("additional_observations") or []

    print(f"INPUT {num}: {repr(text)}")
    print(f"  Reported Location : {repr(loc)}")
    print(f"  Shortlisted Cands : {', '.join(cand_ids[:4])}{'...' if len(cand_ids) > 4 else ''}")
    print(f"  Selected Match    : {selected_match}")
    print(f"  Confidence Score  : {r.confidence_score:.1f} (Tier: {tier.upper()})")
    print(f"  Routing Action    : {action}")
    print(f"  Schedule Updated  : {schedule_updated}")
    print(f"  Extracted Status  : {r.status}")
    print(f"  Extracted Progress: {r.progress_percent}%" if r.progress_percent is not None else "  Extracted Progress: None")
    print(f"  Extracted Delay   : {r.delay_days} days (Reason: {r.delay_reason})")
    print(f"  Extracted Dates   : Start={r.actual_start_date}, End={r.actual_end_date}")
    print(f"  Multi-Activity Obs: {len(add_obs)} secondary observation(s)")
    for i, obs in enumerate(add_obs, 1):
        print(f"    - Obs {i}: {obs.get('candidate_source_task_id')} | {obs.get('activity')} | score={obs.get('keyword_score')}")
    print("-" * 80)
