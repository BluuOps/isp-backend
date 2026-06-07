from app import app
from app.database import Base
from app.models import RadCheck, RadReply, ServicePlan, User
from app.schemas import ServicePlanCreate, UserCreate


def main() -> None:
    route_paths = sorted({route.path.rstrip("/") or "/" for route in app.routes})
    required_routes = {"/health", "/users", "/plans"}

    missing_routes = required_routes.difference(route_paths)
    if missing_routes:
        print("Available routes:")
        for route_path in route_paths:
            print(" -", route_path)
        raise RuntimeError(f"Missing expected routes: {', '.join(sorted(missing_routes))}")

    expected_tables = {"users", "service_plans", "radcheck", "radreply"}
    metadata_tables = set(Base.metadata.tables)

    missing_tables = expected_tables.difference(metadata_tables)
    if missing_tables:
        raise RuntimeError(f"Missing expected metadata tables: {', '.join(sorted(missing_tables))}")

    print("Preflight OK")
    print("Routes:", len(app.routes))
    print("Tables:", ", ".join(sorted(expected_tables)))
    print("Models:", User.__name__, ServicePlan.__name__, RadCheck.__name__, RadReply.__name__)
    print("Schemas:", UserCreate.__name__, ServicePlanCreate.__name__)


if __name__ == "__main__":
    main()
