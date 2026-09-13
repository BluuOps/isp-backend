from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from app.database import SessionLocal
from app.services.radius_authorization import MANUAL_DISCONNECT_REASON, RejectOwnershipConflict
from app.services.reject_reconciliation import ALLOWED_ADOPTION_REASONS, reconcile_legacy_reject


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Exact-row RADIUS reject ownership reconciliation")
    result.add_argument("--organization-id", type=int, required=True)
    result.add_argument("--user-id", type=int, required=True)
    result.add_argument("--username", required=True)
    result.add_argument("--radcheck-id", type=int, required=True)
    result.add_argument("--action", choices=("report", "adopt", "remove"), default="report")
    result.add_argument("--reason-code", choices=sorted(ALLOWED_ADOPTION_REASONS), default=MANUAL_DISCONNECT_REASON)
    result.add_argument("--apply", action="store_true")
    result.add_argument("--confirm-radcheck-id", type=int)
    result.add_argument("--evidence-reference")
    result.add_argument("--operator-id")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.apply and args.confirm_radcheck_id != args.radcheck_id:
        parser().error("--apply requires --confirm-radcheck-id to match --radcheck-id")
    try:
        with SessionLocal() as db:
            if not args.apply and db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
            report = reconcile_legacy_reject(
                db,
                organization_id=args.organization_id,
                user_id=args.user_id,
                username=args.username,
                radcheck_id=args.radcheck_id,
                action=args.action,
                reason_code=args.reason_code,
                evidence_reference=args.evidence_reference,
                operator_id=args.operator_id,
                apply=args.apply,
            )
            if args.apply:
                db.commit()
            else:
                if db.new or db.dirty or db.deleted:
                    raise RuntimeError("reconciliation dry-run attempted to mutate ORM state")
                db.rollback()
    except (RejectOwnershipConflict, ValueError) as exc:
        print(json.dumps({"status": "conflict", "error_type": type(exc).__name__}, sort_keys=True))
        return 3
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
