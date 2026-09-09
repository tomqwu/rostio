"""Sprint 11.2 + 11.3 — forgot-password + reset-password web flow."""

from __future__ import annotations

from html.parser import HTMLParser
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest

from api.models import Person
from api.routers import password_reset as reset_router
from api.security import verify_password
from tests.web.conftest import seed_person


def test_forgot_page_renders(client):
    resp = client.get("/auth/forgot")
    assert resp.status_code == 200
    assert 'name="email"' in resp.text
    assert "Reset your password" in resp.text


def test_forgot_unknown_email_still_shows_sent(client, db):
    # No user enumeration: unknown email returns the same generic message.
    resp = client.post("/auth/forgot", data={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert "reset link is on its way" in resp.text.lower()


def test_forgot_invalid_email_shows_validation_error(client, db):
    resp = client.post("/auth/forgot", data={"email": "not-an-email"})
    assert resp.status_code == 400
    assert "valid email" in resp.text.lower()


def test_reset_page_renders_with_token(client):
    resp = client.get("/auth/reset/sometoken123")
    assert resp.status_code == 200
    assert 'action="/auth/reset/sometoken123"' in resp.text
    assert 'name="password"' in resp.text


def test_reset_invalid_token_shows_error(client, db):
    seed_person(db, email="reset-bad@example.com")
    resp = client.post(
        "/auth/reset/totally-invalid-token",
        data={"password": "BrandNewPass123!"},
    )
    assert resp.status_code in (400, 404)
    assert "error" in resp.text.lower() or "invalid" in resp.text.lower()


def test_reset_short_password_rejected(client, db):
    resp = client.post("/auth/reset/whatever", data={"password": "short"})
    assert resp.status_code == 400
    assert "at least 6" in resp.text.lower()


def test_full_reset_flow_changes_password(client, db, monkeypatch):
    """Real token: API forgot (debug-token on) → web reset → password
    actually changes and login works with the new one."""
    monkeypatch.setenv("DEBUG_RETURN_RESET_TOKEN", "true")
    seed_person(
        db,
        person_id="reset_user",
        email="resetme@example.com",
        password="OldPass123!",
    )
    issued = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "resetme@example.com"},
    )
    assert issued.status_code == 200
    token = issued.json()["token"]

    resp = client.post(f"/auth/reset/{token}", data={"password": "FreshPass456!"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?reset=1"

    person = db.query(Person).filter(Person.id == "reset_user").first()
    db.refresh(person)
    assert verify_password("FreshPass456!", person.password_hash)
    assert not verify_password("OldPass123!", person.password_hash)


def test_login_shows_reset_banner(client):
    resp = client.get("/auth/login?reset=1")
    assert resp.status_code == 200
    assert "password updated" in resp.text.lower()


class _EmailLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def test_web_forgot_delivers_a_working_single_use_link(client, db, monkeypatch):
    monkeypatch.delenv("DEBUG_RETURN_RESET_TOKEN", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://signup.example/")
    person = seed_person(db, email="delivery@example.com", password="OriginalPass123!")
    send = MagicMock(return_value=True)
    monkeypatch.setattr(reset_router.email_service, "send_email", send)

    response = client.post("/auth/forgot", data={"email": person.email})
    assert response.status_code == 200
    send.assert_called_once()
    to_email, _, html_body, plain_body = send.call_args.args
    assert to_email == person.email
    links = _EmailLinks()
    links.feed(html_body)
    web_link = next(link for link in links.links if link.startswith("https://"))
    assert web_link.startswith("https://signup.example/auth/reset/")
    assert web_link in plain_body
    assert any(link.startswith("signupflow:///reset-password?token=") for link in links.links)
    path = urlsplit(web_link).path
    token = path.rsplit("/", 1)[-1]
    assert token not in response.text
    page = client.get(path)
    assert page.status_code == 200
    assert f'action="{path}"' in page.text

    changed = client.post(path, data={"password": "ReplacementPass123!"})
    assert changed.status_code == 303
    db.refresh(person)
    assert verify_password("ReplacementPass123!", person.password_hash)
    assert not verify_password("OriginalPass123!", person.password_hash)
    assert client.post(path, data={"password": "AnotherPass123!"}).status_code == 400

    unknown = client.post("/auth/forgot", data={"email": "unknown@example.com"})
    assert unknown.text == response.text
    send.assert_called_once()


@pytest.mark.parametrize("raises", [False, True])
def test_web_forgot_delivery_failure_is_observable_and_retryable(
    client, db, monkeypatch, caplog, raises
):
    person = seed_person(db, email="retry@example.com")
    send = MagicMock(
        return_value=False, side_effect=RuntimeError("mail unavailable") if raises else None
    )
    monkeypatch.setattr(reset_router.email_service, "send_password_reset_email", send)
    response = client.post("/auth/forgot", data={"email": person.email})
    assert response.status_code == 200
    send.assert_called_once()
    assert "password-reset email delivery failed" in caplog.text.lower()
    old_token = send.call_args.kwargs["reset_token"]
    send.side_effect = None
    send.return_value = True
    retry = client.post("/auth/forgot", data={"email": person.email})
    assert retry.text == response.text
    assert send.call_count == 2
    new_token = send.call_args.kwargs["reset_token"]
    assert new_token != old_token
    assert (
        client.post(f"/auth/reset/{old_token}", data={"password": "NewPass123!"}).status_code == 400
    )
    assert (
        client.post(f"/auth/reset/{new_token}", data={"password": "NewPass123!"}).status_code == 303
    )
