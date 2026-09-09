"""Pytest configuration and fixtures for SignUpFlow tests."""

import os
import tempfile
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Force deterministic env BEFORE any application import. Module-level so
# singletons like `EmailService` (created at import time) see the right
# values when constructed. `TESTING=true` in particular gates email_service
# off so synchronous BackgroundTasks under FastAPI TestClient don't block
# on real SMTP retries.
if "SIGNUPFLOW_TEST_DATABASE_URL" not in os.environ:
    test_directory = tempfile.mkdtemp(prefix="signupflow-tests-")
    os.environ["SIGNUPFLOW_TEST_DATABASE_URL"] = f"sqlite:///{test_directory}/signupflow_test.db"
os.environ["DATABASE_URL"] = os.environ["SIGNUPFLOW_TEST_DATABASE_URL"]
os.environ["TESTING"] = "true"

import pytest
from fastapi.testclient import TestClient

import api.database
import api.main
from api.main import app
from api.models import Base, Organization, Person
from api.security import hash_password

SKIP_DB_FIXTURES = os.getenv("SKIP_TEST_DB_FIXTURES", "").lower() in {"1", "true", "yes", "on"}


def pytest_configure(config):
    """Configure pytest with custom markers."""
    os.environ["TESTING"] = "true"
    for marker in ["unit", "integration", "api", "cli", "slow", "no_mock_auth"]:
        config.addinivalue_line("markers", f"{marker}: {marker} tests")


@pytest.fixture(autouse=True)
def mock_authentication(request):
    """
    Mock authentication dependencies for unit tests.

    Integration tests opt out by living under tests/integration/ or by marking
    @pytest.mark.no_mock_auth.
    """
    is_integration = "integration" in str(request.path)
    if is_integration or request.node.get_closest_marker("no_mock_auth"):
        yield
        return

    from api.dependencies import get_current_admin_user, get_current_user

    async def override_get_admin_user():
        return Person(
            id="test_admin",
            org_id="test_org",
            name="Test Admin",
            email="admin@test.com",
            roles=["admin"],
            timezone="UTC",
            language="en",
            status="active",
        )

    async def override_get_user():
        return Person(
            id="test_admin",
            org_id="test_org",
            name="Test Admin",
            email="admin@test.com",
            roles=["admin"],
            timezone="UTC",
            language="en",
            status="active",
        )

    def override_verify_org_member(person: Person, org_id: str) -> None:
        pass

    app.dependency_overrides[get_current_admin_user] = override_get_admin_user
    app.dependency_overrides[get_current_user] = override_get_user

    import api.dependencies
    import api.routers.calendar
    import api.routers.events
    import api.routers.people
    import api.routers.teams

    original_verify = api.dependencies.verify_org_member
    api.dependencies.verify_org_member = override_verify_org_member
    api.routers.people.verify_org_member = override_verify_org_member
    if hasattr(api.routers.events, "verify_org_member"):
        api.routers.events.verify_org_member = override_verify_org_member
    if hasattr(api.routers.teams, "verify_org_member"):
        api.routers.teams.verify_org_member = override_verify_org_member
    if hasattr(api.routers.calendar, "verify_org_member"):
        api.routers.calendar.verify_org_member = override_verify_org_member

    yield

    app.dependency_overrides.pop(get_current_admin_user, None)
    app.dependency_overrides.pop(get_current_user, None)
    api.dependencies.verify_org_member = original_verify
    api.routers.people.verify_org_member = original_verify
    if hasattr(api.routers.events, "verify_org_member"):
        api.routers.events.verify_org_member = original_verify
    if hasattr(api.routers.teams, "verify_org_member"):
        api.routers.teams.verify_org_member = original_verify
    if hasattr(api.routers.calendar, "verify_org_member"):
        api.routers.calendar.verify_org_member = original_verify


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Create the shared SQLite test DB and patch api.database to use it."""
    if SKIP_DB_FIXTURES:
        yield
        return

    connect_args = {"check_same_thread": False}
    engine = create_engine(
        os.environ["SIGNUPFLOW_TEST_DATABASE_URL"],
        connect_args=connect_args,
        echo=False,
    )

    api.database.DATABASE_URL = os.environ["SIGNUPFLOW_TEST_DATABASE_URL"]
    api.database.engine = engine
    api.database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    Base.metadata.create_all(bind=engine)

    session = api.database.SessionLocal()
    try:
        if not session.query(Organization).filter_by(id="test_org").first():
            from datetime import datetime

            org = Organization(
                id="test_org",
                name="Test Org",
                region="US",
                config={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(org)

        if not session.query(Person).filter_by(id="test_admin").first():
            admin = Person(
                id="test_admin",
                org_id="test_org",
                name="Test Admin",
                email="jane@test.com",
                password_hash=hash_password("password"),
                roles=["admin"],
            )
            session.add(admin)

        if not session.query(Person).filter_by(id="test_volunteer").first():
            volunteer = Person(
                id="test_volunteer",
                org_id="test_org",
                name="Test Volunteer",
                email="sarah@test.com",
                password_hash=hash_password("password"),
                roles=["volunteer"],
            )
            session.add(volunteer)

        session.commit()
    except Exception as e:
        print(f"⚠️ Error seeding initial data: {e}")
        session.rollback()
    finally:
        session.close()

    yield engine


def create_test_org(db: Session, org_id: str = None, name: str = None) -> Organization:
    """Helper to create a test organization directly in DB."""
    if not org_id:
        org_id = f"org_{uuid.uuid4().hex[:8]}"
    org = Organization(
        id=org_id,
        name=name or f"Test Org {org_id}",
        region="US",
        config={},
    )
    db.add(org)
    db.commit()
    return org


def create_test_user(
    db: Session,
    org_id: str,
    roles: list = None,
    email: str = None,
    password: str = None,
) -> Person:
    """Helper to create a test user directly in DB."""
    if not email:
        email = f"user_{uuid.uuid4().hex[:8]}@test.com"
    user = Person(
        id=f"person_{uuid.uuid4().hex[:8]}",
        org_id=org_id,
        name="Test User",
        email=email,
        password_hash=hash_password(password or "TestPassword123!"),
        roles=roles or ["volunteer"],
        status="active",
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture(scope="function")
def db(setup_test_database):
    """Provides a database session for tests."""
    from api.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function", autouse=True)
def reset_database_between_tests(request, setup_test_database):
    """Wipe the DB between tests and re-seed the baseline rows."""
    if SKIP_DB_FIXTURES:
        yield
        return

    if "comprehensive_test_suite.py" in str(request.path):
        yield
        return

    yield

    engine = api.database.engine
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            try:
                conn.execute(text(f"DELETE FROM {table.name}"))
            except Exception:
                pass
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.commit()

    Base.metadata.create_all(bind=engine)

    session = api.database.SessionLocal()
    try:
        org = Organization(id="test_org", name="Test Org", region="US", config={})
        session.add(org)
        admin = Person(
            id="test_admin",
            org_id="test_org",
            name="Test Admin",
            email="jane@test.com",
            password_hash=hash_password("password"),
            roles=["admin"],
        )
        session.add(admin)
        volunteer = Person(
            id="test_volunteer",
            org_id="test_org",
            name="Test Volunteer",
            email="sarah@test.com",
            password_hash=hash_password("password"),
            roles=["volunteer"],
        )
        session.add(volunteer)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


@pytest.fixture(scope="function")
def client(setup_test_database):
    """Return a FastAPI TestClient bound to the patched test DB."""
    return TestClient(app)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Capture test result for fixtures."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)
