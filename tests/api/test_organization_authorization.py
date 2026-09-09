"""Real-JWT tenant authorization and atomic organization lifecycle tests."""

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from api.models import AuditLog, Organization, Person
from api.security import create_access_token

pytestmark = pytest.mark.no_mock_auth


@pytest.fixture
def actors(db):
    db.add_all(
        [
            Organization(id="org-a", name="Org A", config={"private": "A"}),
            Organization(id="org-b", name="Org B", config={"private": "B"}),
        ]
    )
    db.flush()
    headers = {}
    for identity, org_id, roles in [
        ("owner", "org-a", ["admin"]),
        ("volunteer", "org-a", ["volunteer"]),
        ("foreign", "org-b", ["admin"]),
    ]:
        db.add(
            Person(
                id=identity,
                org_id=org_id,
                name=identity,
                email=f"{identity}@example.com",
                roles=roles,
            )
        )
        headers[identity] = {"Authorization": f"Bearer {create_access_token({'sub': identity})}"}
    db.commit()
    return headers


OPERATIONS = [("GET", ""), ("PUT", ""), ("DELETE", ""), ("POST", "/cancel"), ("POST", "/restore")]


@pytest.mark.parametrize(
    "method,suffix,caller",
    [
        (method, suffix, caller)
        for method, suffix in OPERATIONS
        for caller in ["anonymous", "foreign", "volunteer"]
        if not (caller == "volunteer" and method == "GET")
    ],
)
def test_denied_requests_leave_organization_and_members_unchanged(
    client, db, actors, method, suffix, caller
):
    response = client.request(
        method,
        f"/api/v1/organizations/org-a{suffix}",
        headers=actors.get(caller, {}),
        json={"name": "Changed"},
    )
    assert response.status_code in {401, 403}, response.text
    db.expire_all()
    org = db.scalar(select(Organization).where(Organization.id == "org-a"))
    assert org.name == "Org A"
    assert org.cancelled_at is None
    assert len(db.scalars(select(Person).where(Person.org_id == "org-a")).all()) == 2
    assert db.scalars(select(AuditLog).where(AuditLog.organization_id == "org-a")).all() == []


@pytest.mark.parametrize("caller", ["owner", "volunteer"])
def test_member_reads_only_own_organization(client, actors, caller):
    response = client.get("/api/v1/organizations/org-a", headers=actors[caller])
    assert response.status_code == 200
    assert response.json()["config"] == {"private": "A"}
    response = client.get("/api/v1/organizations/?include_cancelled=true", headers=actors[caller])
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == ["org-a"]


def test_anonymous_cannot_list_organizations(client, actors):
    assert client.get("/api/v1/organizations/").status_code in {401, 403}


@pytest.mark.parametrize(
    "method,suffix,action",
    [
        ("PUT", "", "org.updated"),
        ("POST", "/cancel", "org.cancelled"),
        ("POST", "/restore", "org.restored"),
        ("DELETE", "", "data.bulk_delete"),
    ],
)
def test_owner_mutations_are_audited(client, db, actors, method, suffix, action):
    response = client.request(
        method,
        f"/api/v1/organizations/org-a{suffix}",
        headers=actors["owner"],
        json={"name": "Changed"},
    )
    assert response.status_code == (204 if method == "DELETE" else 200), response.text
    db.expire_all()
    records = db.scalars(select(AuditLog).where(AuditLog.organization_id == "org-a")).all()
    assert [(row.action, row.user_id, row.resource_id) for row in records] == [
        (action, "owner", "org-a")
    ]
    if method == "DELETE":
        assert db.scalar(select(Organization).where(Organization.id == "org-a")) is None
        assert db.scalars(select(Person).where(Person.org_id == "org-a")).all() == []
        assert db.scalar(select(Organization).where(Organization.id == "org-b")) is not None


@pytest.mark.parametrize("method,suffix", OPERATIONS[1:])
@pytest.mark.parametrize(
    "failure", ["api.routers.organizations.log_audit_event", "sqlalchemy.orm.Session.commit"]
)
def test_audit_failure_rolls_back_mutation(client, db, actors, method, suffix, failure):
    cancelled_at = datetime(2026, 1, 1) if suffix == "/restore" else None
    org = db.scalar(select(Organization).where(Organization.id == "org-a"))
    org.cancelled_at = cancelled_at
    db.commit()
    with patch(failure, side_effect=RuntimeError("transaction unavailable")):
        response = client.request(
            method,
            f"/api/v1/organizations/org-a{suffix}",
            headers=actors["owner"],
            json={"name": "Changed"},
        )
    assert response.status_code == 500
    db.expire_all()
    org = db.scalar(select(Organization).where(Organization.id == "org-a"))
    assert org is not None
    assert org.name == "Org A"
    assert org.cancelled_at == cancelled_at
    assert len(db.scalars(select(Person).where(Person.org_id == "org-a")).all()) == 2
