"""Password reset endpoints."""

import hashlib
import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from api.database import get_db
from api.models import AuditAction, PasswordResetToken, Person
from api.security import hash_password
from api.services.email_service import email_service
from api.timeutils import utcnow
from api.utils.audit_logger import log_audit_event
from api.utils.rate_limit_middleware import rate_limit


def _debug_return_reset_token() -> bool:
    """True iff DEBUG_RETURN_RESET_TOKEN is opted into via env. Default off."""
    return os.getenv("DEBUG_RETURN_RESET_TOKEN", "false").strip().lower() in ("1", "true", "yes")


def _hash_reset_token(token: str) -> str:
    """SHA-256 digest of the bearer token, used as the PK in
    ``password_reset_tokens``. We store the digest rather than the raw
    token so a leaked DB dump / backup / replica cannot be used to
    redeem outstanding reset links — the attacker would still need the
    raw token from the user's email. SHA-256 is sufficient here (no
    bcrypt) because the token is already 256 bits of cryptographic
    randomness from ``secrets.token_urlsafe(32)``; brute force is
    intractable, so a slow KDF buys nothing.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


router = APIRouter(prefix="/auth", tags=["auth"])


class PasswordResetRequest(BaseModel):
    """Request password reset."""

    email: EmailStr


class PasswordResetConfirm(BaseModel):
    """Confirm password reset with token."""

    token: str
    new_password: str


def _send_reset_email_quiet(to_email: str, name: str, reset_token: str, app_url: str) -> None:
    """Background-task wrapper around send_password_reset_email.

    Swallows all exceptions: a flaky SendGrid (or any other email backend)
    must never affect the HTTP response timing of /forgot-password, both
    to preserve anti-enumeration guarantees and to avoid a DoS path where
    a slow upstream blocks request handlers. Any failure is logged and
    discarded — the reset token is already issued and the user can retry.

    The email service itself short-circuits when ``TESTING=true`` is set
    (see ``EmailService.__init__``), so tests that don't explicitly mock
    the service won't hang on a real SMTP retry loop.
    """
    import logging

    logger = logging.getLogger("password_reset")
    try:
        sent = email_service.send_password_reset_email(
            to_email=to_email,
            name=name,
            reset_token=reset_token,
            app_url=app_url,
        )
        if not sent:
            logger.error("Password-reset email delivery failed; a fresh request may be retried")
    except Exception:  # noqa: BLE001 — see docstring; we never want this to bubble
        logger.error("Password-reset email delivery failed; a fresh request may be retried")


@router.post("/forgot-password", dependencies=[Depends(rate_limit("password_reset"))])
def request_password_reset(
    request: PasswordResetRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Request a password reset token.

    Always returns the same generic message regardless of whether the email
    exists. Audits every request. The reset token is persisted in the
    ``password_reset_tokens`` table (see model in ``api/models.py``) so it
    survives multi-worker deployments — the legacy in-memory dict broke
    under the documented default ``WORKERS=4`` because the worker handling
    ``POST /reset-password`` may differ from the one that issued the token.
    The token is NEVER returned in the response in production. Set
    ``DEBUG_RETURN_RESET_TOKEN=true`` in dev/test environments to opt into
    receiving the token in the JSON body for E2E exercise.

    Email send is queued via ``BackgroundTasks`` so the HTTP response
    timing is independent of email backend latency — both for anti-
    enumeration and to prevent slow-SMTP DoS. The reset token is issued
    synchronously; email delivery is best-effort.
    """
    generic_response = {"message": "If the email exists, a password reset link will be sent"}
    # Roster-only Persons (created via /people or bulk import) live in the
    # database without a login account — ``password_hash IS NULL``. Issuing
    # a reset for those rows would let anyone controlling the listed email
    # bypass invitation/onboarding and inherit the row's roles, since
    # /reset-password writes ``password_hash`` unconditionally. Treat them
    # like an unknown email: no token, generic response.
    person = (
        db.query(Person)
        .filter(Person.email == request.email, Person.password_hash.isnot(None))
        .first()
    )

    log_audit_event(
        db,
        action=AuditAction.PASSWORD_RESET_REQUESTED,
        user_id=person.id if person else None,
        user_email=request.email,
        organization_id=person.org_id if person else None,
        ip_address=http_request.client.host if http_request.client else None,
        user_agent=http_request.headers.get("user-agent"),
        status="success" if person else "denied",
    )

    if not person:
        return generic_response

    # Re-acquire the Person row with ``FOR UPDATE`` immediately before the
    # critical section (invalidate prior tokens + insert new + commit).
    # The initial Person lookup above can't hold the lock, because
    # ``log_audit_event`` commits the audit row in between — which would
    # release any row lock taken on the original query. Re-locking here
    # scopes the lock tightly to the rotation block: two concurrent
    # /forgot-password requests for the same person serialize on this
    # SELECT, then run the UPDATE/INSERT/commit one at a time, so the
    # freshness contract in docs/features/password-reset.md Scenario 6
    # holds. SQLite ignores FOR UPDATE (its global lock already serializes
    # writers); PostgreSQL enforces a real row lock under READ COMMITTED.
    # Scope by org_id too — repo-wide tenancy contract is "every Person
    # query filters by org_id". Filtering by primary key alone is
    # technically sufficient here (Person.id is globally unique), but the
    # convention exists so reviewers can grep for missing org filters in
    # new code paths.
    db.query(Person).filter(
        Person.id == person.id, Person.org_id == person.org_id
    ).with_for_update().one()

    now = utcnow()

    # Invalidate any prior unused reset tokens for this person before
    # issuing a new one. Per docs/features/password-reset.md Scenario 6,
    # a fresh /forgot-password call must render any earlier emailed link
    # invalid — otherwise an attacker who briefly accessed the user's
    # inbox could race them to redeem the stale link.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.person_id == person.id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": now}, synchronize_session=False)

    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            token_hash=_hash_reset_token(token),
            person_id=person.id,
            expires_at=now + timedelta(hours=1),
        )
    )
    db.commit()

    # Queue the email send. Starlette runs background tasks AFTER the
    # response is sent, so HTTP timing is independent of email backend
    # latency (anti-enumeration + anti-DoS).
    #
    # The web fallback link in the email body must point at a host that
    # serves `GET /auth/reset/{token}` in the SignUpFlow web app.
    # FRONTEND_URL is the dedicated public origin; APP_URL is the fallback.
    web_app_url = os.getenv("FRONTEND_URL") or os.getenv("APP_URL", "http://localhost:8000")
    background_tasks.add_task(
        _send_reset_email_quiet,
        to_email=person.email,
        name=person.name,
        reset_token=token,
        app_url=web_app_url,
    )

    if _debug_return_reset_token():
        # Test/dev affordance ONLY. Default off.
        return {**generic_response, "token": token}

    return generic_response


@router.post("/reset-password", dependencies=[Depends(rate_limit("password_reset_confirm"))])
def reset_password(
    request: PasswordResetConfirm,
    db: Session = Depends(get_db),
):
    """Reset password using token.

    The token row is *claimed* with a single conditional UPDATE rather
    than a SELECT-then-update sequence. Two concurrent /reset-password
    submissions of the same emailed link otherwise both pass a SELECT
    (``used_at IS NULL``) before either can stamp it, and both proceed
    to change the password — last-write-wins semantics, not the
    one-time-use guarantee the endpoint advertises. The atomic UPDATE
    closes that race: at most one row transitions from NULL to a
    timestamp, ``rowcount == 0`` for the loser.
    """
    now = utcnow()
    token_hash = _hash_reset_token(request.token)

    # Atomic claim: set used_at on the row IFF it's currently unused
    # AND not expired. Any of {wrong token, already used, expired,
    # raced} produces rowcount == 0.
    claimed = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .update({"used_at": now}, synchronize_session=False)
    )

    if claimed == 0:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    # We hold the claim. Re-fetch the row to learn the person_id, then
    # change the password — both within the same transaction so a
    # rollback path (e.g., person missing) un-claims the token rather
    # than burning it.
    record = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).one()

    person = db.query(Person).filter(Person.id == record.person_id).first()
    if not person:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    person.password_hash = hash_password(request.new_password)
    # Invalidate any auth tokens issued before this reset.
    person.password_changed_at = now

    db.commit()

    return {"message": "Password reset successfully"}
