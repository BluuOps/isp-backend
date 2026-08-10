from __future__ import annotations

import argparse
import json

from app.core.config import settings
from app.database import SessionLocal
from app.services.disconnect_adapter import configured_disconnect_adapter
from app.services.expiry_worker import expiry_metrics, process_next_disconnect_job, scan_expired_services


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="RadiusFiber subscription-expiry worker")
    commands = result.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--dry-run", action="store_true")
    scan.add_argument("--organization-id", type=int)
    commands.add_parser("process-one")
    commands.add_parser("cycle")
    commands.add_parser("metrics")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "scan":
        dry_run = args.dry_run or settings.expiry_worker_dry_run
        if not settings.expiry_worker_enabled and not dry_run:
            raise SystemExit("Expiry worker is disabled; only --dry-run is permitted")
        with SessionLocal() as db:
            summary = scan_expired_services(
                db,
                organization_id=args.organization_id,
                dry_run=dry_run,
            )
            db.commit()
        print(json.dumps(summary.__dict__, sort_keys=True))
        return 0
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
