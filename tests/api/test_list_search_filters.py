"""Search and status filters on list endpoints (Sprint 4 PR 4.1).

Covers:
- GET /api/v1/people/        — q on name/email, status on Person.status
- GET /api/v1/events/        — q on type, status mapping to upcoming/past/ongoing
- GET /api/v1/invitations    — q on email/name, status (renamed from status_filter)
- GET /api/v1/teams/         — q on name/description
- GET /api/v1/organizations/ — q on name
"""


import pytest

from api.models import Person
from tests.api.conftest import (
    auth_headers,
    seed_event,
    seed_invitation,
    seed_org,
    seed_team,
    seed_user,
)


def _admin_headers(client, org_id: str, suffix: str) -> dict:
    """Create org+admin and return auth headers."""
    seed_org(client, org_id)
    seed_user(
        client,
        org_id,
        email=f"admin-{suffix}@lf.org",
        name="Admin",
        password="AdminPass1!",
    )
    return auth_headers(client, email=f"admin-{suffix}@lf.org", password="AdminPass1!")


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@pytest.mark.no_mock_auth
class TestPeopleSearchAndStatus:
    def test_q_matches_name_substring(self, client, db):
        org_id = "lf-people-q-name"
        hdrs = _admin_headers(client, org_id, "pn")
        # Add a couple more volunteers
        seed_user(
            client,
            org_id,
            email="alice.smith@lf.org",
            name="Alice Smith",
            password="VolPass1!",
            roles=["volunteer"],
        )
        seed_user(
            client,
            org_id,
            email="bob.jones@lf.org",
            name="Bob Jones",
            password="VolPass1!",
            roles=["volunteer"],
        )
        resp = client.get(f"/api/v1/people/?org_id={org_id}&q=alice", headers=hdrs)
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["items"]]
        assert "Alice Smith" in names
        assert "Bob Jones" not in names

    def test_q_matches_email_substring(self, client, db):
        org_id = "lf-people-q-email"
        hdrs = _admin_headers(client, org_id, "pe")
        seed_user(
            client,
            org_id,
            email="carol@unique-domain.org",
            name="Carol",
            password="VolPass1!",
            roles=["volunteer"],
        )
        resp = client.get(f"/api/v1/people/?org_id={org_id}&q=unique-domain", headers=hdrs)
        assert resp.status_code == 200
        emails = [p["email"] for p in resp.json()["items"]]
        assert "carol@unique-domain.org" in emails

    def test_q_no_match_returns_empty(self, client, db):
        org_id = "lf-people-q-empty"
        hdrs = _admin_headers(client, org_id, "px")
        resp = client.get(f"/api/v1/people/?org_id={org_id}&q=zzzz-no-such-person", headers=hdrs)
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert resp.json()["total"] == 0

    def test_status_filter(self, client, db):
        org_id = "lf-people-status"
        hdrs = _admin_headers(client, org_id, "ps")
        active = seed_user(
            client,
            org_id,
            email="active@lf.org",
            name="Active Vol",
            password="VolPass1!",
            roles=["volunteer"],
        )
        inactive = seed_user(
            client,
            org_id,
            email="inactive@lf.org",
            name="Inactive Vol",
            password="VolPass1!",
            roles=["volunteer"],
        )
        # Mark the second one as inactive directly via DB
        person = db.query(Person).filter(Person.id == inactive["person_id"]).first()
        assert person is not None
        person.status = "inactive"
        db.commit()

        resp = client.get(f"/api/v1/people/?org_id={org_id}&status=inactive", headers=hdrs)
        assert resp.status_code == 200
        ids = [p["id"] for p in resp.json()["items"]]
        assert inactive["person_id"] in ids
        assert active["person_id"] not in ids


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@pytest.mark.no_mock_auth
class TestEventsSearchAndStatus:
    def test_q_matches_type_substring(self, client, db):
        org_id = "lf-events-q"
        hdrs = _admin_headers(client, org_id, "eq")
        seed_event(client, hdrs, org_id, event_id="evt-sun", event_type="Sunday Service")
        seed_event(client, hdrs, org_id, event_id="evt-wed", event_type="Wednesday Bible Study")

        resp = client.get(f"/api/v1/events/?org_id={org_id}&q=sunday", headers=hdrs)
        assert resp.status_code == 200
        types = [e["type"] for e in resp.json()["items"]]
        assert "Sunday Service" in types
        assert "Wednesday Bible Study" not in types

    def test_status_upcoming(self, client, db):
        org_id = "lf-events-upcoming"
        hdrs = _admin_headers(client, org_id, "eu")
        seed_event(
            client, hdrs, org_id, event_id="evt-future", event_type="Future", days_from_now=10
        )
        seed_event(client, hdrs, org_id, event_id="evt-past", event_type="Past", days_from_now=-10)
        resp = client.get(f"/api/v1/events/?org_id={org_id}&status=upcoming", headers=hdrs)
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()["items"]]
        assert "evt-future" in ids
        assert "evt-past" not in ids

    def test_status_past(self, client, db):
        org_id = "lf-events-past"
        hdrs = _admin_headers(client, org_id, "ep")
        seed_event(
            client, hdrs, org_id, event_id="evt-future-2", event_type="Future", days_from_now=10
        )
        seed_event(
            client, hdrs, org_id, event_id="evt-past-2", event_type="Past", days_from_now=-10
        )
        resp = client.get(f"/api/v1/events/?org_id={org_id}&status=past", headers=hdrs)
        assert resp.status_code == 200
        ids = [e["id"] for e in resp.json()["items"]]
        assert "evt-past-2" in ids
        assert "evt-future-2" not in ids


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


@pytest.mark.no_mock_auth
class TestInvitationsSearchAndStatus:
    def test_q_matches_email(self, client, db):
        org_id = "lf-inv-q-email"
        hdrs = _admin_headers(client, org_id, "ie")
        seed_invitation(client, hdrs, org_id, email="match@lf.org", name="A")
        seed_invitation(client, hdrs, org_id, email="other@lf.org", name="B")
        resp = client.get(f"/api/v1/invitations?org_id={org_id}&q=match", headers=hdrs)
        assert resp.status_code == 200
        emails = [i["email"] for i in resp.json()["items"]]
        assert "match@lf.org" in emails
        assert "other@lf.org" not in emails

    def test_status_filter_renamed(self, client, db):
        org_id = "lf-inv-status"
        hdrs = _admin_headers(client, org_id, "is")
        inv = seed_invitation(client, hdrs, org_id, email="cancel@lf.org", name="C")
        # Cancel one
        client.delete(f"/api/v1/invitations/{inv['id']}", headers=hdrs)
        # And keep one pending
        seed_invitation(client, hdrs, org_id, email="pending@lf.org", name="P")

        resp = client.get(f"/api/v1/invitations?org_id={org_id}&status=cancelled", headers=hdrs)
        assert resp.status_code == 200
        emails = [i["email"] for i in resp.json()["items"]]
        assert "cancel@lf.org" in emails
        assert "pending@lf.org" not in emails


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


@pytest.mark.no_mock_auth
class TestTeamsSearch:
    def test_q_matches_name(self, client, db):
        org_id = "lf-teams-q-name"
        hdrs = _admin_headers(client, org_id, "tn")
        seed_team(client, hdrs, org_id, team_id="t1", name="Worship Team")
        seed_team(client, hdrs, org_id, team_id="t2", name="Hospitality Team")
        resp = client.get(f"/api/v1/teams/?org_id={org_id}&q=worship", headers=hdrs)
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["items"]]
        assert "Worship Team" in names
        assert "Hospitality Team" not in names

    def test_q_matches_description(self, client, db):
        org_id = "lf-teams-q-desc"
        hdrs = _admin_headers(client, org_id, "td")
        # Create with description via direct POST (helper uses default member_ids only)
        client.post(
            "/api/v1/teams/",
            json={
                "id": "t-desc",
                "org_id": org_id,
                "name": "Generic Team",
                "description": "Handles audio-visual setup",
            },
            headers=hdrs,
        )
        client.post(
            "/api/v1/teams/",
            json={
                "id": "t-other",
                "org_id": org_id,
                "name": "Other",
                "description": "Some other notes",
            },
            headers=hdrs,
        )
        resp = client.get(f"/api/v1/teams/?org_id={org_id}&q=audio-visual", headers=hdrs)
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()["items"]]
        assert "t-desc" in ids
        assert "t-other" not in ids


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


@pytest.mark.no_mock_auth
class TestOrganizationsSearch:
    def test_q_matches_name(self, client, db):
        seed_org(client, "lf-orgs-alpha", name="Alpha Church")
        seed_org(client, "lf-orgs-beta", name="Beta League")
        owner = seed_user(client, "lf-orgs-alpha", "alpha@example.com", "Owner")
        resp = client.get(
            "/api/v1/organizations/?q=alpha", headers={"Authorization": f"Bearer {owner['token']}"}
        )
        assert resp.status_code == 200
        names = [o["name"] for o in resp.json()["items"]]
        assert "Alpha Church" in names
        assert "Beta League" not in names

    def test_q_no_match_returns_empty(self, client, db):
        seed_org(client, "lf-orgs-zzz", name="Zeta Org")
        owner = seed_user(client, "lf-orgs-zzz", "zeta@example.com", "Owner")
        resp = client.get(
            "/api/v1/organizations/?q=nomatchhere",
            headers={"Authorization": f"Bearer {owner['token']}"},
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert resp.json()["total"] == 0
