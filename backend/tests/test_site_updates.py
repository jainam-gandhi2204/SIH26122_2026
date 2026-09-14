"""Unit tests for the site updates ingestion layer (app/site_updates.py).

All tests are pure unit tests with mocks: no database connection or network needed.
"""

import datetime
import unittest
from unittest.mock import MagicMock, patch
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.main import (
    create_site_update as create_site_update_endpoint,
    list_site_updates as list_site_updates_endpoint,
)
from app.site_updates import (
    SiteUpdateCreate,
    create_site_update,
    format_site_update,
    list_site_updates,
)


def _make_mock_row(
    id_val: str | None = None,
    source_update_id: str = "U001",
    reported_on: datetime.date | str = datetime.date(2026, 9, 5),
    location: str = "Well Pad A",
    raw_update: str = "Site preparation completed successfully...",
    ingested_at: datetime.datetime | None = datetime.datetime(2026, 9, 5, 12, 0, 0, tzinfo=datetime.timezone.utc),
    source_reference: str | None = None,
    archived_at: datetime.datetime | None = None,
):
    """Return a mock object whose attributes match a site_updates row."""
    row = MagicMock()
    row.id = uuid.UUID(id_val) if id_val else uuid.uuid4()
    row.source_update_id = source_update_id
    row.reported_on = reported_on
    row.location = location
    row.raw_update = raw_update
    row.ingested_at = ingested_at
    row.source_reference = source_reference
    row.archived_at = archived_at
    return row


class SiteUpdateValidationTests(unittest.TestCase):
    """Tests for Pydantic schema validation of incoming site updates."""

    def test_valid_payload_creation(self):
        payload = SiteUpdateCreate(
            source_update_id="U001",
            reported_on="05-Sep-2026",
            location="Well Pad A",
            raw_update="Site preparation completed successfully...",
        )
        self.assertEqual(payload.source_update_id, "U001")
        self.assertEqual(payload.reported_on, "05-Sep-2026")
        self.assertEqual(payload.location, "Well Pad A")
        self.assertEqual(payload.raw_update, "Site preparation completed successfully...")
        self.assertIsNone(payload.source_reference)

    def test_whitespace_is_stripped(self):
        payload = SiteUpdateCreate(
            source_update_id="  U002  ",
            reported_on=" 05-Sep-2026 ",
            location="  Well Pad B  ",
            raw_update="  Foundation ongoing  ",
            source_reference="  daily_log_12.pdf  ",
        )
        self.assertEqual(payload.source_update_id, "U002")
        self.assertEqual(payload.reported_on, "05-Sep-2026")
        self.assertEqual(payload.location, "Well Pad B")
        self.assertEqual(payload.raw_update, "Foundation ongoing")
        self.assertEqual(payload.source_reference, "daily_log_12.pdf")

    def test_missing_required_fields(self):
        # Missing source_update_id
        with self.assertRaises(ValidationError) as ctx:
            SiteUpdateCreate(
                reported_on="05-Sep-2026",
                location="Well Pad A",
                raw_update="Update text",
            )
        self.assertIn("source_update_id", str(ctx.exception))

        # Missing reported_on
        with self.assertRaises(ValidationError) as ctx:
            SiteUpdateCreate(
                source_update_id="U001",
                location="Well Pad A",
                raw_update="Update text",
            )
        self.assertIn("reported_on", str(ctx.exception))

        # Missing location
        with self.assertRaises(ValidationError) as ctx:
            SiteUpdateCreate(
                source_update_id="U001",
                reported_on="05-Sep-2026",
                raw_update="Update text",
            )
        self.assertIn("location", str(ctx.exception))

        # Missing raw_update
        with self.assertRaises(ValidationError) as ctx:
            SiteUpdateCreate(
                source_update_id="U001",
                reported_on="05-Sep-2026",
                location="Well Pad A",
            )
        self.assertIn("raw_update", str(ctx.exception))

    def test_empty_string_fields_rejected(self):
        with self.assertRaises(ValidationError):
            SiteUpdateCreate(
                source_update_id="   ",
                reported_on="05-Sep-2026",
                location="Well Pad A",
                raw_update="Update text",
            )

        with self.assertRaises(ValidationError):
            SiteUpdateCreate(
                source_update_id="U001",
                reported_on="05-Sep-2026",
                location="",
                raw_update="Update text",
            )

        with self.assertRaises(ValidationError):
            SiteUpdateCreate(
                source_update_id="U001",
                reported_on="05-Sep-2026",
                location="Well Pad A",
                raw_update="   ",
            )

    def test_invalid_date_formats_rejected(self):
        invalid_dates = [
            "2026-09-05",       # ISO format (not DD-Mon-YYYY)
            "09/05/2026",       # slash format
            "05-09-2026",       # numeric month
            "invalid-date",     # gibberish
            "32-Sep-2026",      # out-of-range day
            "05-BadMonth-2026", # invalid month
        ]
        for bad_date in invalid_dates:
            with self.subTest(bad_date=bad_date):
                with self.assertRaises(ValidationError) as ctx:
                    SiteUpdateCreate(
                        source_update_id="U001",
                        reported_on=bad_date,
                        location="Well Pad A",
                        raw_update="Update text",
                    )
                self.assertIn("reported_on", str(ctx.exception))


class SiteUpdateDatabaseTests(unittest.TestCase):
    """Tests for database persistence and querying functions."""

    def test_create_site_update_success(self):
        mock_db = MagicMock()
        mock_row = _make_mock_row(
            source_update_id="U001",
            reported_on=datetime.date(2026, 9, 5),
            location="Well Pad A",
            raw_update="Site preparation completed successfully...",
        )
        mock_db.execute.return_value.fetchone.return_value = mock_row

        payload = SiteUpdateCreate(
            source_update_id="U001",
            reported_on="05-Sep-2026",
            location="Well Pad A",
            raw_update="Site preparation completed successfully...",
        )

        result = create_site_update(mock_db, payload)

        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()

        self.assertEqual(result["id"], str(mock_row.id))
        self.assertEqual(result["source_update_id"], "U001")
        self.assertEqual(result["reported_on"], "2026-09-05")
        self.assertEqual(result["location"], "Well Pad A")
        self.assertEqual(result["raw_update"], "Site preparation completed successfully...")
        self.assertIsNotNone(result["ingested_at"])

    def test_create_site_update_database_error(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError("connection failure", None, None)

        payload = SiteUpdateCreate(
            source_update_id="U001",
            reported_on="05-Sep-2026",
            location="Well Pad A",
            raw_update="Site preparation completed successfully...",
        )

        with self.assertRaises(OperationalError):
            create_site_update(mock_db, payload)

    def test_list_site_updates_empty(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []

        result = list_site_updates(mock_db)
        self.assertEqual(result, [])
        mock_db.execute.assert_called_once()

    def test_list_site_updates_returns_all(self):
        mock_db = MagicMock()
        row1 = _make_mock_row(source_update_id="U002", reported_on=datetime.date(2026, 9, 6))
        row2 = _make_mock_row(source_update_id="U001", reported_on=datetime.date(2026, 9, 5))
        mock_db.execute.return_value.fetchall.return_value = [row1, row2]

        result = list_site_updates(mock_db)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["source_update_id"], "U002")
        self.assertEqual(result[1]["source_update_id"], "U001")

    def test_list_site_updates_active_filters_archived(self):
        mock_db = MagicMock()
        row = _make_mock_row(source_update_id="U001", archived_at=None)
        mock_db.execute.return_value.fetchall.return_value = [row]

        result = list_site_updates(mock_db, status="active")
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["archived_at"])

        call_sql = str(mock_db.execute.call_args[0][0])
        self.assertIn("WHERE archived_at IS NULL", call_sql)

    def test_list_site_updates_historical(self):
        mock_db = MagicMock()
        archived_time = datetime.datetime(2026, 9, 8, 10, 0, 0, tzinfo=datetime.timezone.utc)
        row = _make_mock_row(source_update_id="U001", archived_at=archived_time)
        mock_db.execute.return_value.fetchall.return_value = [row]

        result = list_site_updates(mock_db, status="historical")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["archived_at"], archived_time.isoformat())

        call_sql = str(mock_db.execute.call_args[0][0])
        self.assertIn("WHERE archived_at IS NOT NULL", call_sql)

    def test_list_site_updates_all(self):
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = []

        result = list_site_updates(mock_db, status="all")
        self.assertEqual(result, [])

        call_sql = str(mock_db.execute.call_args[0][0])
        self.assertNotIn("WHERE", call_sql)

    def test_format_site_update_with_and_without_archived_at(self):
        row_active = _make_mock_row(archived_at=None)
        formatted_active = format_site_update(row_active)
        self.assertIsNone(formatted_active["archived_at"])

        archived_time = datetime.datetime(2026, 9, 9, 15, 30, 0, tzinfo=datetime.timezone.utc)
        row_archived = _make_mock_row(archived_at=archived_time)
        formatted_archived = format_site_update(row_archived)
        self.assertEqual(formatted_archived["archived_at"], archived_time.isoformat())


class SiteUpdateEndpointTests(unittest.TestCase):
    """Tests for the FastAPI endpoint functions with mock DB."""

    def test_create_endpoint_success(self):
        mock_db = MagicMock()
        mock_row = _make_mock_row()
        mock_db.execute.return_value.fetchone.return_value = mock_row

        payload = SiteUpdateCreate(
            source_update_id="U001",
            reported_on="05-Sep-2026",
            location="Well Pad A",
            raw_update="Site preparation completed successfully...",
        )

        response = create_site_update_endpoint(payload, db=mock_db)
        self.assertEqual(response["source_update_id"], "U001")
        self.assertEqual(response["id"], str(mock_row.id))

    def test_create_endpoint_database_error_raises_503(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError("connection lost", None, None)

        payload = SiteUpdateCreate(
            source_update_id="U001",
            reported_on="05-Sep-2026",
            location="Well Pad A",
            raw_update="Site preparation completed successfully...",
        )

        with self.assertRaises(HTTPException) as ctx:
            create_site_update_endpoint(payload, db=mock_db)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Database is unavailable")
        mock_db.rollback.assert_called_once()

    def test_list_endpoint_success(self):
        mock_db = MagicMock()
        mock_row = _make_mock_row()
        mock_db.execute.return_value.fetchall.return_value = [mock_row]

        response = list_site_updates_endpoint(db=mock_db)
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0]["source_update_id"], "U001")

    def test_list_endpoint_passes_status_filter(self):
        mock_db = MagicMock()
        row = _make_mock_row()
        mock_db.execute.return_value.fetchall.return_value = [row]

        response = list_site_updates_endpoint(status_filter="historical", db=mock_db)
        self.assertEqual(len(response), 1)
        call_sql = str(mock_db.execute.call_args[0][0])
        self.assertIn("WHERE archived_at IS NOT NULL", call_sql)

    def test_list_endpoint_database_error_raises_503(self):
        mock_db = MagicMock()
        mock_db.execute.side_effect = OperationalError("connection lost", None, None)

        with self.assertRaises(HTTPException) as ctx:
            list_site_updates_endpoint(db=mock_db)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Database is unavailable")


if __name__ == "__main__":
    unittest.main()
