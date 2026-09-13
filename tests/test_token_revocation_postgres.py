from __future__ import annotations

import hashlib
import os
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services.token_revocation import cleanup_expired_revocations, ensure_token_not_revoked


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
                        (:first, 'customer', '1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '10 minutes', 'test'),
                        (:second, 'customer', '2', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '9 minutes', 'test'),
                        (:third, 'customer', '3', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '8 minutes', 'test'),
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

    def test_clock_skew_retention_and_exact_cleanup_boundary(self):
        within_retention_jti = "database-ahead-within-supported-skew"
        hashes = {
            "before_expiry": "a" * 64,
            "at_expiry": "b" * 64,
            "within_retention": hashlib.sha256(within_retention_jti.encode("utf-8")).hexdigest(),
            "at_boundary": "d" * 64,
            "after_retention": "e" * 64,
        }
        with self.Session.begin() as db:
            db.execute(
                text(
                    """
                    INSERT INTO auth_token_revocations
                        (jti_hash, principal_type, subject_id, revoked_at, expires_at, reason)
                    VALUES
                        (:before_expiry, 'customer', 'before', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 second', 'test'),
                        (:at_expiry, 'customer', 'at-expiry', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'test'),
                        (:within_retention, 'customer', 'within', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '299 seconds', 'test'),
                        (:at_boundary, 'customer', 'boundary', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '300 seconds', 'test'),
                        (:after_retention, 'customer', 'after', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '301 seconds', 'test')
                    """
                ),
                hashes,
            )
            self.assertEqual(
                cleanup_expired_revocations(
                    db,
                    batch_size=100,
                    retention_seconds=300,
                ),
                2,
            )
            self.assertEqual(
                cleanup_expired_revocations(
                    db,
                    batch_size=100,
                    retention_seconds=300,
                ),
                0,
            )
            with self.assertRaises(HTTPException) as raised:
                ensure_token_not_revoked(
                    db,
                    {"jti": within_retention_jti},
                )
            self.assertEqual(raised.exception.status_code, 401)

        self.assertEqual(
            self._hashes(),
            sorted(
                [
                    hashes["before_expiry"],
                    hashes["at_expiry"],
                    hashes["within_retention"],
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
