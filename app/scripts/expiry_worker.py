from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from app.core.config import settings
from app.database import SessionLocal
from app.services.disconnect_adapter import configured_disconnect_adapter
from app.services.expiry_worker import expiry_metrics, process_next_disconnect_job, scan_expired_services


EXIT_SUCCESS = 0
EXIT_SELECTOR_INVALID = 2
EXIT_TARGET_CARDINALITY = 3
EXIT_SCAN_ERRORS = 4
EXIT_DATABASE_FAILURE = 5


def _scan_exit_code(summary, *, targeted: bool) -> int:
    if summary.errors:
        return EXIT_SCAN_ERRORS
    if summary.dry_run and summary.changed != 0:
        return EXIT_SCAN_ERRORS
    if targeted and (summary.matched != 1 or summary.evaluated != 1):
        return EXIT_TARGET_CARDINALITY
    if targeted and summary.newly_expired != 1:
        return EXIT_SCAN_ERRORS
    return EXIT_SUCCESS


def _failure_output(exc: Exception) -> dict[str, object]:
    return {
        "matched": 0,
        "evaluated": 0,
        "would_change": 0,
        "changed": 0,
        "disconnected": 0,
        "errors": 1,
        "error_type": type(exc).__name__,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="RadiusFiber subscription-expiry worker")
    commands = result.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--dry-run", action="store_true")
    scan.add_argument("--organization-id", type=int)
    scan.add_argument("--user-id", type=int)
    scan.add_argument("--username")
    commands.add_parser("process-one")
    commands.add_parser("cycle")
    commands.add_parser("metrics")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "scan":
        dry_run = args.dry_run or settings.expiry_worker_dry_run
        target_parts = (args.user_id is not None, args.username is not None)
        if any(target_parts) and not all(target_parts):
            parser().error("Targeted scan requires both --user-id and --username")
        if all(target_parts) and args.organization_id is None:
            parser().error("Targeted scan requires --organization-id")
        if not settings.expiry_worker_enabled and not dry_run:
            raise SystemExit("Expiry worker is disabled; only --dry-run is permitted")
        targeted = all(target_parts)
        try:
            with SessionLocal() as db:
                if dry_run and db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION READ ONLY"))
                summary = scan_expired_services(
                    db,
                    organization_id=args.organization_id,
                    user_id=args.user_id,
                    username=args.username,
                    dry_run=dry_run,
                )
                exit_code = _scan_exit_code(summary, targeted=targeted)
                if dry_run:
                    if db.new or db.dirty or db.deleted:
                        raise RuntimeError("dry-run attempted to mutate ORM state")
                    db.rollback()
                elif exit_code == EXIT_SUCCESS:
                    db.commit()
                else:
                    db.rollback()
        except Exception as exc:
            if "db" in locals():
                db.rollback()
            print(json.dumps(_failure_output(exc), sort_keys=True))
            return EXIT_DATABASE_FAILURE
        print(json.dumps(summary.__dict__, sort_keys=True))
        return exit_code
    if args.command == "process-one":
        if not settings.expiry_worker_enabled:
            raise SystemExit("Expiry worker is disabled")
        result = process_next_disconnect_job(SessionLocal, configured_disconnect_adapter())
        print(json.dumps(result.__dict__, sort_keys=True))
        return 0
    if args.command == "cycle":
        if not settings.expiry_worker_enabled:
            raise SystemExit("Expiry worker is disabled")
        with SessionLocal() as db:
            summary = scan_expired_services(db, dry_run=False)
            db.commit()
        adapter = configured_disconnect_adapter()
        processed = []
        for _ in range(settings.expiry_scan_batch_size):
            result = process_next_disconnect_job(SessionLocal, adapter)
            if result.status == "empty":
                break
            processed.append(result.__dict__)
        print(json.dumps({"scan": summary.__dict__, "processed": processed}, sort_keys=True))
        return 0
    with SessionLocal() as db:
        metrics = expiry_metrics(db)
    print(json.dumps(metrics, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
