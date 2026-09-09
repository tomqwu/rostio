"""Full-page routes. 11.0 ships the shell: landing redirect + minimal
volunteer/admin dashboards proving cookie auth + nav + theming work
end-to-end. Real screens land in 11.1+ (see plan)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from api.database import get_db
from api.models import (
    Assignment,
    Constraint,
    EmailPreference,
    Event,
    Notification,
    Person,
    RecurringSeries,
    Team,
    TeamMember,
)
from web.deps import get_session_admin, get_session_user

router = APIRouter(tags=["web-pages"])


def _row_dict(a: Assignment, e: Event) -> dict:
    start, end = e.start_time, e.end_time
    return {
        "id": a.id,
        "event_type": e.type,
        "date_label": start.strftime("%a %d %b %Y").upper() if start else "",
        "time_label": (
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}" if start and end else ""
        ),
        "role": a.role,
        "status": (a.status or "pending").lower(),
        "decline_reason": a.decline_reason,
    }


def _my_schedule_rows(db: Session, person: Person) -> list[dict]:
    """Assignment ⋈ Event for the caller, by event start. Enriched with
    event type + times — richer than the bare /api/v1/assignments/me
    shape, which omits event details the schedule list needs."""
    rows = (
        db.query(Assignment, Event)
        .join(Event, Assignment.event_id == Event.id)
        .filter(
            Assignment.person_id == person.id,
            Event.org_id == person.org_id,
        )
        .order_by(Event.start_time.asc())
        .all()
    )
    return [_row_dict(a, e) for a, e in rows]


def _my_assignment(db: Session, person: Person, aid: int) -> dict | None:
    """Single Assignment ⋈ Event owned by the caller, or None (404)."""
    row = (
        db.query(Assignment, Event)
        .join(Event, Assignment.event_id == Event.id)
        .filter(
            Assignment.id == aid,
            Assignment.person_id == person.id,
            Event.org_id == person.org_id,
        )
        .first()
    )
    return _row_dict(*row) if row else None


def _pricing_tiers() -> list[dict]:
    """Pricing rows from the canonical UsageService.PLAN_LIMITS so the
    public page can never drift from what the app actually enforces."""
    from api.services.usage_service import UsageService

    order = ["free", "starter", "pro", "enterprise"]
    blurb = {
        "free": "Get started — small teams & trials.",
        "starter": "Growing volunteer programs.",
        "pro": "Established organizations.",
        "enterprise": "Large / multi-ministry. Custom terms.",
    }

    def fmt(v):
        return "Unlimited" if v is None else f"{v:,}"

    rows = []
    for tier in order:
        lim = UsageService.PLAN_LIMITS[tier]
        rows.append(
            {
                "tier": tier,
                "blurb": blurb[tier],
                "volunteers": fmt(lim["volunteers"]),
                "events": fmt(lim["events_per_month"]),
                "storage_mb": fmt(lim["storage_mb"]),
                "api_per_day": fmt(lim["api_calls_per_day"]),
            }
        )
    return rows


@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    """Public pricing page — no auth (mirrors /auth/login)."""
    from web.app import templates

    return templates.TemplateResponse(request, "pricing.html", {"tiers": _pricing_tiers()})


@router.get("/")
def root(request: Request):
    # Resolve session lazily so the bare "/" works signed-in or not.
    from api.database import SessionLocal
    from web.deps import _resolve_person

    db = SessionLocal()
    try:
        person = _resolve_person(request, db)
    finally:
        db.close()
    if person is None:
        return RedirectResponse(url="/auth/login", status_code=303)
    landing = "/a/dashboard" if "admin" in (person.roles or []) else "/v/schedule"
    return RedirectResponse(url=landing, status_code=303)


def _open_shifts(db: Session, person: Person) -> list[dict]:
    """Future events in the volunteer's org with unfilled role capacity
    (extra_data.role_counts vs assignments), excluding events they're
    already on. Org-scoped via the Event join."""
    from datetime import datetime

    now = datetime.utcnow()
    events = (
        db.query(Event)
        .filter(Event.org_id == person.org_id, Event.start_time >= now)
        .order_by(Event.start_time.asc())
        .all()
    )
    out = []
    for e in events:
        rc = (e.extra_data or {}).get("role_counts") or {}
        if not rc:
            continue
        rows = (
            db.query(Assignment)
            .join(Event, Assignment.event_id == Event.id)
            .filter(Event.org_id == person.org_id, Assignment.event_id == e.id)
            .all()
        )
        if any(a.person_id == person.id for a in rows):
            continue  # already on this event — don't offer it
        filled: dict[str, int] = {}
        for a in rows:
            # A declined assignment frees its slot — it must not count
            # as filled, so the role re-opens for self-serve (B8).
            if (a.status or "").lower() == "declined":
                continue
            filled[a.role or ""] = filled.get(a.role or "", 0) + 1
        roles = [
            {"role": r, "needed": n, "remaining": n - filled.get(r, 0)}
            for r, n in rc.items()
            if n - filled.get(r, 0) > 0
        ]
        if roles:
            out.append(
                {
                    "event_id": e.id,
                    "event_type": e.type,
                    "when": e.start_time.strftime("%a %d %b %Y · %H:%M") if e.start_time else "",
                    "roles": roles,
                }
            )
    return out


@router.get("/v/open", response_class=HTMLResponse)
def volunteer_open_shifts(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "volunteer/open.html",
        {"person": person, "active_tab": "schedule", "open": _open_shifts(db, person)},
    )


@router.get("/v/schedule", response_class=HTMLResponse)
def volunteer_schedule(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "volunteer/schedule.html",
        {
            "person": person,
            "active_tab": "schedule",
            "rows": _my_schedule_rows(db, person),
            "unread": _unread_count(db, person),
        },
    )


@router.get("/v/schedule/{assignment_id}", response_class=HTMLResponse)
def volunteer_assignment_detail(
    request: Request,
    assignment_id: int,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    row = _my_assignment(db, person, assignment_id)
    if row is None:
        return templates.TemplateResponse(
            request,
            "volunteer/assignment_detail.html",
            {"person": person, "active_tab": "schedule", "row": None},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "volunteer/assignment_detail.html",
        {"person": person, "active_tab": "schedule", "row": row},
    )


def _my_timeoff(db: Session, person: Person) -> list[dict]:
    """Time-off rows for the caller via the API handler, formatted for
    display."""
    from datetime import date as _date

    from api.routers.availability import get_timeoff

    out = []
    for t in get_timeoff(person.id, db)["timeoff"]:
        s = _date.fromisoformat(t["start_date"])
        e = _date.fromisoformat(t["end_date"])
        out.append(
            {
                "id": t["id"],
                "label": (
                    s.strftime("%d %b %Y").upper()
                    if s == e
                    else f"{s.strftime('%d %b').upper()} – {e.strftime('%d %b %Y').upper()}"
                ),
                "reason": t["reason"],
            }
        )
    return out


# Preset recurring patterns. label → RRULE; the editor also accepts a
# custom expression. Mirrors the mobile kRrulePresets intent.
RRULE_PRESETS = [
    ("Every Sunday", "FREQ=WEEKLY;BYDAY=SU"),
    ("Every Saturday", "FREQ=WEEKLY;BYDAY=SA"),
    ("Weekdays", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
    ("Every other week", "FREQ=WEEKLY;INTERVAL=2"),
    ("Monthly (1st)", "FREQ=MONTHLY;BYMONTHDAY=1"),
]


def _my_rrule(db: Session, person: Person) -> str | None:
    from api.routers.availability import get_rrule

    return get_rrule(person.id, db).rrule


def _my_exceptions(db: Session, person: Person) -> list[dict]:
    """Single-date blocked exceptions for the caller, formatted."""
    from api.routers.availability import list_exceptions

    return [
        {
            "id": r.id,
            "date": r.exception_date.isoformat(),
            "label": r.exception_date.strftime("%a %d %b %Y").upper(),
        }
        for r in list_exceptions(person.id, db)
    ]


@router.get("/v/availability", response_class=HTMLResponse)
def volunteer_availability(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "volunteer/availability.html",
        {
            "person": person,
            "active_tab": "availability",
            "timeoff": _my_timeoff(db, person),
            "rrule": _my_rrule(db, person),
            "rrule_presets": RRULE_PRESETS,
            "exceptions": _my_exceptions(db, person),
        },
    )


def _account_ctx(person: Person) -> dict:
    """Initial profile-form values for the signed-in user."""
    return {
        "name": person.name,
        "timezone": person.timezone,
        "language": person.language,
        "saved": False,
        "error": None,
    }


NOTIF_TYPES = ["assignment", "reminder", "update", "cancellation"]
_NOTIF_LABELS = {
    "assignment": "New assignment",
    "reminder": "Reminder",
    "update": "Schedule update",
    "cancellation": "Cancellation",
}


def _notif_label(t: str | None) -> str:
    key = (t or "").lower()
    return _NOTIF_LABELS.get(key, (t or "Notification").replace("_", " ").title())


def _inbox(db: Session, person: Person) -> dict:
    """The signed-in user's own notifications, newest first."""
    rows = (
        db.query(Notification)
        .filter(
            Notification.org_id == person.org_id,
            Notification.recipient_id == person.id,
        )
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    out = []
    unread = 0
    for n in rows:
        is_unread = n.opened_at is None and n.clicked_at is None
        if is_unread:
            unread += 1
        out.append(
            {
                "id": n.id,
                "label": _notif_label(n.type),
                "event_id": n.event_id,
                "when": n.created_at.strftime("%a %d %b %Y, %H:%M") if n.created_at else "",
                "unread": is_unread,
                "status": (n.status or "").lower(),
            }
        )
    return {"rows": out, "unread": unread}


def _unread_count(db: Session, person: Person) -> int:
    from sqlalchemy import func

    return (
        db.query(func.count(Notification.id))
        .filter(
            Notification.org_id == person.org_id,
            Notification.recipient_id == person.id,
            Notification.opened_at.is_(None),
            Notification.clicked_at.is_(None),
        )
        .scalar()
        or 0
    )


def _email_prefs(db: Session, person: Person) -> dict:
    p = (
        db.query(EmailPreference)
        .filter(
            EmailPreference.org_id == person.org_id,
            EmailPreference.person_id == person.id,
        )
        .first()
    )
    if not p:
        return {
            "frequency": "immediate",
            "enabled_types": list(NOTIF_TYPES),
            "digest_hour": 8,
        }
    return {
        "frequency": p.frequency or "immediate",
        "enabled_types": list(p.enabled_types or NOTIF_TYPES),
        "digest_hour": p.digest_hour if p.digest_hour is not None else 8,
    }


@router.get("/v/inbox", response_class=HTMLResponse)
def volunteer_inbox(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "volunteer/inbox.html",
        {
            "person": person,
            "active_tab": None,
            "inbox": _inbox(db, person),
            "prefs": _email_prefs(db, person),
            "notif_types": NOTIF_TYPES,
        },
    )


def _my_calendar(request: Request, db: Session, person: Person) -> dict:
    """Calendar subscription URL for the caller (token generated lazily
    by the API handler)."""
    from api.routers.calendar import get_subscription_url

    resp = get_subscription_url(person.id, person, db, request)
    return {"https_url": resp.https_url, "webcal_url": resp.webcal_url}


@router.get("/v/profile", response_class=HTMLResponse)
def volunteer_profile(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "volunteer/profile.html",
        {
            "person": person,
            "active_tab": "profile",
            "calendar": _my_calendar(request, db, person),
            "profile": _account_ctx(person),
            "pw": {"error": None, "saved": False},
        },
    )


def _dashboard_kpis(db: Session, person: Person) -> dict:
    """Roll the three analytics endpoints into the dashboard tiles.
    All three return graceful zeros for an empty org.

    Direct in-process call: the API endpoints require an admin Person
    (Sprint 4 PR 4.5d). Pass the caller's session admin so
    `verify_org_member` still runs.
    """
    from api.routers.analytics import (
        get_burnout_risk,
        get_schedule_health,
        get_volunteer_stats,
    )

    org_id = person.org_id
    # Pass the Query-defaulted args explicitly: a direct (non-FastAPI)
    # call doesn't resolve Query(...) sentinels to their defaults.
    vs = get_volunteer_stats(org_id, days=30, current_admin=person, db=db)
    sh = get_schedule_health(org_id, current_admin=person, db=db)
    br = get_burnout_risk(org_id, threshold=4, current_admin=person, db=db)
    latest = sh.get("latest_solution")
    return {
        "active_volunteers": vs["active_volunteers"],
        "total_volunteers": vs["total_volunteers"],
        "participation_rate": vs["participation_rate"],
        "upcoming_events": sh["upcoming_events"],
        "coverage_rate": sh["coverage_rate"],
        "health_score": (round(latest["health_score"]) if latest else None),
        "at_risk_count": br["at_risk_count"],
        "top_volunteers": vs["top_volunteers"][:5],
    }


@router.get("/a/dashboard", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "person": person,
            "active_tab": "dashboard",
            "kpis": _dashboard_kpis(db, person),
            "ob": _onboarding_state(db, person),
        },
    )


ONBOARDING_TOTAL = 4


def _onboarding_state(db: Session, person: Person) -> dict:
    """Derive the 4-step first-run checklist from real org data and
    persist the count to onboarding_progress. Steps are independent
    (a checklist, not a gated wizard) so progress survives any order."""
    from api.models import Invitation, OnboardingProgress, Solution

    org_id = person.org_id
    people_n = db.query(Person).filter(Person.org_id == org_id).count()
    invites_n = db.query(Invitation).filter(Invitation.org_id == org_id).count()
    events_n = db.query(Event).filter(Event.org_id == org_id).count()
    sols_n = db.query(Solution).filter(Solution.org_id == org_id).count()
    pub_n = (
        db.query(Solution)
        .filter(Solution.org_id == org_id, Solution.is_published.is_(True))
        .count()
    )

    steps = [
        {
            "key": "invite",
            "title": "Invite a teammate",
            "desc": "Add the people you schedule.",
            "href": "/a/people",
            "cta": "Invite people",
            "done": invites_n > 0 or people_n > 1,
        },
        {
            "key": "event",
            "title": "Create an event",
            "desc": "Add something that needs volunteers.",
            "href": "/a/events",
            "cta": "Create event",
            "done": events_n > 0,
        },
        {
            "key": "solve",
            "title": "Generate a schedule",
            "desc": "Let the solver build a fair roster.",
            "href": "/a/solver",
            "cta": "Run solver",
            "done": sols_n > 0,
        },
        {
            "key": "publish",
            "title": "Publish it",
            "desc": "Share the schedule with volunteers.",
            "href": "/a/solver",
            "cta": "Publish",
            "done": pub_n > 0,
        },
    ]
    done_n = sum(1 for s in steps if s["done"])

    row = (
        db.query(OnboardingProgress)
        .filter(
            OnboardingProgress.person_id == person.id,
            OnboardingProgress.org_id == org_id,
        )
        .first()
    )
    if row is None:
        row = OnboardingProgress(person_id=person.id, org_id=org_id)
        db.add(row)
    if row.wizard_step_completed != done_n:
        row.wizard_step_completed = done_n
    db.commit()

    return {
        "steps": steps,
        "done": done_n,
        "total": ONBOARDING_TOTAL,
        "complete": done_n >= ONBOARDING_TOTAL,
        "skipped": bool(row.onboarding_skipped),
    }


@router.get("/a/onboarding", response_class=HTMLResponse)
def admin_onboarding(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/onboarding.html",
        {
            "person": person,
            "active_tab": "dashboard",
            "ob": _onboarding_state(db, person),
        },
    )


@router.post("/a/onboarding/skip")
def admin_onboarding_skip(
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from api.models import OnboardingProgress

    row = (
        db.query(OnboardingProgress)
        .filter(
            OnboardingProgress.person_id == person.id,
            OnboardingProgress.org_id == person.org_id,
        )
        .first()
    )
    if row is None:
        row = OnboardingProgress(person_id=person.id, org_id=person.org_id)
        db.add(row)
    row.onboarding_skipped = True
    db.commit()
    return RedirectResponse(url="/a/dashboard", status_code=303)


def _org_settings(db: Session, person: Person) -> dict:
    """Current org settings for the form (timezone lives in config)."""
    from api.routers.organizations import get_organization

    org = get_organization(person.org_id, db, current_user=person)
    config = org.config or {}
    return {
        "name": org.name,
        "region": org.region,
        "timezone": config.get("timezone", ""),
    }


def _email_status() -> dict:
    """Non-secret email-delivery config for the settings page. Reflects
    the same gate the password-reset / invitation senders use."""
    from api.services.email_service import EmailService

    svc = EmailService()
    if not svc.enabled:
        backend = "disabled"
    elif svc.use_sendgrid:
        backend = "SendGrid"
    else:
        backend = "SMTP / Mailtrap"
    return {
        "enabled": svc.enabled,
        "backend": backend,
        "host": None if svc.use_sendgrid else svc.smtp_host,
        "from_email": svc.from_email,
    }


def _sms_status() -> dict:
    """Non-secret SMS-delivery config. Read env directly (never build
    SMSService here — its __init__ raises if SMS_ENABLED without creds)."""
    import os

    enabled = os.getenv("SMS_ENABLED", "false").lower() == "true"
    has_creds = all(
        os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    )
    if enabled and has_creds:
        state = "active"
    elif enabled:
        state = "misconfigured"  # enabled but missing Twilio creds
    else:
        state = "disabled"
    return {"enabled": enabled, "has_creds": has_creds, "state": state}


@router.get("/a/settings", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/settings.html",
        {
            "person": person,
            "active_tab": None,
            "org": _org_settings(db, person),
            "error": None,
            "saved": False,
            "profile": _account_ctx(person),
            "pw": {"error": None, "saved": False},
            "email": _email_status(),
            "sms": _sms_status(),
        },
    )


def _people(db: Session, org_id: str, q: str | None) -> list[dict]:
    """People in the admin's org, optional case-insensitive name/email
    search. Direct query (org-scoped) — simpler than threading the
    API list_people's Query()/pagination deps through a direct call."""
    from sqlalchemy import or_

    query = db.query(Person).filter(Person.org_id == org_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Person.name.ilike(like), Person.email.ilike(like)))
    rows = query.order_by(Person.name.asc()).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "email": p.email,
            "roles": [r.upper() for r in (p.roles or ["volunteer"])],
            "status": (p.status or "active").lower(),
        }
        for p in rows
    ]


def _teams(db: Session, org_id: str) -> list[dict]:
    """Teams in the org with members + the people who could still be
    added (org-scoped direct queries — the API has no team-members
    listing endpoint)."""
    teams = db.query(Team).filter(Team.org_id == org_id).order_by(Team.name.asc()).all()
    people = db.query(Person).filter(Person.org_id == org_id).order_by(Person.name.asc()).all()
    by_id = {p.id: p for p in people}
    out = []
    for t in teams:
        member_ids = [
            m.person_id for m in db.query(TeamMember).filter(TeamMember.team_id == t.id).all()
        ]
        member_set = set(member_ids)
        out.append(
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "members": [
                    {"id": pid, "name": by_id[pid].name} for pid in member_ids if pid in by_id
                ],
                "candidates": [
                    {"id": p.id, "name": p.name} for p in people if p.id not in member_set
                ],
            }
        )
    return out


def _constraints(db: Session, org_id: str) -> list[dict]:
    """Scheduling constraints for the org (direct org-scoped query)."""
    import json

    rows = (
        db.query(Constraint)
        .filter(Constraint.org_id == org_id)
        .order_by(Constraint.key.asc())
        .all()
    )
    return [
        {
            "id": c.id,
            "key": c.key,
            "type": (c.type or "hard").lower(),
            "weight": c.weight,
            "predicate": c.predicate,
            "params_json": json.dumps(c.params) if c.params else "",
        }
        for c in rows
    ]


@router.get("/a/constraints", response_class=HTMLResponse)
def admin_constraints(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/constraints.html",
        {
            "person": person,
            "active_tab": "solver",
            "constraints": _constraints(db, person.org_id),
            "error": None,
        },
    )


@router.get("/a/teams", response_class=HTMLResponse)
def admin_teams(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/teams.html",
        {
            "person": person,
            "active_tab": "people",
            "teams": _teams(db, person.org_id),
            "error": None,
        },
    )


def _analytics(db: Session, person: Person, days: int, threshold: int) -> dict:
    """Deep analytics — the three API endpoints with explicit args
    (their Query() defaults don't resolve on a direct call).

    Direct in-process call: the API endpoints require an admin Person
    (Sprint 4 PR 4.5d), and the caller here (`admin_analytics`) already
    holds one via `get_session_admin`. Pass it as `current_admin` so the
    endpoints' `verify_org_member` still runs.
    """
    from api.routers.analytics import (
        get_burnout_risk,
        get_schedule_health,
        get_volunteer_stats,
    )

    org_id = person.org_id
    vs = get_volunteer_stats(org_id, days=days, current_admin=person, db=db)
    sh = get_schedule_health(org_id, current_admin=person, db=db)
    br = get_burnout_risk(org_id, threshold=threshold, current_admin=person, db=db)
    return {"days": days, "threshold": threshold, "participation": vs, "health": sh, "burnout": br}


@router.get("/a/analytics", response_class=HTMLResponse)
def admin_analytics(
    request: Request,
    days: int = 30,
    threshold: int = 4,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    days = days if 1 <= days <= 365 else 30
    threshold = threshold if 1 <= threshold <= 50 else 4
    return templates.TemplateResponse(
        request,
        "admin/analytics.html",
        {
            "person": person,
            "active_tab": "dashboard",
            "a": _analytics(db, person, days, threshold),
        },
    )


@router.get("/a/people", response_class=HTMLResponse)
def admin_people(
    request: Request,
    q: str | None = None,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/people.html",
        {
            "person": person,
            "active_tab": "people",
            "people": _people(db, person.org_id, q),
            "q": q or "",
        },
    )


@router.get("/a/people/list", response_class=HTMLResponse)
def admin_people_list(
    request: Request,
    q: str | None = None,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """HTMX fragment for live search."""
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "partials/people_list.html",
        {"people": _people(db, person.org_id, q), "q": q or ""},
    )


def _events(db: Session, org_id: str) -> dict:
    """Org events split into upcoming vs past, formatted. Direct query
    (org-scoped) — simpler than the API list_events Query()/pagination
    deps via a direct call."""
    from api.timeutils import utcnow

    now = utcnow()
    rows = db.query(Event).filter(Event.org_id == org_id).order_by(Event.start_time.asc()).all()
    upcoming, past = [], []
    for e in rows:
        item = {
            "id": e.id,
            "type": e.type,
            "date_label": e.start_time.strftime("%a %d %b %Y").upper() if e.start_time else "",
            "time_label": (
                f"{e.start_time.strftime('%H:%M')}–{e.end_time.strftime('%H:%M')}"
                if e.start_time and e.end_time
                else ""
            ),
        }
        (upcoming if e.start_time and e.start_time >= now else past).append(item)
    past.reverse()  # most-recent past first
    return {"upcoming": upcoming, "past": past}


def _billing(db: Session, org_id: str) -> dict:
    """Subscription + usage summary — DB-only services, no Stripe call
    (Stripe-live management is gated on STRIPE_SECRET_KEY)."""
    import os

    from api.services.billing_service import BillingService
    from api.services.usage_service import UsageService

    sub = BillingService(db).get_subscription(org_id)
    try:
        summary = UsageService(db).get_usage_summary(org_id)
    except Exception:
        summary = {}
    usage_rows = []
    for name, v in (summary.get("usage") or {}).items():
        if isinstance(v, dict):
            usage_rows.append(
                {
                    "name": name.replace("_", " ").title(),
                    "current": v.get("current", 0),
                    "limit": v.get("limit", "—"),
                    "percentage": v.get("percentage", 0),
                }
            )
    return {
        "tier": (sub.plan_tier if sub else "free"),
        "status": (sub.status if sub else "—"),
        "billing_cycle": (sub.billing_cycle if sub else None),
        "trial_end": (
            sub.trial_end_date.strftime("%d %b %Y") if sub and sub.trial_end_date else None
        ),
        "has_sub": sub is not None,
        "stripe_ready": bool(os.getenv("STRIPE_SECRET_KEY")),
        "usage_rows": usage_rows,
        "warnings": (summary.get("warnings") or []),
    }


@router.get("/a/billing", response_class=HTMLResponse)
def admin_billing(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/billing.html",
        {"person": person, "active_tab": "dashboard", "b": _billing(db, person.org_id)},
    )


def _claimable_swaps(db: Session, person: Person) -> list[dict]:
    """Swap-requested assignments another volunteer can cover: same org,
    future, not the caller's own request, and not an event the caller is
    already on. Org-scoped via the Event join."""
    from datetime import datetime

    now = datetime.utcnow()
    rows = (
        db.query(Assignment, Event, Person)
        .join(Event, Assignment.event_id == Event.id)
        .join(Person, Assignment.person_id == Person.id)
        .filter(
            Event.org_id == person.org_id,
            Assignment.status == "swap_requested",
            Assignment.person_id != person.id,
            Event.start_time >= now,
        )
        .order_by(Event.start_time.asc())
        .all()
    )
    mine = {
        a.event_id
        for a in db.query(Assignment)
        .join(Event, Assignment.event_id == Event.id)
        .filter(Event.org_id == person.org_id, Assignment.person_id == person.id)
        .all()
    }
    return [
        {
            "assignment_id": a.id,
            "event_type": e.type,
            "when": e.start_time.strftime("%a %d %b %Y · %H:%M") if e.start_time else "",
            "role": a.role,
            "requested_by": p.name,
        }
        for a, e, p in rows
        if e.id not in mine
    ]


@router.get("/v/swaps", response_class=HTMLResponse)
def volunteer_swaps_open(
    request: Request,
    person: Person = Depends(get_session_user),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "volunteer/swaps_open.html",
        {
            "person": person,
            "active_tab": "schedule",
            "swaps": _claimable_swaps(db, person),
        },
    )


def _swap_requests(db: Session, org_id: str) -> list[dict]:
    """Assignments the volunteer flagged for swap, org-scoped via the
    Event join."""
    rows = (
        db.query(Assignment, Event, Person)
        .join(Event, Assignment.event_id == Event.id)
        .join(Person, Assignment.person_id == Person.id)
        .filter(Event.org_id == org_id, Assignment.status == "swap_requested")
        .order_by(Event.start_time.asc())
        .all()
    )
    return [
        {
            "assignment_id": a.id,
            "event_id": a.event_id,
            "event_type": e.type,
            "when": e.start_time.strftime("%a %d %b %Y · %H:%M") if e.start_time else "",
            "person_id": p.id,
            "person_name": p.name,
            "role": a.role,
        }
        for a, e, p in rows
    ]


@router.get("/a/swaps", response_class=HTMLResponse)
def admin_swaps(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/swaps.html",
        {
            "person": person,
            "active_tab": "events",
            "swaps": _swap_requests(db, person.org_id),
            "error": None,
        },
    )


def _all_assignments(db: Session, org_id: str, person_id: str | None) -> dict:
    """Org-wide assignments (Assignment ⋈ Event ⋈ Person), newest event
    first, optionally filtered to one person. Org-scoped via the Event
    join (mirrors api get_all_assignments)."""
    q = (
        db.query(Assignment, Event, Person)
        .join(Event, Assignment.event_id == Event.id)
        .join(Person, Assignment.person_id == Person.id)
        .filter(Event.org_id == org_id)
    )
    if person_id:
        q = q.filter(Assignment.person_id == person_id)
    joined = q.order_by(Event.start_time.desc()).all()
    out = [
        {
            "event_type": e.type,
            "when": e.start_time.strftime("%a %d %b %Y · %H:%M") if e.start_time else "",
            "person_id": p.id,
            "person_name": p.name,
            "role": a.role,
            "status": (a.status or "pending").lower(),
            "manual": a.solution_id is None,
        }
        for a, e, p in joined
    ]
    people = db.query(Person).filter(Person.org_id == org_id).order_by(Person.name.asc()).all()
    return {
        "rows": out,
        "total": len(out),
        "people": [{"id": p.id, "name": p.name} for p in people],
        "person_id": person_id or "",
    }


@router.get("/a/assignments", response_class=HTMLResponse)
def admin_all_assignments(
    request: Request,
    person_id: str | None = None,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/assignments.html",
        {
            "person": person,
            "active_tab": "events",
            "data": _all_assignments(db, person.org_id, person_id),
        },
    )


def _recurring(db: Session, org_id: str) -> list[dict]:
    """Active recurring series for the org (direct org-scoped query)."""
    rows = (
        db.query(RecurringSeries)
        .filter(RecurringSeries.org_id == org_id, RecurringSeries.active.is_(True))
        .order_by(RecurringSeries.created_at.desc())
        .all()
    )
    out = []
    for s in rows:
        if s.pattern_type in ("weekly", "biweekly") and s.selected_days:
            when = ", ".join(d.title()[:3] for d in s.selected_days)
        elif s.pattern_type == "monthly" and s.weekday_position and s.weekday_name:
            when = f"{s.weekday_position} {s.weekday_name.title()}"
        elif s.pattern_type == "custom" and s.frequency_interval:
            when = f"every {s.frequency_interval} wk"
        else:
            when = ""
        if s.end_condition_type == "date" and s.end_date:
            ends = f"until {s.end_date.isoformat()}"
        elif s.end_condition_type == "count" and s.occurrence_count:
            ends = f"{s.occurrence_count}×"
        else:
            ends = "ongoing"
        out.append(
            {
                "id": s.id,
                "title": s.title,
                "pattern": s.pattern_type,
                "when": when,
                "ends": ends,
                "from": s.start_date.isoformat() if s.start_date else "",
            }
        )
    return out


@router.get("/a/recurring", response_class=HTMLResponse)
def admin_recurring(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/recurring.html",
        {
            "person": person,
            "active_tab": "events",
            "series": _recurring(db, person.org_id),
            "error": None,
        },
    )


@router.get("/a/events", response_class=HTMLResponse)
def admin_events(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/events.html",
        {
            "person": person,
            "active_tab": "events",
            "events": _events(db, person.org_id),
        },
    )


def _event_assignments(db: Session, org_id: str, event_id: str) -> dict | None:
    """Current assignees for an event + people who could still be added.
    Org-scoped via the Event join (mirrors _my_schedule_rows)."""
    ev = db.query(Event).filter(Event.org_id == org_id, Event.id == event_id).first()
    if ev is None:
        return None
    rows = (
        db.query(Assignment, Person)
        .join(Person, Assignment.person_id == Person.id)
        .join(Event, Assignment.event_id == Event.id)
        .filter(Event.org_id == org_id, Assignment.event_id == event_id)
        .all()
    )
    assigned_ids = {p.id for _, p in rows}
    people = db.query(Person).filter(Person.org_id == org_id).order_by(Person.name.asc()).all()
    rc = (ev.extra_data or {}).get("role_counts") or {}
    filled_by_role: dict[str, int] = {}
    for a, _ in rows:
        filled_by_role[a.role or ""] = filled_by_role.get(a.role or "", 0) + 1
    coverage = [
        {
            "role": r,
            "needed": n,
            "filled": filled_by_role.get(r, 0),
            "gap": max(0, n - filled_by_role.get(r, 0)),
        }
        for r, n in rc.items()
    ]
    return {
        "event": {
            "id": ev.id,
            "type": ev.type,
            "date_label": ev.start_time.strftime("%a %d %b %Y") if ev.start_time else "",
        },
        "coverage": coverage,
        "fully_staffed": bool(coverage) and all(c["gap"] == 0 for c in coverage),
        "assignees": [
            {
                "assignment_id": a.id,
                "person_id": p.id,
                "name": p.name,
                "role": a.role,
            }
            for a, p in rows
        ],
        "candidates": [{"id": p.id, "name": p.name} for p in people if p.id not in assigned_ids],
    }


@router.get("/a/events/{event_id}/assignments", response_class=HTMLResponse)
def admin_event_assignments(
    request: Request,
    event_id: str,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    data = _event_assignments(db, person.org_id, event_id)
    return templates.TemplateResponse(
        request,
        "admin/event_assignments.html",
        {"person": person, "active_tab": "events", "data": data, "error": None},
        status_code=200 if data else 404,
    )


@router.get("/a/solver", response_class=HTMLResponse)
def admin_solver(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from datetime import timedelta

    from api.timeutils import utcnow
    from web.app import templates

    today = utcnow().date()
    return templates.TemplateResponse(
        request,
        "admin/solver.html",
        {
            "person": person,
            "active_tab": "solver",
            "default_from": today.isoformat(),
            "default_to": (today + timedelta(days=28)).isoformat(),
        },
    )


def _solution_owned(db: Session, person: Person, sid: int):
    """The Solution row iff it belongs to the caller's org, else None."""
    from api.models import Solution

    return db.query(Solution).filter(Solution.id == sid, Solution.org_id == person.org_id).first()


def _solution_events(db: Session, person: Person, sid: int) -> list[dict] | None:
    """Event-grouped assignees for an owned solution, formatted. None →
    404 (unknown or other-org). Shared by the full review page and the
    SSE-driven assignments refetch."""
    from api.routers.solutions import get_solution_assignments

    if _solution_owned(db, person, sid) is None:
        return None
    assignments = get_solution_assignments(sid, db)
    return [
        {
            "event_type": e.event_type or e.event_id,
            "date_label": e.event_start.strftime("%a %d %b %Y · %H:%M").upper()
            if e.event_start
            else "",
            "assignees": [a.person_name or a.person_id for a in e.assignees],
        }
        for e in assignments.events
    ]


def _solution_review(db: Session, person: Person, sid: int) -> dict | None:
    """Solution header + event-grouped assignments + stats for a solution
    in the admin's org. None → 404."""
    from api.routers.solutions import get_solution, get_solution_stats

    if _solution_owned(db, person, sid) is None:
        return None
    from api.models import AuditAction, AuditLog

    detail = get_solution(sid, db)
    stats = get_solution_stats(sid, person, db)
    events = _solution_events(db, person, sid)
    ever_published = (
        db.query(AuditLog)
        .filter(
            AuditLog.organization_id == person.org_id,
            AuditLog.resource_id == str(sid),
            AuditLog.action.in_([AuditAction.SOLUTION_PUBLISHED, AuditAction.SOLUTION_ROLLED_BACK]),
        )
        .count()
        > 0
    )
    return {
        "id": detail.id,
        "health_score": round(detail.health_score),
        "hard_violations": detail.hard_violations,
        "soft_score": round(detail.soft_score, 1),
        "assignment_count": detail.assignment_count,
        "is_published": detail.is_published,
        "can_rollback": ever_published and not detail.is_published,
        "created_label": detail.created_at.strftime("%a %d %b %Y").upper()
        if detail.created_at
        else "",
        "events": events,
        "stats": {
            "fairness_stdev": round(stats.fairness.stdev, 2),
            "workload_max": stats.workload.max_events_per_person,
            "workload_min": stats.workload.min_events_per_person,
            "workload_median": stats.workload.median_events_per_person,
            "distinct": stats.workload.distinct_persons_assigned,
            "total": stats.workload.total_events_assigned,
        },
    }


@router.get("/a/solution/{solution_id}", response_class=HTMLResponse)
def admin_solution_review(
    request: Request,
    solution_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    review = _solution_review(db, person, solution_id)
    if review is None:
        return templates.TemplateResponse(
            request,
            "admin/solution_review.html",
            {"person": person, "active_tab": "solver", "review": None},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "admin/solution_review.html",
        {"person": person, "active_tab": "solver", "review": review},
    )


@router.get("/a/solution/{solution_id}/assignments", response_class=HTMLResponse)
def admin_solution_assignments(
    request: Request,
    solution_id: int,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """HTMX fragment: the event-grouped assignments block, refetched on
    each SSE event."""
    from web.app import templates

    events = _solution_events(db, person, solution_id)
    if events is None:
        return templates.TemplateResponse(
            request,
            "partials/solution_assignments.html",
            {"events": []},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "partials/solution_assignments.html",
        {"events": events},
    )


@router.get("/a/solution/{solution_id}/stream")
async def admin_solution_stream(
    solution_id: int,
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    """Cookie-authed SSE mirror of GET /api/v1/solutions/{id}/assignments
    /stream (which is Bearer-only). Same per-process event_bus topic
    (solution:{id}) the assignment-mutation endpoints publish to."""
    import json

    from fastapi.responses import StreamingResponse

    from api.services import event_bus

    if _solution_owned(db, person, solution_id) is None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Solution not found")

    topic = f"solution:{solution_id}"

    async def _stream():
        yield ": stream open\n\n"
        async for event in event_bus.subscribe(topic):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _org_solutions(db: Session, org_id: str) -> list[dict]:
    """Org solutions newest-first, for the compare pickers."""
    from api.models import Solution

    rows = (
        db.query(Solution)
        .filter(Solution.org_id == org_id)
        .order_by(Solution.created_at.desc())
        .all()
    )
    return [
        {
            "id": s.id,
            "label": f"#{s.id} · "
            + (s.created_at.strftime("%d %b %Y").upper() if s.created_at else "")
            + (" · PUBLISHED" if s.is_published else ""),
        }
        for s in rows
    ]


@router.get("/a/compare", response_class=HTMLResponse)
def admin_compare(
    request: Request,
    person: Person = Depends(get_session_admin),
    db: Session = Depends(get_db),
):
    from web.app import templates

    return templates.TemplateResponse(
        request,
        "admin/compare.html",
        {
            "person": person,
            "active_tab": "solver",
            "solutions": _org_solutions(db, person.org_id),
        },
    )
