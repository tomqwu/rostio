"""Tests for api/dependencies.py"""

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.dependencies import (
    check_admin_permission,
    get_organization_by_id,
    get_person_by_id,
    verify_admin_access,
    verify_org_member,
)
from api.models import Organization, Person


class TestCheckAdminPermission:
    """Test check_admin_permission function."""

    def test_admin_role_returns_true(self):
        """Test that admin role returns True."""
        person = Person(id="p1", name="Admin", roles=["admin"])
        assert check_admin_permission(person) is True

    def test_super_admin_role_does_not_grant_privileges(self):
        """Only the documented admin role grants administrative access."""
        person = Person(id="p1", name="Super Admin", roles=["super_admin"])
        assert check_admin_permission(person) is False

    def test_multiple_roles_with_admin_returns_true(self):
        """Test that having admin among other roles returns True."""
        person = Person(id="p1", name="User", roles=["volunteer", "admin"])
        assert check_admin_permission(person) is True

    def test_non_admin_role_returns_false(self):
        """Test that non-admin role returns False."""
        person = Person(id="p1", name="User", roles=["volunteer"])
        assert check_admin_permission(person) is False

    def test_empty_roles_returns_false(self):
        """Test that empty roles returns False."""
        person = Person(id="p1", name="User", roles=[])
        assert check_admin_permission(person) is False

    def test_none_roles_returns_false(self):
        """Test that None roles returns False."""
        person = Person(id="p1", name="User", roles=None)
        assert check_admin_permission(person) is False

    def test_none_person_returns_false(self):
        """Test that None person returns False."""
        assert check_admin_permission(None) is False


class TestGetPersonById:
    """Test get_person_by_id function."""

    def test_existing_person_returns_person(self, db_session: Session, test_org: Organization):
        """Test that existing person is returned."""
        import time

        person_id = f"person_{int(time.time() * 1000000)}"

        # Create person
        person = Person(
            id=person_id,
            org_id=test_org.id,
            name="John Doe",
            email=f"{person_id}@test.com",
            roles=["volunteer"],
        )
        db_session.add(person)
        db_session.commit()

        # Get person
        result = get_person_by_id(person_id, db_session)
        assert result is not None
        assert result.id == person_id
        assert result.name == "John Doe"

    def test_nonexistent_person_raises_404(self, db_session: Session):
        """Test that nonexistent person raises HTTPException with 404."""
        with pytest.raises(HTTPException) as exc_info:
            get_person_by_id("nonexistent", db_session)

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


class TestGetOrganizationById:
    """Test get_organization_by_id function."""

    def test_existing_org_returns_org(self, db_session: Session):
        """Test that existing organization is returned."""
        import time

        org_id = f"org_{int(time.time() * 1000000)}"

        # Create org
        org = Organization(id=org_id, name="Test Org")
        db_session.add(org)
        db_session.commit()

        # Get org
        result = get_organization_by_id(org_id, db_session)
        assert result is not None
        assert result.id == org_id
        assert result.name == "Test Org"

    def test_nonexistent_org_raises_404(self, db_session: Session):
        """Test that nonexistent organization raises HTTPException with 404."""
        with pytest.raises(HTTPException) as exc_info:
            get_organization_by_id("nonexistent", db_session)

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()


class TestVerifyOrgMember:
    """Test verify_org_member function."""

    def test_member_of_org_passes(self, test_org: Organization):
        """Test that person who is member of org passes verification."""
        person = Person(id="p1", org_id=test_org.id, name="User", roles=["volunteer"])

        # Should not raise exception
        verify_org_member(person, test_org.id)

    def test_non_member_raises_403(self, test_org: Organization):
        """Test that person who is not member raises HTTPException with 403."""
        person = Person(id="p1", org_id="other_org", name="User", roles=["volunteer"])

        with pytest.raises(HTTPException) as exc_info:
            verify_org_member(person, test_org.id)

        assert exc_info.value.status_code == 403
        assert "not a member" in exc_info.value.detail.lower()


class TestVerifyAdminAccess:
    """Test verify_admin_access dependency function."""

    @pytest.mark.asyncio
    async def test_admin_user_returns_person(self, db_session: Session, test_org: Organization):
        """Test that admin user is returned."""
        import time

        person_id = f"admin_{int(time.time() * 1000000)}"

        # Create admin person
        person = Person(
            id=person_id,
            org_id=test_org.id,
            name="Admin User",
            email=f"{person_id}@test.com",
            roles=["admin"],
        )
        db_session.add(person)
        db_session.commit()

        # Verify admin access
        result = await verify_admin_access(person)
        assert result is not None
        assert result.id == person_id
        assert "admin" in result.roles

    @pytest.mark.asyncio
    async def test_non_admin_raises_403(self, db_session: Session, test_org: Organization):
        """Test that non-admin user raises HTTPException with 403."""
        import time

        person_id = f"user_{int(time.time() * 1000000)}"

        # Create non-admin person
        person = Person(
            id=person_id,
            org_id=test_org.id,
            name="Regular User",
            email=f"{person_id}@test.com",
            roles=["volunteer"],
        )
        db_session.add(person)
        db_session.commit()

        # Verify admin access should fail
        with pytest.raises(HTTPException) as exc_info:
            await verify_admin_access(person)

        assert exc_info.value.status_code == 403
        assert "admin" in exc_info.value.detail.lower()

    def test_legacy_dependency_requires_authenticated_identity(self):
        """The compatibility dependency must not accept a caller-supplied ID."""
        from inspect import signature

        from api.dependencies import get_current_user

        parameters = signature(verify_admin_access).parameters
        assert list(parameters) == ["current_user"]
        assert parameters["current_user"].default.dependency is get_current_user


# Fixtures
@pytest.fixture
def db_session():
    """Create a test database session."""
    import os
    import tempfile

    from api.database import get_db, init_db

    # Create temporary database
    fd, path = tempfile.mkstemp(suffix=".db")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"

    # Initialize database
    init_db()

    # Get session
    db = next(get_db())

    yield db

    # Cleanup
    db.close()
    os.close(fd)
    os.unlink(path)


@pytest.fixture
def test_org(db_session: Session):
    """Create a test organization with unique ID."""
    import time

    org_id = f"test_org_{int(time.time() * 1000000)}"
    org = Organization(id=org_id, name="Test Organization")
    db_session.add(org)
    db_session.commit()
    return org
