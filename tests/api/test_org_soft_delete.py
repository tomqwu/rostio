"""Soft-delete (cancel/restore) tests for /api/v1/organizations/.

The Organization model has `cancelled_at`/`deletion_scheduled_at`/`data_retention_until`
fields. This sprint exposes an admin-only cancel/restore workflow that sets the
fields and a `?include_cancelled=true` query param on the list endpoint.
"""

import pytest

from api.models import AuditAction, AuditLog
from tests.api.conftest import auth_headers, seed_org, seed_user


def _admin_for(client, org_id: str, suffix: str):
    seed_user(client, org_id, email=f"admin-{suffix}@o.org", name="Admin", password="AdminPass1!")
    return auth_headers(client, email=f"admin-{suffix}@o.org", password="AdminPass1!")


@pytest.mark.no_mock_auth
class TestOrgCancel:
    def test_admin_can_cancel_own_org(self, client, db):
        org_id = "soft-del-cancel"
        seed_org(client, org_id)
        hdrs = _admin_for(client, org_id, "cancel")

        resp = client.post(f"/api/v1/organizations/{org_id}/cancel", headers=hdrs)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cancelled_at"] is not None

    def test_volunteer_cannot_cancel(self, client, db):
        org_id = "soft-del-vol"
        seed_org(client, org_id)
        _admin_for(client, org_id, "vol-admin")
        seed_user(
            client,
            org_id,
            email="vol@o.org",
            name="Vol",
            password="VolPass1!",
            roles=["volunteer"],
        )
        vol_hdrs = auth_headers(client, email="vol@o.org", password="VolPass1!")

        resp = client.post(f"/api/v1/organizations/{org_id}/cancel", headers=vol_hdrs)
        assert resp.status_code == 403

    def test_cross_org_admin_blocked(self, client, db):
        seed_org(client, "soft-del-a")
        seed_org(client, "soft-del-b")
        a_hdrs = _admin_for(client, "soft-del-a", "a")

        resp = client.post("/api/v1/organizations/soft-del-b/cancel", headers=a_hdrs)
        assert resp.status_code == 403

    def test_cancel_emits_audit_row(self, client, db):
        org_id = "soft-del-audit"
        seed_org(client, org_id)
        hdrs = _admin_for(client, org_id, "audit")

        before = db.query(AuditLog).filter(AuditLog.action == AuditAction.ORG_CANCELLED).count()
        client.post(f"/api/v1/organizations/{org_id}/cancel", headers=hdrs)
        after = db.query(AuditLog).filter(AuditLog.action == AuditAction.ORG_CANCELLED).count()
        assert after == before + 1


@pytest.mark.no_mock_auth
class TestOrgRestore:
    def test_admin_can_restore(self, client, db):
        org_id = "soft-del-restore"
        seed_org(client, org_id)
        hdrs = _admin_for(client, org_id, "restore")

        client.post(f"/api/v1/organizations/{org_id}/cancel", headers=hdrs)
        resp = client.post(f"/api/v1/organizations/{org_id}/restore", headers=hdrs)
        assert resp.status_code == 200
        assert resp.json()["cancelled_at"] is None

    def test_restore_emits_audit_row(self, client, db):
        org_id = "soft-del-restore-audit"
        seed_org(client, org_id)
        hdrs = _admin_for(client, org_id, "restore-audit")
        client.post(f"/api/v1/organizations/{org_id}/cancel", headers=hdrs)

        before = db.query(AuditLog).filter(AuditLog.action == AuditAction.ORG_RESTORED).count()
        client.post(f"/api/v1/organizations/{org_id}/restore", headers=hdrs)
        after = db.query(AuditLog).filter(AuditLog.action == AuditAction.ORG_RESTORED).count()
        assert after == before + 1


@pytest.mark.no_mock_auth
class TestListExcludesCancelled:
    def test_list_excludes_cancelled_by_default(self, client, db):
        # Two orgs, one cancelled
        seed_org(client, "list-active")
        seed_org(client, "list-cancelled")
        active_hdrs = _admin_for(client, "list-active", "active")
        cancelled_hdrs = _admin_for(client, "list-cancelled", "cancelled")
        client.post("/api/v1/organizations/list-cancelled/cancel", headers=cancelled_hdrs)

        resp = client.get("/api/v1/organizations/", headers=active_hdrs)
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        assert "list-active" in ids
        assert "list-cancelled" not in ids

    def test_include_cancelled_returns_them(self, client, db):
        seed_org(client, "incl-active")
        seed_org(client, "incl-cancelled")
        active_hdrs = _admin_for(client, "incl-active", "incl-active")
        cancelled_hdrs = _admin_for(client, "incl-cancelled", "incl-cancelled")
        client.post("/api/v1/organizations/incl-cancelled/cancel", headers=cancelled_hdrs)

        resp = client.get("/api/v1/organizations/?include_cancelled=true", headers=active_hdrs)
        ids = [item["id"] for item in resp.json()["items"]]
        assert "incl-cancelled" not in ids
        resp = client.get("/api/v1/organizations/?include_cancelled=true", headers=cancelled_hdrs)
        ids = [item["id"] for item in resp.json()["items"]]
        assert "incl-cancelled" in ids
