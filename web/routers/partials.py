"""HTMX fragment endpoints — return HTML partials, not full pages.

Assignment actions reuse the API handlers in api/routers/assignments.py
directly (same _load_own_assignment ownership check, same event_bus
publish). The web session user is passed as `current_user`; a throwaway
BackgroundTasks collects the publish side-effect. On success we re-render
the assignment card partial so HTMX swaps the fresh status in place.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.database import get_db
from api.models import Assignment, EmailPreference, Event, Notification, Person
from api.routers.assignments import (
    accept_assignment,
    decline_assignment,
    request_swap,
)
from api.routers.auth import ChangePasswordRequest, change_password
from api.routers.availability import (
    add_exception,
    add_timeoff,
    clear_rrule,
    delete_exception,
    delete_timeoff,
    set_rrule,
)
from api.routers.calendar import reset_calendar_token
from api.routers.constraints import (
    create_constraint,
    delete_constraint,
    update_constraint,
)
from api.routers.events import AssignmentRequest, create_event, delete_event, manage_assignment
from api.routers.invitations import create_invitation
from api.routers.organizations import get_organization, update_organization
from api.routers.people import bulk_import_people, update_current_person
from api.routers.recurring_events import (
    RecurringSeriesCreate,
    create_recurring_series,
    delete_recurring_series,
)
from api.routers.solutions import (
    compare_solutions,
    publish_solution,
    rollback_solution,
    unpublish_solution,
)
from api.routers.solver import solve_schedule
from api.routers.teams import (
    add_team_members,
    create_team,
    delete_team,
    remove_team_members,
    update_team,
)
from api.schemas.assignment import AssignmentDeclineRequest, AssignmentSwapRequest
from api.schemas.availability import (
    AvailabilityExceptionCreate,
    AvailabilityRruleUpdate,
    TimeOffCreate,
)
from api.schemas.constraint import ConstraintCreate, ConstraintUpdate
from api.schemas.event import EventCreate
from api.schemas.invitation import InvitationCreate
from api.schemas.organization import OrganizationUpdate
from api.schemas.person import PersonUpdate
from api.schemas.solver import SolveRequest
from api.schemas.team import TeamCreate, TeamMemberAdd, TeamMemberRemove, TeamUpdate
from api.timeutils import utcnow
from web.deps import get_session_admin, get_session_user
from web.routers.pages import (
    NOTIF_TYPES,
    RRULE_PRESETS,
    _claimable_swaps,
    _constraints,
    _email_prefs,
    _event_assignments,
    _events,
    _inbox,
    _my_assignment,
    _my_calendar,
    _my_exceptions,
    _my_rrule,
    _my_timeoff,
    _open_shifts,
    _recurring,
    _solution_owned,
    _solution_review,
    _swap_requests,
    _teams,
)

router = APIRouter(tags=["web-partials"])


def _gone() -> HTMLResponse:
    return HTMLResponse(
        '<div id="assignment-card" class="empty">'
        "This assignment isn't on your schedule anymore.</div>",
        status_code=404,
    )


def _card(request: Request, person: Person, db: Session, aid: int):
    from web.app import templates

    row = _my_assignment(db, person, aid)
    if row is None:
        return _gone()
    return templates.TemplateResponse(request, "partials/assignment_detail_card.html", {"row": row})


# NOTE: these routes declare `background_tasks: BackgroundTasks` as a
# FastAPI param (not a throwaway BackgroundTasks()) and pass it to the
# API handler. The handler queues event_bus.publish via add_task;
# FastAPI runs those AFTER the response. A discarded BackgroundTasks()
# would silently drop the publish, so the Solution-Review SSE stream
# (11.19) never fires. Caught by the 11.19 live e2e test.


@router.post("/v/schedule/{assignment_id}/accept", response_class=HTMLResponse)
def accept(
    request: Request,
    assignment_id: int,
    background_tasks: BackgroundTasks,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        accept_assignment(assignment_id, request, background_tasks, person, db)
    except HTTPException:
        return _gone()
    return _card(request, person, db, assignment_id)


@router.post("/v/schedule/{assignment_id}/decline", response_class=HTMLResponse)
def decline(
    request: Request,
    assignment_id: int,
    background_tasks: BackgroundTasks,
    decline_reason: str = Form(...),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        decline_assignment(
            assignment_id,
            AssignmentDeclineRequest(decline_reason=decline_reason),
            request,
            background_tasks,
            person,
            db,
        )
    except HTTPException:
        return _gone()
    return _card(request, person, db, assignment_id)


@router.post("/v/schedule/{assignment_id}/swap-request", response_class=HTMLResponse)
def swap(
    request: Request,
    assignment_id: int,
    background_tasks: BackgroundTasks,
    note: str | None = Form(None),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        request_swap(
            assignment_id,
            AssignmentSwapRequest(note=note),
            request,
            background_tasks,
            person,
            db,
        )
    except HTTPException:
        return _gone()
    return _card(request, person, db, assignment_id)


# ── Volunteer: self-serve open shifts ────────────────────────────────


def _open_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/open_list.html",
        {"open": _open_shifts(db, person), "error": error},
        status_code=400 if error else 200,
    )


@router.post("/v/open/{event_id}/claim", response_class=HTMLResponse)
def open_claim(
    request: Request,
    event_id: str,
    role: str = Form(...),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Volunteer self-claims an open role. Re-validated server-side
    (org, future, role exists, capacity, no double-claim) so it's
    race-safe; org-scoped direct write (no admin API reuse)."""
    from datetime import datetime

    ev = (
        db.query(Event)
        .filter(Event.org_id == person.org_id, Event.id == event_id)
        .first()
    )
    if ev is None or (ev.start_time and ev.start_time < datetime.utcnow()):
        return _open_list(request, person, db, error="That event is no longer open.")
    rc = (ev.extra_data or {}).get("role_counts") or {}
    if role not in rc:
        return _open_list(request, person, db, error="That role isn't open.")
    rows = (
        db.query(Assignment)
        .join(Event, Assignment.event_id == Event.id)
        .filter(Event.org_id == person.org_id, Assignment.event_id == event_id)
        .all()
    )
    if any(a.person_id == person.id for a in rows):
        return _open_list(request, person, db, error="You're already on this event.")
    if (
        sum(
            1
            for a in rows
            if (a.role or "") == role and (a.status or "").lower() != "declined"
        )
        >= rc[role]
    ):
        return _open_list(request, person, db, error="That role just filled up.")

    db.add(
        Assignment(
            event_id=event_id,
            person_id=person.id,
            role=role,
            solution_id=None,
            status="confirmed",
        )
    )
    db.commit()
    return _open_list(request, person, db)


# ── Volunteer: swap-claim marketplace ────────────────────────────────


def _swaps_open_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/swaps_open_list.html",
        {"swaps": _claimable_swaps(db, person), "error": error},
        status_code=400 if error else 200,
    )


@router.post("/v/swaps/{assignment_id}/claim", response_class=HTMLResponse)
def swap_claim(
    request: Request,
    assignment_id: int,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Cover a teammate's swap: transfer the assignment to the claimer.
    Re-validated server-side (org, still swap_requested, not own, future,
    claimer not already on the event) — org-scoped direct write."""
    from datetime import datetime

    a = (
        db.query(Assignment)
        .join(Event, Assignment.event_id == Event.id)
        .filter(
            Event.org_id == person.org_id,
            Assignment.id == assignment_id,
            Assignment.status == "swap_requested",
        )
        .first()
    )
    if a is None:
        return _swaps_open_list(
            request, person, db, error="That swap is no longer available."
        )
    if a.person_id == person.id:
        return _swaps_open_list(
            request, person, db, error="That's your own swap request."
        )
    ev = db.query(Event).filter(Event.id == a.event_id).first()
    if ev is None or (ev.start_time and ev.start_time < datetime.utcnow()):
        return _swaps_open_list(request, person, db, error="That event has passed.")
    already = (
        db.query(Assignment)
        .join(Event, Assignment.event_id == Event.id)
        .filter(
            Event.org_id == person.org_id,
            Assignment.event_id == a.event_id,
            Assignment.person_id == person.id,
        )
        .first()
    )
    if already is not None:
        return _swaps_open_list(
            request, person, db, error="You're already on that event."
        )

    a.person_id = person.id
    a.status = "confirmed"
    a.decline_reason = None
    db.commit()
    return _swaps_open_list(request, person, db)


# ── Availability: time-off ───────────────────────────────────────────


def _timeoff_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/timeoff_list.html",
        {"timeoff": _my_timeoff(db, person), "error": error},
    )


@router.post("/v/availability/timeoff", response_class=HTMLResponse)
def timeoff_add(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    reason: str | None = Form(None),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        payload = TimeOffCreate(start_date=start_date, end_date=end_date, reason=reason or None)
    except ValueError:
        return _timeoff_list(request, person, db, error="Enter valid dates.")
    try:
        add_timeoff(person.id, payload, db)
    except HTTPException as exc:
        return _timeoff_list(request, person, db, error=str(exc.detail))
    return _timeoff_list(request, person, db)


@router.post("/v/availability/timeoff/{timeoff_id}/delete", response_class=HTMLResponse)
def timeoff_delete(
    request: Request,
    timeoff_id: int,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        delete_timeoff(person.id, timeoff_id, db)
    except HTTPException:
        pass  # already gone — fall through to a fresh (correct) list
    return _timeoff_list(request, person, db)


# ── Availability: recurring rrule ────────────────────────────────────


def _rrule_section(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/rrule_section.html",
        {
            "rrule": _my_rrule(db, person),
            "rrule_presets": RRULE_PRESETS,
            "error": error,
        },
    )


@router.post("/v/availability/rrule", response_class=HTMLResponse)
def rrule_set(
    request: Request,
    rrule: str = Form(...),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        payload = AvailabilityRruleUpdate(rrule=rrule.strip())
    except ValueError:
        return _rrule_section(request, person, db, error="Enter a recurrence rule.")
    set_rrule(person.id, payload, db)
    return _rrule_section(request, person, db)


@router.post("/v/availability/rrule/clear", response_class=HTMLResponse)
def rrule_clear(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    clear_rrule(person.id, db)
    return _rrule_section(request, person, db)


# ── Availability: single-date exceptions ─────────────────────────────


def _exceptions_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/exceptions_list.html",
        {"exceptions": _my_exceptions(db, person), "error": error},
    )


@router.post("/v/availability/exception", response_class=HTMLResponse)
def exception_add(
    request: Request,
    exception_date: str = Form(...),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        payload = AvailabilityExceptionCreate(exception_date=exception_date)
    except ValueError:
        return _exceptions_list(request, person, db, error="Pick a valid date.")
    add_exception(person.id, payload, db)
    return _exceptions_list(request, person, db)


@router.post(
    "/v/availability/exception/{exception_id}/delete",
    response_class=HTMLResponse,
)
def exception_delete(
    request: Request,
    exception_id: int,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    try:
        delete_exception(person.id, exception_id, db)
    except HTTPException:
        pass  # already gone — return a fresh, correct list
    return _exceptions_list(request, person, db)


# ── Profile: calendar subscription ───────────────────────────────────


@router.post("/v/profile/calendar/reset", response_class=HTMLResponse)
def calendar_reset(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Rotate the calendar token (invalidates the old subscription URL)
    and re-render the calendar card with the fresh URL."""
    from web.app import templates

    reset_calendar_token(person.id, person, db, request)
    return templates.TemplateResponse(
        request,
        "partials/calendar_section.html",
        {"calendar": _my_calendar(request, db, person)},
    )


# ── Admin: invite person ─────────────────────────────────────────────


@router.post("/a/people/invite", response_class=HTMLResponse)
def people_invite(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form("volunteer"),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Create an invitation in the admin's org. Returns a small result
    partial (success banner or inline error) swapped into #invite-result.
    The invitee only appears on /a/people once they accept."""
    from web.app import templates

    def _result(ok: bool, msg: str, code: int = 200):
        return templates.TemplateResponse(
            request,
            "partials/invite_result.html",
            {"ok": ok, "msg": msg},
            status_code=code,
        )

    role = role if role in ("volunteer", "admin") else "volunteer"
    try:
        payload = InvitationCreate(name=name, email=email, roles=[role])
    except ValueError:
        return _result(False, "Enter a valid name and email.", 400)
    try:
        create_invitation(payload, BackgroundTasks(), org_id=person.org_id, inviter=person, db=db)
    except HTTPException as exc:
        return _result(False, str(exc.detail), exc.status_code or 400)
    return _result(True, f"Invitation sent to {email}.")


@router.post("/a/people/import", response_class=HTMLResponse)
def people_import(
    request: Request,
    csv_text: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Bulk-import people from pasted CSV (name,email,roles). Each row
    becomes a PersonCreate; reuses the API bulk_import_people so the
    same validation, dedupe, and caps apply."""
    import csv
    import io
    import uuid

    from web.app import templates
    from web.auth import _slugify

    def _render(*, result=None, error=None):
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {"result": result, "error": error},
            status_code=400 if error else 200,
        )

    text = csv_text.strip()
    if not text:
        return _render(error="Paste at least one CSV row (name,email,roles).")

    items = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        cells = [c.strip() for c in row]
        if not cells or not cells[0]:
            continue
        if cells[0].lower() == "name":  # header row
            continue
        name = cells[0]
        email = cells[1] if len(cells) > 1 and cells[1] else None
        roles_raw = cells[2] if len(cells) > 2 and cells[2] else ""
        roles = [r.strip() for r in roles_raw.replace("|", ";").split(";") if r.strip()]
        items.append(
            {
                "id": f"{_slugify(name)}-{uuid.uuid4().hex[:8]}",
                "org_id": person.org_id,
                "name": name,
                "email": email,
                "roles": roles or ["volunteer"],
            }
        )

    if not items:
        return _render(error="No data rows found (a header line alone isn't enough).")

    try:
        resp = bulk_import_people(request, {"items": items}, person.org_id, person, db)
    except HTTPException as exc:
        return _render(error=str(exc.detail))
    return _render(
        result={
            "created": resp.created,
            "skipped": resp.skipped,
            "errors": [{"index": e.index, "reason": e.reason} for e in resp.errors],
        }
    )


# ── Admin: organization settings ─────────────────────────────────────


@router.post("/a/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    name: str = Form(...),
    region: str = Form(""),
    timezone: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Update the admin's own organization. Timezone is merged into the
    org config dict (other config keys preserved). Reuses the API's
    update_organization so the same validation applies."""
    from web.app import templates

    def _render(*, error=None, saved=False, org=None):
        if org is None:
            org = {
                "name": name,
                "region": region or None,
                "timezone": timezone or "",
            }
        return templates.TemplateResponse(
            request,
            "partials/settings_form.html",
            {"org": org, "error": error, "saved": saved},
            status_code=400 if error else 200,
        )

    if not name.strip():
        return _render(error="Organization name is required.")

    current = get_organization(person.org_id, db, current_user=person)
    config = dict(current.config or {})
    tz = timezone.strip()
    if tz:
        config["timezone"] = tz
    else:
        config.pop("timezone", None)

    try:
        payload = OrganizationUpdate(
            name=name.strip(),
            region=region.strip() or None,
            config=config,
        )
    except ValueError:
        return _render(error="Invalid settings.")
    try:
        update_organization(person.org_id, payload, db, current_admin=person)
    except HTTPException as exc:
        return _render(error=str(exc.detail), org=None)

    return _render(
        saved=True,
        org={
            "name": name.strip(),
            "region": region.strip() or None,
            "timezone": tz,
        },
    )


# ── Account / security (self-service) ────────────────────────────────


def _profile_ctx(person: Person, *, saved=False, error=None, name=None) -> dict:
    return {
        "name": name if name is not None else person.name,
        "timezone": person.timezone,
        "language": person.language,
        "saved": saved,
        "error": error,
    }


@router.post("/v/profile", response_class=HTMLResponse)
async def profile_save(
    request: Request,
    name: str = Form(...),
    timezone: str = Form(""),
    language: str = Form(""),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Update the signed-in user's own profile (any role). Reuses the
    API's PUT /people/me handler."""
    from web.app import templates

    def _render(ctx):
        return templates.TemplateResponse(
            request,
            "partials/profile_form.html",
            {"profile": ctx},
            status_code=400 if ctx["error"] else 200,
        )

    if not name.strip():
        return _render(_profile_ctx(person, error="Name is required.", name=""))

    payload = PersonUpdate(
        name=name.strip(),
        timezone=timezone.strip() or None,
        language=language.strip() or None,
    )
    try:
        await update_current_person(payload, person, db)
    except HTTPException as exc:
        return _render(_profile_ctx(person, error=str(exc.detail)))
    return _render(_profile_ctx(person, saved=True))


@router.post("/v/account/password", response_class=HTMLResponse)
def password_change(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Change the signed-in user's password. Reuses the API
    change_password handler, then re-mints the web session cookie so
    the password-version bump doesn't log the user out mid-session."""
    from web.app import templates
    from web.auth import set_session_cookie

    def _err(msg: str):
        return templates.TemplateResponse(
            request,
            "partials/password_form.html",
            {"pw": {"error": msg, "saved": False}},
            status_code=400,
        )

    if new_password != confirm_password:
        return _err("New password and confirmation don't match.")
    if len(new_password) < 6:
        return _err("New password must be at least 6 characters.")

    try:
        change_password(
            ChangePasswordRequest(current_password=current_password, new_password=new_password),
            person,
            db,
        )
    except HTTPException as exc:
        return _err(str(exc.detail))

    resp = templates.TemplateResponse(
        request,
        "partials/password_form.html",
        {"pw": {"error": None, "saved": True}},
    )
    set_session_cookie(resp, person)
    return resp


# ── Admin: teams ─────────────────────────────────────────────────────


def _teams_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/teams_list.html",
        {"teams": _teams(db, person.org_id), "error": error},
        status_code=400 if error else 200,
    )


@router.post("/a/teams/create", response_class=HTMLResponse)
def team_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    import uuid

    from web.auth import _slugify

    if not name.strip():
        return _teams_list(request, person, db, error="Team name is required.")
    payload = TeamCreate(
        id=f"{_slugify(name)}-{uuid.uuid4().hex[:8]}",
        org_id=person.org_id,
        name=name.strip(),
        description=description.strip() or None,
        member_ids=[],
    )
    try:
        create_team(payload, person, db)
    except HTTPException as exc:
        return _teams_list(request, person, db, error=str(exc.detail))
    return _teams_list(request, person, db)


@router.post("/a/teams/{team_id}/update", response_class=HTMLResponse)
def team_update(
    request: Request,
    team_id: str,
    name: str = Form(...),
    description: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    if not name.strip():
        return _teams_list(request, person, db, error="Team name is required.")
    try:
        update_team(
            team_id,
            TeamUpdate(name=name.strip(), description=description.strip() or None),
            person,
            db,
        )
    except HTTPException as exc:
        return _teams_list(request, person, db, error=str(exc.detail))
    return _teams_list(request, person, db)


@router.post("/a/teams/{team_id}/delete", response_class=HTMLResponse)
def team_delete(
    request: Request,
    team_id: str,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        delete_team(team_id, person, db)
    except HTTPException:
        pass  # already gone — return a fresh, correct list
    return _teams_list(request, person, db)


@router.post("/a/teams/{team_id}/members/add", response_class=HTMLResponse)
def team_member_add(
    request: Request,
    team_id: str,
    person_id: str = Form(...),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        add_team_members(team_id, TeamMemberAdd(person_ids=[person_id]), person, db)
    except HTTPException as exc:
        return _teams_list(request, person, db, error=str(exc.detail))
    return _teams_list(request, person, db)


@router.post("/a/teams/{team_id}/members/remove", response_class=HTMLResponse)
def team_member_remove(
    request: Request,
    team_id: str,
    person_id: str = Form(...),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        remove_team_members(team_id, TeamMemberRemove(person_ids=[person_id]), person, db)
    except HTTPException as exc:
        return _teams_list(request, person, db, error=str(exc.detail))
    return _teams_list(request, person, db)


# ── Notifications inbox + email preferences ──────────────────────────


def _inbox_list(request: Request, person: Person, db: Session):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/inbox_list.html",
        {"inbox": _inbox(db, person)},
    )


def _notif_prefs(request: Request, person: Person, db: Session, *, saved=False, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/notif_prefs.html",
        {
            "prefs": _email_prefs(db, person),
            "notif_types": NOTIF_TYPES,
            "saved": saved,
            "error": error,
        },
        status_code=400 if error else 200,
    )


@router.post("/v/inbox/{notification_id}/read", response_class=HTMLResponse)
def inbox_mark_read(
    request: Request,
    notification_id: int,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Org-scoped + recipient-scoped direct update (reusing the API
    handler would query Notification without an org_id filter, which
    the strict tenancy guard rejects)."""
    n = (
        db.query(Notification)
        .filter(
            Notification.org_id == person.org_id,
            Notification.id == notification_id,
            Notification.recipient_id == person.id,
        )
        .first()
    )
    if n is not None and n.opened_at is None:
        n.opened_at = utcnow()
        db.commit()
    return _inbox_list(request, person, db)


@router.post("/v/inbox/read-all", response_class=HTMLResponse)
def inbox_mark_all_read(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """No bulk API endpoint exists — stamp opened_at directly (same
    org-scoped recipient filter the API's per-item handler enforces)."""
    now = utcnow()
    (
        db.query(Notification)
        .filter(
            Notification.org_id == person.org_id,
            Notification.recipient_id == person.id,
            Notification.opened_at.is_(None),
        )
        .update({Notification.opened_at: now}, synchronize_session=False)
    )
    db.commit()
    return _inbox_list(request, person, db)


@router.post("/v/inbox/preferences", response_class=HTMLResponse)
def inbox_save_preferences(
    request: Request,
    frequency: str = Form("immediate"),
    digest_hour: int = Form(8),
    types: list[str] = Form(default=[]),
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    """Upsert the signed-in user's email preferences (org-scoped direct
    write — the API handler isn't org-filtered for the tenancy guard)."""
    if not 0 <= digest_hour <= 23:
        return _notif_prefs(request, person, db, error="Digest hour must be between 0 and 23.")
    if frequency not in ("immediate", "daily", "weekly", "disabled"):
        frequency = "immediate"
    valid_types = [t for t in types if t in NOTIF_TYPES]

    pref = (
        db.query(EmailPreference)
        .filter(
            EmailPreference.org_id == person.org_id,
            EmailPreference.person_id == person.id,
        )
        .first()
    )
    if pref is None:
        import secrets

        pref = EmailPreference(
            person_id=person.id,
            org_id=person.org_id,
            language=person.language or "en",
            timezone=person.timezone or "UTC",
            unsubscribe_token=secrets.token_urlsafe(32),
        )
        db.add(pref)
    pref.frequency = frequency
    pref.enabled_types = valid_types
    pref.digest_hour = digest_hour
    db.commit()
    return _notif_prefs(request, person, db, saved=True)


# ── Admin: manual assignment editing ─────────────────────────────────


def _event_assignments_partial(
    request: Request, person: Person, db: Session, event_id: str, *, error=None
):
    from web.app import templates

    data = _event_assignments(db, person.org_id, event_id)
    if data is None:
        return HTMLResponse(
            '<div id="event-assignments" class="empty">Event not found.</div>',
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "partials/event_assignments.html",
        {"data": data, "error": error},
        status_code=400 if error else 200,
    )


@router.post("/a/events/{event_id}/assignments/add", response_class=HTMLResponse)
def event_assignment_add(
    request: Request,
    event_id: str,
    background_tasks: BackgroundTasks,
    person_id: str = Form(...),
    role: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        manage_assignment(
            event_id,
            AssignmentRequest(person_id=person_id, action="assign", role=role.strip() or None),
            background_tasks,
            person,
            db,
        )
    except HTTPException as exc:
        return _event_assignments_partial(request, person, db, event_id, error=str(exc.detail))
    return _event_assignments_partial(request, person, db, event_id)


@router.post("/a/events/{event_id}/assignments/remove", response_class=HTMLResponse)
def event_assignment_remove(
    request: Request,
    event_id: str,
    background_tasks: BackgroundTasks,
    person_id: str = Form(...),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        manage_assignment(
            event_id,
            AssignmentRequest(person_id=person_id, action="unassign"),
            background_tasks,
            person,
            db,
        )
    except HTTPException:
        pass  # already gone — return a fresh, correct list
    return _event_assignments_partial(request, person, db, event_id)


# ── Admin: swap-request review ───────────────────────────────────────


def _swaps_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/swaps_list.html",
        {"swaps": _swap_requests(db, person.org_id), "error": error},
        status_code=400 if error else 200,
    )


def _owned_swap(db: Session, person: Person, assignment_id: int):
    """The swap-requested assignment in the admin's org, or None."""
    return (
        db.query(Assignment)
        .join(Event, Assignment.event_id == Event.id)
        .filter(
            Event.org_id == person.org_id,
            Assignment.id == assignment_id,
            Assignment.status == "swap_requested",
        )
        .first()
    )


@router.post("/a/swaps/{assignment_id}/approve", response_class=HTMLResponse)
def swap_approve(
    request: Request,
    assignment_id: int,
    background_tasks: BackgroundTasks,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Approve the swap = unassign the volunteer (frees the slot). Reuses
    manage_assignment so the solution-stream publish still fires."""
    a = _owned_swap(db, person, assignment_id)
    if a is None:
        return _swaps_list(request, person, db)
    try:
        manage_assignment(
            a.event_id,
            AssignmentRequest(person_id=a.person_id, action="unassign"),
            background_tasks,
            person,
            db,
        )
    except HTTPException as exc:
        return _swaps_list(request, person, db, error=str(exc.detail))
    return _swaps_list(request, person, db)


@router.post("/a/swaps/{assignment_id}/deny", response_class=HTMLResponse)
def swap_deny(
    request: Request,
    assignment_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Deny the swap = keep the volunteer on; reset status to confirmed."""
    a = _owned_swap(db, person, assignment_id)
    if a is not None:
        a.status = "confirmed"
        db.commit()
    return _swaps_list(request, person, db)


# ── Admin: recurring series ──────────────────────────────────────────


def _recurring_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/recurring_list.html",
        {"series": _recurring(db, person.org_id), "error": error},
        status_code=400 if error else 200,
    )


@router.post("/a/recurring/create", response_class=HTMLResponse)
def recurring_create(
    request: Request,
    title: str = Form(""),
    duration: int = Form(60),
    location: str = Form(""),
    pattern_type: str = Form("weekly"),
    selected_days: list[str] = Form(default=[]),
    weekday_position: str = Form(""),
    weekday_name: str = Form(""),
    frequency_interval: int = Form(0),
    start_date: str = Form(""),
    start_time: str = Form(""),
    end_condition_type: str = Form("count"),
    end_date: str = Form(""),
    occurrence_count: int = Form(0),
    role_name: list[str] = Form(default=[]),
    role_count: list[str] = Form(default=[]),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from datetime import date as _date
    from datetime import time as _time

    if not title.strip():
        return _recurring_list(request, person, db, error="Title is required.")

    role_requirements: dict[str, int] = {}
    for idx, raw in enumerate(role_name):
        nm = raw.strip()
        if not nm:
            continue
        try:
            cnt = int(role_count[idx]) if idx < len(role_count) else 1
        except (TypeError, ValueError):
            continue
        if cnt >= 1:
            role_requirements[nm] = role_requirements.get(nm, 0) + cnt

    try:
        payload = RecurringSeriesCreate(
            title=title.strip(),
            duration=duration,
            location=location.strip() or None,
            role_requirements=role_requirements or None,
            pattern_type=pattern_type,
            frequency_interval=frequency_interval or None,
            selected_days=[d for d in selected_days if d] or None,
            weekday_position=weekday_position or None,
            weekday_name=weekday_name or None,
            start_date=_date.fromisoformat(start_date),
            start_time=_time.fromisoformat(start_time),
            end_condition_type=end_condition_type,
            end_date=_date.fromisoformat(end_date) if end_date else None,
            occurrence_count=occurrence_count or None,
        )
    except (ValidationError, ValueError):
        return _recurring_list(
            request, person, db, error="Check the pattern, dates, and end condition."
        )
    try:
        create_recurring_series(payload, person.org_id, person, db)
    except HTTPException as exc:
        return _recurring_list(request, person, db, error=str(exc.detail))
    return _recurring_list(request, person, db)


@router.post("/a/recurring/{series_id}/delete", response_class=HTMLResponse)
def recurring_delete(
    request: Request,
    series_id: str,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        delete_recurring_series(series_id, person, db)
    except HTTPException:
        pass  # already gone — return a fresh, correct list
    return _recurring_list(request, person, db)


# ── Admin: constraints ───────────────────────────────────────────────


def _constraints_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/constraints_list.html",
        {"constraints": _constraints(db, person.org_id), "error": error},
        status_code=400 if error else 200,
    )


def _parse_constraint_form(ctype: str, weight: str, params: str):
    """Returns (type, weight|None, params|None) or raises ValueError."""
    import json

    ctype = ctype if ctype in ("hard", "soft") else "hard"
    w = weight.strip()
    weight_val = None
    if w:
        try:
            weight_val = int(w)
        except ValueError as exc:
            raise ValueError("Weight must be a whole number.") from exc
    p = params.strip()
    params_val = None
    if p:
        try:
            parsed = json.loads(p)
        except json.JSONDecodeError as exc:
            raise ValueError("Params must be valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Params must be a JSON object.")
        params_val = parsed
    return ctype, weight_val, params_val


@router.post("/a/constraints/create", response_class=HTMLResponse)
def constraint_create(
    request: Request,
    key: str = Form(""),
    type: str = Form("hard"),
    weight: str = Form(""),
    predicate: str = Form(""),
    params: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    if not key.strip():
        return _constraints_list(request, person, db, error="Key is required.")
    if not predicate.strip():
        return _constraints_list(request, person, db, error="Predicate is required.")
    try:
        ctype, weight_val, params_val = _parse_constraint_form(type, weight, params)
    except ValueError as exc:
        return _constraints_list(request, person, db, error=str(exc))
    payload = ConstraintCreate(
        org_id=person.org_id,
        key=key.strip(),
        type=ctype,
        weight=weight_val,
        predicate=predicate.strip(),
        params=params_val,
    )
    try:
        create_constraint(payload, person, db)
    except HTTPException as exc:
        return _constraints_list(request, person, db, error=str(exc.detail))
    return _constraints_list(request, person, db)


@router.post("/a/constraints/{constraint_id}/update", response_class=HTMLResponse)
def constraint_update(
    request: Request,
    constraint_id: int,
    type: str = Form("hard"),
    weight: str = Form(""),
    predicate: str = Form(""),
    params: str = Form(""),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    if not predicate.strip():
        return _constraints_list(request, person, db, error="Predicate is required.")
    try:
        ctype, weight_val, params_val = _parse_constraint_form(type, weight, params)
    except ValueError as exc:
        return _constraints_list(request, person, db, error=str(exc))
    try:
        update_constraint(
            constraint_id,
            ConstraintUpdate(
                type=ctype,
                weight=weight_val,
                predicate=predicate.strip(),
                params=params_val,
            ),
            person,
            db,
        )
    except HTTPException as exc:
        return _constraints_list(request, person, db, error=str(exc.detail))
    return _constraints_list(request, person, db)


@router.post("/a/constraints/{constraint_id}/delete", response_class=HTMLResponse)
def constraint_delete(
    request: Request,
    constraint_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        delete_constraint(constraint_id, person, db)
    except HTTPException:
        pass  # already gone — return a fresh, correct list
    return _constraints_list(request, person, db)


# ── Admin: event create / delete ─────────────────────────────────────


def _events_list(request: Request, person: Person, db: Session, *, error=None):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/events_list.html",
        {"events": _events(db, person.org_id), "error": error},
    )


@router.post("/a/events/create", response_class=HTMLResponse)
def event_create(
    request: Request,
    type: str = Form(...),
    event_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    role_name: list[str] = Form(default=[]),
    role_count: list[str] = Form(default=[]),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Create an event in the admin's org. Combines the date + two time
    inputs into start/end datetimes; generates a unique slug id. Parallel
    role_name/role_count rows become extra_data.role_counts so the solver
    has roles to assign — without this a web-created event gets 0
    assignments (issue #141)."""
    import uuid
    from datetime import datetime

    from web.auth import _slugify

    def _err(msg: str, code: int = 400):
        return _events_list(request, person, db, error=msg)

    try:
        start_dt = datetime.fromisoformat(f"{event_date}T{start_time}")
        end_dt = datetime.fromisoformat(f"{event_date}T{end_time}")
    except ValueError:
        return _err("Enter a valid date and times.")
    if end_dt <= start_dt:
        return _err("End time must be after start time.")

    role_counts: dict[str, int] = {}
    for idx, raw_name in enumerate(role_name):
        name = raw_name.strip()
        if not name:
            continue
        raw_count = role_count[idx] if idx < len(role_count) else "1"
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count < 1:
            continue
        role_counts[name] = role_counts.get(name, 0) + count

    event_id = f"{_slugify(type)}-{uuid.uuid4().hex[:8]}"
    try:
        payload = EventCreate(
            id=event_id,
            org_id=person.org_id,
            type=type,
            start_time=start_dt,
            end_time=end_dt,
            extra_data={"role_counts": role_counts} if role_counts else None,
        )
    except ValueError:
        return _err("Invalid event details.")
    try:
        create_event(payload, person, db)
    except HTTPException as exc:
        return _err(str(exc.detail), exc.status_code or 400)
    return _events_list(request, person, db)


@router.post("/a/events/{event_id}/delete", response_class=HTMLResponse)
def event_delete(
    request: Request,
    event_id: str,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    try:
        delete_event(event_id, person, db)
    except HTTPException:
        pass  # already gone — return a fresh, correct list
    return _events_list(request, person, db)


# ── Admin: run solver ────────────────────────────────────────────────


@router.post("/a/solver/run", response_class=HTMLResponse)
def solver_run(
    request: Request,
    from_date: str = Form(...),
    to_date: str = Form(...),
    mode: str = Form("strict"),
    change_min: str | None = Form(None),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Run the scheduler for the admin's org and render a result summary
    with a link to review the new solution (11.18)."""
    from web.app import templates

    def _err(msg: str, code: int = 400):
        return templates.TemplateResponse(
            request, "partials/solver_result.html", {"error": msg}, status_code=code
        )

    mode = mode if mode in ("strict", "relaxed") else "strict"
    try:
        req = SolveRequest(
            org_id=person.org_id,
            from_date=from_date,
            to_date=to_date,
            mode=mode,
            change_min=(change_min == "true"),
        )
    except ValueError:
        return _err("Enter a valid date range.")
    try:
        resp = solve_schedule(req, person, db)
    except HTTPException as exc:
        return _err(str(exc.detail), exc.status_code or 400)

    m = resp.metrics
    return templates.TemplateResponse(
        request,
        "partials/solver_result.html",
        {
            "r": {
                "solution_id": resp.solution_id,
                "assignment_count": resp.assignment_count,
                "health_score": round(m.health_score),
                "hard_violations": m.hard_violations,
                "solve_ms": round(m.solve_ms),
            }
        },
    )


# ── Admin: publish solution + compare ────────────────────────────────


def _emit_publish_notifications(db: Session, person: Person, sid: int) -> None:
    """Best-effort: create inbox Notification rows for everyone assigned
    in the just-published solution. Inline + org-scoped on purpose —
    notification_service.create_assignment_notifications queries Person
    un-scoped, which the strict tenancy guard rejects. DB-only, no email
    dependency. Never breaks the publish response."""
    try:
        rows = (
            db.query(Assignment.person_id, Assignment.event_id)
            .join(Event, Assignment.event_id == Event.id)
            .filter(
                Event.org_id == person.org_id,
                Assignment.solution_id == sid,
            )
            .all()
        )
        for person_id, event_id in rows:
            db.add(
                Notification(
                    org_id=person.org_id,
                    recipient_id=person_id,
                    type="assignment",
                    status="pending",
                    event_id=event_id,
                    template_data={"event_id": event_id, "solution_id": sid},
                )
            )
        if rows:
            db.commit()
    except Exception:
        db.rollback()


def _emit_reminder_notifications(db: Session, person: Person, sid: int) -> int:
    """Create a `reminder` inbox Notification for each distinct assignee
    in the (published) solution, honoring per-person EmailPreference —
    recipients who removed `reminder` from enabled_types are skipped.
    Org-scoped on purpose (strict tenancy guard); DB-only, no email
    dependency. Returns how many notifications were created."""
    rows = (
        db.query(Assignment.person_id, Assignment.event_id)
        .join(Event, Assignment.event_id == Event.id)
        .filter(Event.org_id == person.org_id, Assignment.solution_id == sid)
        .all()
    )
    first_event: dict[str, str] = {}
    for pid, eid in rows:
        first_event.setdefault(pid, eid)
    if not first_event:
        return 0
    prefs = {
        p.person_id: (p.enabled_types or [])
        for p in db.query(EmailPreference)
        .filter(EmailPreference.org_id == person.org_id)
        .all()
    }
    created = 0
    for pid, eid in first_event.items():
        if pid in prefs and "reminder" not in prefs[pid]:
            continue
        db.add(
            Notification(
                org_id=person.org_id,
                recipient_id=pid,
                type="reminder",
                status="pending",
                event_id=eid,
                template_data={"solution_id": sid},
            )
        )
        created += 1
    if created:
        db.commit()
    return created


def _publish_state(
    request: Request, person: Person, db: Session, sid: int, *, error=None, notified=None
):
    """Re-render #publish-state from the solution's current state
    (is_published + rollback eligibility), always carrying solution_id
    so the swapped fragment's buttons keep working. `notified` is the
    count from a just-run notify action (None = not just notified)."""
    from web.app import templates

    review = _solution_review(db, person, sid)
    if review is None:
        return HTMLResponse(
            '<div id="publish-state" class="empty">Solution not found.</div>',
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "partials/publish_state.html",
        {
            "solution_id": sid,
            "published": review["is_published"],
            "can_rollback": review["can_rollback"],
            "error": error,
            "notified": notified,
        },
        status_code=400 if error else 200,
    )


@router.post("/a/solution/{solution_id}/publish", response_class=HTMLResponse)
def solution_publish(
    request: Request,
    solution_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Publish the solution (unpublishes any prior in the org)."""
    if _solution_owned(db, person, solution_id) is None:
        return HTMLResponse(
            '<div id="publish-state" class="empty">Solution not found.</div>',
            status_code=404,
        )
    try:
        publish_solution(solution_id, request, person, db)
    except HTTPException as exc:
        return _publish_state(request, person, db, solution_id, error=str(exc.detail))
    _emit_publish_notifications(db, person, solution_id)
    return _publish_state(request, person, db, solution_id)


@router.post("/a/solution/{solution_id}/notify", response_class=HTMLResponse)
def solution_notify(
    request: Request,
    solution_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Send a reminder to everyone on the published solution (inbox +
    sandbox email where enabled), honoring each person's preferences."""
    if _solution_owned(db, person, solution_id) is None:
        return HTMLResponse(
            '<div id="publish-state" class="empty">Solution not found.</div>',
            status_code=404,
        )
    review = _solution_review(db, person, solution_id)
    if review is None or not review["is_published"]:
        return _publish_state(
            request,
            person,
            db,
            solution_id,
            error="Publish the solution before notifying assignees.",
        )
    count = _emit_reminder_notifications(db, person, solution_id)
    return _publish_state(request, person, db, solution_id, notified=count)


@router.post("/a/solution/{solution_id}/unpublish", response_class=HTMLResponse)
def solution_unpublish(
    request: Request,
    solution_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Unpublish the solution; volunteers stop seeing its assignments."""
    if _solution_owned(db, person, solution_id) is None:
        return HTMLResponse(
            '<div id="publish-state" class="empty">Solution not found.</div>',
            status_code=404,
        )
    try:
        unpublish_solution(solution_id, request, person, db)
    except HTTPException as exc:
        return _publish_state(request, person, db, solution_id, error=str(exc.detail))
    return _publish_state(request, person, db, solution_id)


@router.post("/a/solution/{solution_id}/rollback", response_class=HTMLResponse)
def solution_rollback(
    request: Request,
    solution_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Re-publish a previously-published solution, replacing the current
    one (rejected with a friendly error if it was never published)."""
    if _solution_owned(db, person, solution_id) is None:
        return HTMLResponse(
            '<div id="publish-state" class="empty">Solution not found.</div>',
            status_code=404,
        )
    try:
        rollback_solution(solution_id, request, person, db)
    except HTTPException as exc:
        return _publish_state(request, person, db, solution_id, error=str(exc.detail))
    return _publish_state(request, person, db, solution_id)


@router.post("/a/compare", response_class=HTMLResponse)
def compare_run(
    request: Request,
    solution_a: int = Form(...),
    solution_b: int = Form(...),
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    def _err(msg: str, code: int = 400):
        return templates.TemplateResponse(
            request,
            "partials/compare_result.html",
            {"diff": None, "error": msg},
            status_code=code,
        )

    if (
        _solution_owned(db, person, solution_a) is None
        or _solution_owned(db, person, solution_b) is None
    ):
        return _err("Pick two solutions from your organization.", 404)
    try:
        diff = compare_solutions(solution_a, solution_b, person, db)
    except HTTPException as exc:
        return _err(str(exc.detail), exc.status_code or 400)
    return templates.TemplateResponse(
        request,
        "partials/compare_result.html",
        {
            "diff": {
                "a": diff.solution_a_id,
                "b": diff.solution_b_id,
                "added": len(diff.added),
                "removed": len(diff.removed),
                "unchanged": diff.unchanged_count,
                "moves": diff.moves,
                "affected": diff.affected_persons,
            },
            "error": None,
        },
    )
