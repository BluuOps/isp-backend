"""Validate the ephemeral CI-only 0016/0017 PostgreSQL migration chain."""

from __future__ import annotations

import os

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url


BASE_REVISION = "0016_staging_uat_fixtures"
HEAD_REVISION = "0017_organization_admin_invitations"


def _validated_disposable_url() -> str:
    raw = os.environ["RADIUSFIBER_DISPOSABLE_TEST_DATABASE_URL"]
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("CI migration validation requires PostgreSQL")
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("CI migration validation refuses a non-loopback database")
    if url.database != "radiusfiber_validation" or url.username != "validation":
        raise RuntimeError("CI migration validation refuses an unexpected database identity")
    return raw


def main() -> None:
    database_url = _validated_disposable_url()
    if os.environ.get("DATABASE_URL") != database_url:
        raise RuntimeError("DATABASE_URL must exactly match the validated disposable test URL")
    # Import application settings and metadata only after the target is proven disposable.
    from app.database import Base
    from app.models import BillingAccount, Customer, RadCheck

    def protected_counts(connection) -> tuple[int, int, int]:
        return (
            connection.scalar(select(func.count()).select_from(Customer.__table__)),
            connection.scalar(select(func.count()).select_from(BillingAccount.__table__)),
            connection.scalar(select(func.count()).select_from(RadCheck.__table__)),
        )

    engine = create_engine(database_url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if heads != [HEAD_REVISION]:
        raise RuntimeError(f"expected one Alembic head {HEAD_REVISION}, got {heads}")
    # ``create_all`` builds head metadata but does not create Alembic's version
    # table. Stamp the shorter predecessor first, widen the bookkeeping column
    # exactly as 0017 does, then stamp head so the first downgrade materializes
    # a genuine 0016 schema for the forward/reverse rehearsal.
    command.stamp(config, BASE_REVISION)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"
        )
    command.stamp(config, HEAD_REVISION)
    command.downgrade(config, BASE_REVISION)
    with engine.connect() as connection:
        protected_before = protected_counts(connection)
    command.upgrade(config, HEAD_REVISION)
    command.downgrade(config, BASE_REVISION)
    command.upgrade(config, HEAD_REVISION)
    command.check(config)
    with engine.connect() as connection:
        protected_after = protected_counts(connection)
    if protected_after != protected_before:
        raise RuntimeError("protected customer, billing, or RADIUS data changed")
    print("MIGRATION_REHEARSAL=0016_to_0017_to_0016_to_0017_passed")
    print("ALEMBIC_HEADS=single")
    print("ALEMBIC_CHECK=clean")
    print("PROTECTED_DATA=unchanged")
    engine.dispose()


if __name__ == "__main__":
    main()
