#!/usr/bin/env python3
"""Integration tests: notifications router (Sprint 4 PR 4.6b).

Drives the /api/v1/notifications endpoints (mobile Inbox surface) over real
HTTP against the session-scoped uvicorn api_server:

- GET  /notifications/                         - List (envelope, scoped per role)
- GET  /notifications/{id}                     - Single notification
- GET  /notifications/unread/count             - Unread badge for mobile Inbox
- POST /notifications/{id}/read                - Mark read (idempotent)
- GET  /notifications/preferences/me           - Get email preferences (defaults)
- PUT  /notifications/preferences/me           - Create / update preferences
- GET  /notifications/stats/organization       - Admin org-wide stats
- POST /notifications/test/send                - Admin test-send

Notification rows are seeded directly via SessionLocal (shared with the
uvicorn thread through the on-disk SQLite file) because the router
exposes only read + mark-read for volunteers.
"""

import random
import time

import httpx
import pytest

from api.database import SessionLocal
from api.models import Notification, NotificationStatus, NotificationType
from api.timeutils import utcnow


def _unique(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{random.randint(10000, 99999)}"


def _seed_notification(
    org_id: str,
    recipient_id: str,
    *,
    ntype: str = NotificationType.ASSIGNMENT,
    nstatus: str = NotificationStatus.PENDING,
    opened: bool = False,
) -> int:
    """Insert one Notification row and return its id."""
    session = SessionLocal()
    try:
        n = Notification(
            org_id=org_id,
            recipient_id=recipient_id,
            type=ntype,
            status=nstatus,
            template_data={},
            created_at=utcnow(),
            opened_at=utcnow() if opened else None,
        )
        session.add(n)
        session.commit()
        session.refresh(n)
        return n.id
    finally:
        session.close()


@pytest.fixture
def notifications_org(api_server, api_base):
    marker = _unique("notif_org")
    org_id = marker
    admin_email = f"admin_{marker}@test.com"
    vol1_email = f"vol1_{marker}@test.com"
    vol2_email = f"vol2_{marker}@test.com"

    bootstrap = httpx.Client()
    org_resp = bootstrap.post(
        f"{api_base}/organizations/",
        json={"id": org_id, "name": f"Notif Setup {marker}", "region": "US", "config": {}},
    )
    assert org_resp.status_code == 201, org_resp.text

    admin_resp = bootstrap.post(
        f"{api_base}/auth/signup",
        json={
            "org_id": org_id,
            "name": "Notif Admin",
            "email": admin_email,
            "password": "AdminPass123!",
        },
    )
    assert admin_resp.status_code == 201, admin_resp.text
    admin_data = admin_resp.json()

    vol1_resp = bootstrap.post(
        f"{api_base}/auth/signup",
        json={
            "org_id": org_id,
            "name": "Notif Vol One",
            "email": vol1_email,
            "password": "VolPass123!",
            "roles": ["volunteer"],
        },
    )
    assert vol1_resp.status_code == 201, vol1_resp.text
    vol1_data = vol1_resp.json()

    vol2_resp = bootstrap.post(
        f"{api_base}/auth/signup",
        json={
            "org_id": org_id,
            "name": "Notif Vol Two",
            "email": vol2_email,
            "password": "VolPass123!",
            "roles": ["volunteer"],
        },
    )
    assert vol2_resp.status_code == 201, vol2_resp.text
    vol2_data = vol2_resp.json()

    bootstrap.close()

    admin_client = httpx.Client()
    admin_client.headers["Authorization"] = f"Bearer {admin_data['token']}"

    vol1_client = httpx.Client()
    vol1_client.headers["Authorization"] = f"Bearer {vol1_data['token']}"

    vol2_client = httpx.Client()
    vol2_client.headers["Authorization"] = f"Bearer {vol2_data['token']}"

    yield {
        "marker": marker,
        "org_id": org_id,
        "admin_id": admin_data["person_id"],
        "vol1_id": vol1_data["person_id"],
        "vol2_id": vol2_data["person_id"],
        "admin_client": admin_client,
        "vol1_client": vol1_client,
        "vol2_client": vol2_client,
        "api_base": api_base,
    }

    admin_client.close()
    vol1_client.close()
    vol2_client.close()


@pytest.fixture
def second_org(api_server, api_base):
    """A completely unrelated org + admin for cross-org 403 assertions."""
    marker = _unique("notif_second")
    org_id = marker
    admin_email = f"admin_{marker}@test.com"

    bootstrap = httpx.Client()
    org_resp = bootstrap.post(
        f"{api_base}/organizations/",
        json={"id": org_id, "name": f"Second Org {marker}", "region": "US", "config": {}},
    )
    assert org_resp.status_code == 201, org_resp.text

    admin_resp = bootstrap.post(
        f"{api_base}/auth/signup",
        json={
            "org_id": org_id,
            "name": "Second Admin",
            "email": admin_email,
            "password": "AdminPass123!",
        },
    )
    assert admin_resp.status_code == 201, admin_resp.text
    admin_data = admin_resp.json()
    bootstrap.close()

    admin_client = httpx.Client()
    admin_client.headers["Authorization"] = f"Bearer {admin_data['token']}"

    yield {
        "org_id": org_id,
        "admin_id": admin_data["person_id"],
        "admin_client": admin_client,
    }

    admin_client.close()


class TestListNotifications:
    """GET /notifications/ - envelope + role-based scoping."""

    def test_volunteer_sees_only_own(self, notifications_org):
        data = notifications_org
        _seed_notification(data["org_id"], data["vol1_id"])
        _seed_notification(data["org_id"], data["vol1_id"])
        _seed_notification(data["org_id"], data["vol2_id"])

        resp = data["vol1_client"].get(
            f"{data['api_base']}/notifications/",
            params={"org_id": data["org_id"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) >= {"notifications", "total", "limit", "offset"}
        # Vol1 sees only their two rows, not vol2's
        assert body["total"] == 2
        assert all(row["recipient_id"] == data["vol1_id"] for row in body["notifications"])

    def test_admin_sees_all_org(self, notifications_org):
        data = notifications_org
        _seed_notification(data["org_id"], data["vol1_id"])
        _seed_notification(data["org_id"], data["vol2_id"])
        _seed_notification(data["org_id"], data["admin_id"])

        resp = data["admin_client"].get(
            f"{data['api_base']}/notifications/",
            params={"org_id": data["org_id"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        recipients = {row["recipient_id"] for row in body["notifications"]}
        assert {data["vol1_id"], data["vol2_id"], data["admin_id"]} <= recipients

    def test_type_filter(self, notifications_org):
        data = notifications_org
        _seed_notification(data["org_id"], data["vol1_id"], ntype=NotificationType.ASSIGNMENT)
        _seed_notification(data["org_id"], data["vol1_id"], ntype=NotificationType.REMINDER)

        resp = data["vol1_client"].get(
            f"{data['api_base']}/notifications/",
            params={"org_id": data["org_id"], "type": NotificationType.REMINDER},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 1
        assert all(row["type"] == NotificationType.REMINDER for row in body["notifications"])

    def test_status_filter(self, notifications_org):
        data = notifications_org
        _seed_notification(data["org_id"], data["vol1_id"], nstatus=NotificationStatus.PENDING)
        _seed_notification(data["org_id"], data["vol1_id"], nstatus=NotificationStatus.DELIVERED)

        resp = data["vol1_client"].get(
            f"{data['api_base']}/notifications/",
            params={"org_id": data["org_id"], "status": NotificationStatus.DELIVERED},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 1
        assert all(row["status"] == NotificationStatus.DELIVERED for row in body["notifications"])

    def test_pagination_envelope_echoes_params(self, notifications_org):
        data = notifications_org
        _seed_notification(data["org_id"], data["vol1_id"])

        resp = data["vol1_client"].get(
            f"{data['api_base']}/notifications/",
            params={"org_id": data["org_id"], "limit": 5, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 5
        assert body["offset"] == 0

    def test_unauthenticated_rejected(self, notifications_org):
        data = notifications_org
        anon = httpx.Client()
        try:
            resp = anon.get(
                f"{data['api_base']}/notifications/",
                params={"org_id": data["org_id"]},
            )
        finally:
            anon.close()
        assert resp.status_code in (401, 403)

    def test_cross_org_forbidden(self, notifications_org, second_org):
        # A caller from `notifications_org` should not be able to read the
        # stranger org's notification list.
        resp = notifications_org["admin_client"].get(
            f"{notifications_org['api_base']}/notifications/",
            params={"org_id": second_org["org_id"]},
        )
        assert resp.status_code == 403


class TestGetNotification:
    """GET /notifications/{id} - recipient-only access."""

    def test_recipient_can_read_own(self, notifications_org):
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"])
        resp = data["vol1_client"].get(f"{data['api_base']}/notifications/{nid}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == nid
        assert body["recipient_id"] == data["vol1_id"]

    def test_non_recipient_forbidden(self, notifications_org):
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"])
        resp = data["vol2_client"].get(f"{data['api_base']}/notifications/{nid}")
        assert resp.status_code == 403

    def test_admin_non_recipient_also_forbidden(self, notifications_org):
        # Router uses `recipient_id != current_user.id` regardless of role,
        # so even admins can't view another user's notification via /{id}.
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"])
        resp = data["admin_client"].get(f"{data['api_base']}/notifications/{nid}")
        assert resp.status_code == 403

    def test_missing_notification_returns_404(self, notifications_org):
        data = notifications_org
        resp = data["vol1_client"].get(f"{data['api_base']}/notifications/999999999")
        assert resp.status_code == 404


class TestUnreadCount:
    """GET /notifications/unread/count - only own, only unopened/unclicked."""

    def test_unread_count_reflects_own_unopened(self, notifications_org):
        data = notifications_org
        before = data["vol1_client"].get(f"{data['api_base']}/notifications/unread/count")
        assert before.status_code == 200
        before_count = before.json()["unread"]

        _seed_notification(data["org_id"], data["vol1_id"], opened=False)
        _seed_notification(data["org_id"], data["vol1_id"], opened=False)

        after = data["vol1_client"].get(f"{data['api_base']}/notifications/unread/count")
        assert after.status_code == 200
        assert after.json()["unread"] == before_count + 2

    def test_unread_count_excludes_others_notifications(self, notifications_org):
        data = notifications_org
        before = data["vol2_client"].get(f"{data['api_base']}/notifications/unread/count")
        assert before.status_code == 200
        before_count = before.json()["unread"]

        # Seed rows for vol1 — must NOT bump vol2's unread count
        _seed_notification(data["org_id"], data["vol1_id"], opened=False)
        _seed_notification(data["org_id"], data["vol1_id"], opened=False)

        after = data["vol2_client"].get(f"{data['api_base']}/notifications/unread/count")
        assert after.json()["unread"] == before_count

    def test_opened_notifications_not_counted(self, notifications_org):
        data = notifications_org
        before = data["vol1_client"].get(f"{data['api_base']}/notifications/unread/count")
        before_count = before.json()["unread"]

        _seed_notification(data["org_id"], data["vol1_id"], opened=True)

        after = data["vol1_client"].get(f"{data['api_base']}/notifications/unread/count")
        assert after.json()["unread"] == before_count


class TestMarkRead:
    """POST /notifications/{id}/read - sets opened_at, idempotent."""

    def test_marks_notification_read(self, notifications_org):
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"], opened=False)
        resp = data["vol1_client"].post(f"{data['api_base']}/notifications/{nid}/read")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["opened_at"] is not None
        assert body["status"] == NotificationStatus.OPENED

    def test_mark_read_is_idempotent(self, notifications_org):
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"], opened=False)

        first = data["vol1_client"].post(f"{data['api_base']}/notifications/{nid}/read")
        assert first.status_code == 200
        first_opened_at = first.json()["opened_at"]

        second = data["vol1_client"].post(f"{data['api_base']}/notifications/{nid}/read")
        assert second.status_code == 200
        # Router only sets opened_at when it's None, so the timestamp is stable.
        assert second.json()["opened_at"] == first_opened_at

    def test_non_recipient_cannot_mark_read(self, notifications_org):
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"], opened=False)
        resp = data["vol2_client"].post(f"{data['api_base']}/notifications/{nid}/read")
        assert resp.status_code == 403

    def test_missing_notification_returns_404(self, notifications_org):
        data = notifications_org
        resp = data["vol1_client"].post(f"{data['api_base']}/notifications/999999999/read")
        assert resp.status_code == 404

    def test_mark_read_decrements_unread_count(self, notifications_org):
        data = notifications_org
        nid = _seed_notification(data["org_id"], data["vol1_id"], opened=False)
        before = data["vol1_client"].get(f"{data['api_base']}/notifications/unread/count")
        before_count = before.json()["unread"]
        assert before_count >= 1

        data["vol1_client"].post(f"{data['api_base']}/notifications/{nid}/read")

        after = data["vol1_client"].get(f"{data['api_base']}/notifications/unread/count")
        assert after.json()["unread"] == before_count - 1


class TestEmailPreferences:
    """GET/PUT /notifications/preferences/me."""

    def test_get_returns_defaults_when_none_saved(self, notifications_org):
        data = notifications_org
        resp = data["vol1_client"].get(f"{data['api_base']}/notifications/preferences/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["person_id"] == data["vol1_id"]
        assert body["org_id"] == data["org_id"]
        assert body["frequency"] == "immediate"
        assert set(body["enabled_types"]) == {
            NotificationType.ASSIGNMENT,
            NotificationType.REMINDER,
            NotificationType.UPDATE,
            NotificationType.CANCELLATION,
        }

    def test_put_creates_preferences(self, notifications_org):
        data = notifications_org
        payload = {
            "frequency": "daily",
            "enabled_types": [NotificationType.REMINDER],
            "language": "es",
            "timezone": "America/Los_Angeles",
            "digest_hour": 9,
        }
        resp = data["vol1_client"].put(
            f"{data['api_base']}/notifications/preferences/me",
            json=payload,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["person_id"] == data["vol1_id"]
        assert body["frequency"] == "daily"
        assert body["enabled_types"] == [NotificationType.REMINDER]
        assert body["language"] == "es"
        assert body["timezone"] == "America/Los_Angeles"
        assert body["digest_hour"] == 9

    def test_put_updates_existing_preferences(self, notifications_org):
        data = notifications_org
        # Create first
        data["vol1_client"].put(
            f"{data['api_base']}/notifications/preferences/me",
            json={"frequency": "immediate", "digest_hour": 8},
        )
        # Update
        resp = data["vol1_client"].put(
            f"{data['api_base']}/notifications/preferences/me",
            json={"frequency": "weekly", "digest_hour": 15},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["frequency"] == "weekly"
        assert body["digest_hour"] == 15

    def test_digest_hour_out_of_range_rejected(self, notifications_org):
        data = notifications_org
        resp = data["vol1_client"].put(
            f"{data['api_base']}/notifications/preferences/me",
            json={"digest_hour": 25},
        )
        assert resp.status_code == 422

    def test_preferences_require_auth(self, notifications_org):
        data = notifications_org
        anon = httpx.Client()
        try:
            resp = anon.get(f"{data['api_base']}/notifications/preferences/me")
        finally:
            anon.close()
        assert resp.status_code in (401, 403)


class TestOrganizationStats:
    """GET /notifications/stats/organization - admin only."""

    def test_admin_can_read_stats(self, notifications_org):
        data = notifications_org
        _seed_notification(data["org_id"], data["vol1_id"], nstatus=NotificationStatus.DELIVERED)
        _seed_notification(data["org_id"], data["vol1_id"], nstatus=NotificationStatus.FAILED)

        resp = data["admin_client"].get(
            f"{data['api_base']}/notifications/stats/organization",
            params={"org_id": data["org_id"], "days": 7},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["org_id"] == data["org_id"]
        assert body["days_analyzed"] == 7
        assert body["total_notifications"] >= 2
        assert 0 <= body["success_rate"] <= 100
        assert isinstance(body["status_breakdown"], dict)
        assert isinstance(body["type_breakdown"], dict)
        assert isinstance(body["recent_failures"], list)

    def test_volunteer_forbidden(self, notifications_org):
        data = notifications_org
        resp = data["vol1_client"].get(
            f"{data['api_base']}/notifications/stats/organization",
            params={"org_id": data["org_id"]},
        )
        assert resp.status_code == 403

    def test_cross_org_admin_forbidden(self, notifications_org, second_org):
        # Second-org admin cannot pull stats for notifications_org.
        resp = second_org["admin_client"].get(
            f"{notifications_org['api_base']}/notifications/stats/organization",
            params={"org_id": notifications_org["org_id"]},
        )
        assert resp.status_code == 403


class TestTestSend:
    """POST /notifications/test/send - admin only."""

    def test_admin_can_send_test(self, notifications_org):
        data = notifications_org
        resp = data["admin_client"].post(
            f"{data['api_base']}/notifications/test/send",
            params={"recipient_email": "test@example.com", "org_id": data["org_id"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["notification_id"] > 0
        assert "test@example.com" in body["message"]

    def test_volunteer_forbidden(self, notifications_org):
        data = notifications_org
        resp = data["vol1_client"].post(
            f"{data['api_base']}/notifications/test/send",
            params={"recipient_email": "test@example.com", "org_id": data["org_id"]},
        )
        assert resp.status_code == 403

    def test_cross_org_admin_forbidden(self, notifications_org, second_org):
        resp = second_org["admin_client"].post(
            f"{notifications_org['api_base']}/notifications/test/send",
            params={
                "recipient_email": "test@example.com",
                "org_id": notifications_org["org_id"],
            },
        )
        assert resp.status_code == 403


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
