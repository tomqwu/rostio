"""Web auth: HTML login form + cookie session set/clear.

Credential validation is identical to `api/routers/auth.py:login`
(`verify_password` + `_pwd_iat_for`). The only difference: instead of
returning the JWT in a JSON body, we set it as an httpOnly cookie and
redirect the browser to the role-appropriate landing page.
"""

from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.database import get_db
from api.models import Person
from api.routers.auth import SignupRequest, signup as api_signup
from api.routers.invitations import accept_invitation, verify_invitation
from api.routers.organizations import create_organization
from api.routers.password_reset import (
    PasswordResetConfirm,
    PasswordResetRequest,
    request_password_reset,
    reset_password,
)
from api.schemas.invitation import InvitationAccept
from api.schemas.organization import OrganizationCreate
from api.security import create_access_token, verify_password
from api.timeutils import utcnow
from web.deps import SESSION_COOKIE, get_optional_session_user

router = APIRouter(tags=["web-auth"])

# Secure flag off in dev/test (http://localhost) so the cookie is stored;
# on in production. ENVIRONMENT=production is set by the app at boot.
_COOKIE_SECURE = os.getenv("ENVIRONMENT", "development") == "production"
_COOKIE_MAX_AGE = 60 * 60 * 24  # 24h, matches access-token lifetime intent


def _pwd_iat_for(person: Person) -> float:
    """Mirror of api/routers/auth.py:_pwd_iat_for — the password-version
    claim used for session-invalidation-on-password-change."""
    if person.password_changed_at is not None:
        return person.password_changed_at.timestamp()
    return utcnow().timestamp()


def _landing_for(person: Person) -> str:
    return "/a/dashboard" if "admin" in (person.roles or []) else "/v/schedule"


def _set_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def set_session_cookie(response, person: Person) -> None:
    _set_cookie(
        response,
        create_access_token(data={"sub": person.id, "pwd_iat": _pwd_iat_for(person)}),
    )


def _slugify(name: str) -> str:
    """org name → url-safe id. Mirrors the mobile create-org slug rule."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "org"


@router.get("/auth/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    person: Person | None = Depends(get_optional_session_user),
):
    # Already signed in → skip the form.
    if person is not None:
        return RedirectResponse(url=_landing_for(person), status_code=303)
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            "error": None,
            "notice": (
                "Password updated — sign in with your new password."
                if request.query_params.get("reset") == "1"
                else None
            ),
        },
    )


@router.post("/auth/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from web.app import templates

    person = db.query(Person).filter(Person.email == email).first()
    invalid = (
        person is None
        or not person.password_hash
        or not verify_password(password, person.password_hash)
    )
    if invalid:
        # 401 + re-render the form with an inline error (HTMX swaps the
        # whole <main>; full-page nav also works since the template
        # extends base).
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Invalid email or password."},
            status_code=401,
        )

    response = RedirectResponse(url=_landing_for(person), status_code=303)
    set_session_cookie(response, person)
    return response


@router.get("/auth/signup", response_class=HTMLResponse)
def signup_form(
    request: Request,
    person: Person | None = Depends(get_optional_session_user),
):
    if person is not None:
        return RedirectResponse(url=_landing_for(person), status_code=303)
    from web.app import templates

    return templates.TemplateResponse(request, "auth/signup.html", {"error": None, "form": {}})


@router.post("/auth/signup")
def signup_submit(
    request: Request,
    org_name: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create an organization and its first user (auto-admin). Reuses the
    API's create_organization + signup so all validation/limits apply."""
    from web.app import templates

    form = {"org_name": org_name, "name": name, "email": email}

    def _err(msg: str, code: int = 400):
        return templates.TemplateResponse(
            request,
            "auth/signup.html",
            {"error": msg, "form": form},
            status_code=code,
        )

    org_id = f"{_slugify(org_name)}-{uuid.uuid4().hex[:6]}"

    # Validate the signup payload BEFORE creating the org, so a bad
    # password (pydantic min_length) can't leave an orphan organization.
    try:
        signup_req = SignupRequest(org_id=org_id, name=name, email=email, password=password)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = first.get("loc", ["field"])[-1]
        return _err(f"{field}: {first.get('msg', 'invalid value')}", code=400)

    try:
        create_organization(OrganizationCreate(id=org_id, name=org_name), db)
    except HTTPException as exc:
        return _err(f"Could not create organization: {exc.detail}")

    try:
        auth = api_signup(signup_req, db)
    except HTTPException as exc:
        return _err(str(exc.detail), code=exc.status_code or 400)

    response = RedirectResponse(url="/a/dashboard", status_code=303)
    _set_cookie(response, auth.token)
    return response


@router.get("/auth/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    from web.app import templates

    return templates.TemplateResponse(request, "auth/forgot.html", {"sent": False, "error": None})


@router.post("/auth/forgot")
def forgot_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Always renders the same 'if it exists, a link was sent' message —
    no user enumeration (mirrors the API's generic response)."""
    from web.app import templates

    try:
        req = PasswordResetRequest(email=email)
    except ValidationError:
        return templates.TemplateResponse(
            request,
            "auth/forgot.html",
            {"sent": False, "error": "Enter a valid email address."},
            status_code=400,
        )
    request_password_reset(req, request, background_tasks, db)
    return templates.TemplateResponse(
        request,
        "auth/forgot.html",
        {"sent": True, "error": None},
        background=background_tasks,
    )


@router.get("/auth/reset/{token}", response_class=HTMLResponse)
def reset_form(request: Request, token: str):
    from web.app import templates

    return templates.TemplateResponse(request, "auth/reset.html", {"token": token, "error": None})


@router.post("/auth/reset/{token}")
def reset_submit(
    request: Request,
    token: str,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from web.app import templates

    def _err(msg: str, code: int = 400):
        return templates.TemplateResponse(
            request,
            "auth/reset.html",
            {"token": token, "error": msg},
            status_code=code,
        )

    try:
        confirm = PasswordResetConfirm(token=token, new_password=password)
    except ValidationError:
        return _err("Password must be at least 6 characters.")
    try:
        reset_password(confirm, db)
    except HTTPException as exc:
        return _err(str(exc.detail), code=exc.status_code or 400)

    # Success → bounce to login with a one-shot banner via query flag.
    return RedirectResponse(url="/auth/login?reset=1", status_code=303)


@router.get("/auth/invitation/{token}", response_class=HTMLResponse)
def invitation_form(request: Request, token: str, db: Session = Depends(get_db)):
    from web.app import templates

    result = verify_invitation(token, db)
    return templates.TemplateResponse(
        request,
        "auth/invitation.html",
        {
            "token": token,
            "valid": result.valid,
            "invitation": result.invitation,
            "message": result.message,
            "error": None,
        },
        status_code=200 if result.valid else 410,
    )


@router.post("/auth/invitation/{token}")
def invitation_submit(
    request: Request,
    token: str,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Accept the invitation, create the account, and land the user
    authenticated (mirrors api accept_invitation which returns a JWT)."""
    from web.app import templates

    def _rerender(error: str, code: int):
        result = verify_invitation(token, db)
        return templates.TemplateResponse(
            request,
            "auth/invitation.html",
            {
                "token": token,
                "valid": result.valid,
                "invitation": result.invitation,
                "message": result.message,
                "error": error,
            },
            status_code=code,
        )

    try:
        accept = InvitationAccept(password=password, timezone="UTC")
    except ValidationError:
        return _rerender("Password must be at least 6 characters.", 400)
    try:
        result = accept_invitation(token, accept, db)
    except HTTPException as exc:
        return _rerender(str(exc.detail), exc.status_code or 400)

    landing = "/a/dashboard" if "admin" in (result.roles or []) else "/v/schedule"
    response = RedirectResponse(url=landing, status_code=303)
    _set_cookie(response, result.token)
    return response


@router.post("/auth/logout")
def logout():
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
