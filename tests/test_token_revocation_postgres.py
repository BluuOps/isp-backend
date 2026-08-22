from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.token_revocation import cleanup_expired_revocations


DATABASE_URL = os.getenv("RADIUSFIBER_DISPOSABLE_TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "disposable PostgreSQL URL is not configured")
class PostgreSQLRevocationCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DATABASE_URL)
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM auth_token_revocations"))

    def _seed(self):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO auth_token_revocations
                        (jti_hash, principal_type, subject_id, revoked_at, expires_at, reason)
                    VALUES
                        (:first, 'customer', '1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '3 minutes', 'test'),
                        (:second, 'customer', '2', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '2 minutes', 'test'),
                        (:third, 'customer', '3', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '1 minute', 'test'),
                        (:valid, 'customer', '4', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour', 'test')
                    """
                ),
                {
                    "first": "1" * 64,
                    "second": "2" * 64,
                    "third": "3" * 64,
                    "valid": "4" * 64,
                },
            )

    def _hashes(self):
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    text("SELECT jti_hash FROM auth_token_revocations ORDER BY jti_hash")
                ).scalars()
            )

    def test_bounded_runs_remove_oldest_expired_rows_and_preserve_valid_row(self):
        self._seed()
        with self.Session.begin() as db:
            self.assertEqual(cleanup_expired_revocations(db, batch_size=2), 2)
        self.assertEqual(self._hashes(), ["3" * 64, "4" * 64])

        with self.Session.begin() as db:
            self.assertEqual(cleanup_expired_revocations(db, batch_size=2), 1)
        self.assertEqual(self._hashes(), ["4" * 64])

        with self.Session.begin() as db:
            self.assertEqual(cleanup_expired_revocations(db, batch_size=2), 0)
        self.assertEqual(self._hashes(), ["4" * 64])


if __name__ == "__main__":
    unittest.main()
