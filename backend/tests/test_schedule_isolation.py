"""Regression tests for Current-Schedule Data Isolation (SIH26122).

Verifies that when the project schedule baseline is replaced:
1. Historical site updates and AI-processed records are NOT permanently deleted.
2. Old site updates and AI records do not appear in the active dashboard (status='active').
3. Historical records remain accessible for audit/history (status='historical' and status='all').
4. Newly imported schedules and newly created site updates appear normally in active views.
5. Isolation works generically without hardcoded task IDs or locations.
"""

from __future__ import annotations

import datetime
import json
import types
import unittest
from unittest.mock import MagicMock, call
import uuid

from app.importer import ParsedRow, import_to_db
from app.review_queue import get_review_queue
from app.site_updates import format_site_update, list_site_updates


class ScheduleDataIsolationTests(unittest.TestCase):
    """Test suite proving baseline schedule replacement isolates active views while preserving audit history."""

    def test_schedule_replacement_isolates_site_updates(self):
        """When replace=True is executed:
        - Active site_updates are archived (archived_at set)
        - Historical records are not deleted
        - Active query (status='active') excludes old updates
        - Historical query (status='historical') includes old updates
        - New updates appear in active query
        """
        db = MagicMock()

        # Step 1: Mock existing active site update from Old Project
        old_update_id = uuid.uuid4()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        yesterday_utc = now_utc - datetime.timedelta(days=1)

        old_site_update = types.SimpleNamespace(
            id=old_update_id,
            source_update_id="UPD-PROJ1-001",
            reported_on=datetime.date(2026, 9, 1),
            location="Compressor Station 1",
            raw_update="Foundation excavation completed for Compressor Station 1",
            ingested_at=yesterday_utc,
            source_reference="daily_log_p1.csv",
            archived_at=now_utc,  # simulate post-replacement state
        )

        new_site_update = types.SimpleNamespace(
            id=uuid.uuid4(),
            source_update_id="UPD-PROJ2-001",
            reported_on=datetime.date(2026, 10, 2),
            location="Solar Substation Alpha",
            raw_update="Inverter pad construction underway",
            ingested_at=now_utc,
            source_reference="daily_log_p2.csv",
            archived_at=None,  # active in new project
        )

        # Step 2: Test import_to_db(replace=True) archives site updates
        new_project_rows = [
            ParsedRow("SUB-101", "Site Grading", "Solar Substation Alpha",
                      datetime.date(2026, 10, 1), datetime.date(2026, 10, 10), dependencies=[]),
            ParsedRow("SUB-102", "Inverter Pad", "Solar Substation Alpha",
                      datetime.date(2026, 10, 11), datetime.date(2026, 10, 20), dependencies=["SUB-101"]),
        ]

        import_to_db(db, new_project_rows, "new_baseline_schedule.csv", replace=True)

        executed_calls = db.execute.call_args_list
        sql_calls = [str(c[0][0]) for c in executed_calls]

        # Verify site_updates was updated with archived_at, NOT deleted
        self.assertTrue(
            any("UPDATE site_updates" in s and "SET archived_at = :archived_at" in s and "WHERE archived_at IS NULL" in s for s in sql_calls),
            "Expected site_updates to be archived with archived_at timestamp on schedule replacement",
        )
        self.assertFalse(
            any("DELETE FROM site_updates" in s for s in sql_calls),
            "Historical site_updates must NEVER be deleted on schedule replacement",
        )

        # Step 3: Test active query excludes archived updates
        db_query = MagicMock()
        # Active query returns only new_site_update (where archived_at IS NULL)
        db_query.execute.return_value.fetchall.return_value = [new_site_update]
        active_results = list_site_updates(db_query, status="active")
        self.assertEqual(len(active_results), 1)
        self.assertEqual(active_results[0]["source_update_id"], "UPD-PROJ2-001")
        self.assertIsNone(active_results[0]["archived_at"])
        active_sql = str(db_query.execute.call_args[0][0])
        self.assertIn("WHERE archived_at IS NULL", active_sql)

        # Step 4: Test historical query returns archived updates with timestamp
        db_query.execute.return_value.fetchall.return_value = [old_site_update]
        historical_results = list_site_updates(db_query, status="historical")
        self.assertEqual(len(historical_results), 1)
        self.assertEqual(historical_results[0]["source_update_id"], "UPD-PROJ1-001")
        self.assertEqual(historical_results[0]["archived_at"], now_utc.isoformat())
        hist_sql = str(db_query.execute.call_args[0][0])
        self.assertIn("WHERE archived_at IS NOT NULL", hist_sql)

        # Step 5: Test all query returns both for complete auditability
        db_query.execute.return_value.fetchall.return_value = [new_site_update, old_site_update]
        all_results = list_site_updates(db_query, status="all")
        self.assertEqual(len(all_results), 2)
        all_sql = str(db_query.execute.call_args[0][0])
        self.assertNotIn("WHERE", all_sql)

    def test_schedule_replacement_isolates_review_queue(self):
        """When schedule baseline is replaced, review items belonging to old project
        do not leak into active pending review queue.
        """
        db = MagicMock()

        old_review_row = types.SimpleNamespace(
            processed_id="rev-old-p1",
            site_update_id="site-old-p1",
            source_update_id="UPD-P1-009",
            reported_on="2026-08-20",
            location="Terminal 4",
            raw_update="Terminal piping hydrotest completed",
            source_reference="log.csv",
            archived_at="2026-09-01T00:00:00+00:00",
            matched_task_id=None,
            progress_percent=100.0,
            status="completed",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-08-15",
            actual_end_date="2026-08-20",
            confidence_score=50.0,
            model_name="mock-model",
            model_response=json.dumps({"review_status": "historical", "archived_reason": "Schedule baseline was replaced"}),
            processed_at="2026-08-20T12:00:00",
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
        )

        new_review_row = types.SimpleNamespace(
            processed_id="rev-new-p2",
            site_update_id="site-new-p2",
            source_update_id="UPD-P2-001",
            reported_on="2026-10-05",
            location="Solar Substation Alpha",
            raw_update="Inverter pad concrete pour 40% complete",
            source_reference="p2_log.csv",
            archived_at=None,
            matched_task_id=None,
            progress_percent=40.0,
            status="in_progress",
            delay_days=None,
            delay_reason=None,
            actual_start_date="2026-10-01",
            actual_end_date=None,
            confidence_score=45.0,
            model_name="mock-model",
            model_response=json.dumps({"review_status": "pending"}),
            processed_at="2026-10-05T14:00:00",
            matched_st_id=None,
            matched_source_task_id=None,
            matched_activity=None,
            matched_location=None,
            matched_planned_start=None,
            matched_planned_end=None,
        )

        db.execute.return_value.fetchall.side_effect = [
            [old_review_row, new_review_row],  # _REVIEW_QUEUE_QUERY
            [],                                 # _ALL_SCHEDULE_TASKS_QUERY
        ]

        # 1. Active pending queue: includes ONLY the new project update
        pending = get_review_queue(db, status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["review_id"], "rev-new-p2")
        self.assertEqual(pending[0]["source_update_id"], "UPD-P2-001")
        self.assertFalse(pending[0]["is_archived"])

        # 2. Historical review queue: includes ONLY the old project update
        db.execute.return_value.fetchall.side_effect = [
            [old_review_row, new_review_row],
            [],
        ]
        historical = get_review_queue(db, status="historical")
        self.assertEqual(len(historical), 1)
        self.assertEqual(historical[0]["review_id"], "rev-old-p1")
        self.assertTrue(historical[0]["is_archived"])

        # 3. All reviews: includes both for audit
        db.execute.return_value.fetchall.side_effect = [
            [old_review_row, new_review_row],
            [],
        ]
        all_reviews = get_review_queue(db, status="all")
        self.assertEqual(len(all_reviews), 2)


if __name__ == "__main__":
    unittest.main()

