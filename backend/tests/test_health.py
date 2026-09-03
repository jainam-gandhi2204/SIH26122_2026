"""Basic health endpoint tests that do not require real database credentials."""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.database import DatabaseNotConfiguredError
from app.main import database_health_check, health_check


class HealthCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_health_check(self) -> None:
        self.assertEqual(await health_check(), {"status": "ok"})


class DatabaseHealthCheckTests(unittest.TestCase):
    @patch("app.main.check_database_connection")
    def test_database_health_check_when_reachable(self, check_connection) -> None:
        self.assertEqual(
            database_health_check(), {"status": "ok", "database": "reachable"}
        )
        check_connection.assert_called_once_with()

    @patch(
        "app.main.check_database_connection",
        side_effect=DatabaseNotConfiguredError(),
    )
    def test_database_health_check_when_not_configured(self, _check_connection) -> None:
        with self.assertRaises(HTTPException) as context:
            database_health_check()

        self.assertEqual(context.exception.status_code, 503)
        self.assertEqual(context.exception.detail, "Database is not configured")
