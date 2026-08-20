"""Solutions router - view and export generated solutions."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from api.core.models import (
    Assignment as AssignmentModel,
)
from api.core.models import (
    Event as EventModel,
)
from api.core.models import (
    Person as PersonModel,
)
from api.database import get_db
from api.dependencies import get_current_admin_user, verify_org_member
from api.models import Assignment, AuditAction, AuditLog, Event, Organization, Person, Solution
from api.schemas.common import PaginationParams, get_pagination_params
from api.schemas.solver import (
    AssignmentChange,
    ExportFormat,
    FairnessStats,
    SolutionAssignmentAssignee,
    SolutionAssignmentEntry,
    SolutionAssignmentsResponse,
    SolutionDiffResponse,
    SolutionList,
    SolutionResponse,
    SolutionStatsResponse,
    StabilityMetrics,
    WorkloadStats,
)
from api.services import event_bus
from api.timeutils import utcnow
from api.utils.audit_logger import log_audit_event
from api.utils.pdf_export import generate_schedule_pdf

router = APIRouter(prefix="/solutions", tags=["solutions"])


@router.get("/", response_model=SolutionList)
def list_solutions(
    org_id: str | None = Query(None, description="Filter by organization ID"),
    pagination: PaginationParams = Depends(get_pagination_params),
    db: Session = Depends(get_db),
):
    """List solutions with optional filters."""
    query = db.query(Solution)

    if org_id:
        query = query.filter(Solution.org_id == org_id)

    query = query.order_by(Solution.created_at.desc())
    solutions = query.offset(pagination.offset).limit(pagination.limit).all()
    total = query.count()

    # Add assignment counts
    solution_responses = []
    for sol in solutions:
        assignment_count = db.query(Assignment).filter(Assignment.solution_id == sol.id).count()
        response = SolutionResponse.model_validate(sol)
        response.assignment_count = assignment_count
        solution_responses.append(response)

    return {
        "items": solution_responses,
        "total": total,
        "limit": pagination.limit,
        "offset": pagination.offset,
    }


@router.get("/{solution_id}", response_model=SolutionResponse)
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    """Get solution by ID."""
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )

    assignment_count = db.query(Assignment).filter(Assignment.solution_id == solution.id).count()
    response = SolutionResponse.model_validate(solution)
    response.assignment_count = assignment_count
    return response


@router.get(
    "/{solution_id}/assignments/stream",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE stream of assignment-change events",
        },
        404: {"description": "Solution not found"},
    },
)
async def stream_solution_assignments(
    solution_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: Person = Depends(get_current_admin_user),
):
    """Server-Sent Events stream of assignment-change events for a solution.

    Sprint 10 PR 10.4: replaces pull-to-refresh on the admin Solution
    Review with live updates. Each subscriber gets its own per-process
    asyncio.Queue (see api/services/event_bus.py); publishers fan-out
    via `event_bus.publish(\"solution:{id}\", ...)` from assignment-mutation
    endpoints.

    Format: standard `text/event-stream` per W3C SSE. Each event is a
    JSON object on a single `data:` line. The client reconnects on
    drop; on reconnect it should re-fetch the snapshot via the
    non-stream `/assignments` endpoint and resume.

    Tenant scoping: tenancy via `get_current_admin_user` +
    `verify_org_member` below — the stream only emits events for a
    solution the admin can already read. No org_id is published in the
    event body because the subscriber is already scoped.
    """
    # Scope the lookup by the admin's org so an existence probe across
    # tenants returns the same 404 as an actually-missing solution.
    # Without this scoping, an admin from org A querying a solution in
    # org B would get 403 (the later verify_org_member) while an unknown
    # id returns 404 — leaking cross-tenant solution existence.
    solution = (
        db.query(Solution)
        .filter(Solution.id == solution_id, Solution.org_id == admin.org_id)
        .first()
    )
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Solution not found",
        )

    topic = f"solution:{solution_id}"

    async def _event_stream():
        # Initial comment line so the connection is fully established
        # before the first real event (some clients buffer until first
        # byte arrives).
        yield ": stream open\n\n"
        async for event in event_bus.subscribe(topic):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer
        },
    )


@router.get("/{solution_id}/assignments", response_model=SolutionAssignmentsResponse)
def get_solution_assignments(solution_id: int, db: Session = Depends(get_db)):
    """Get all assignments for a solution, grouped by event.

    Mobile Solution Review renders an event-grouped list, so we group server-side
    rather than forcing the client to do O(n²) regrouping every render.
    """
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )

    # Tenancy-guard requires an org_id filter on any cross-table SELECT;
    # all three tables carry org_id and a single solution belongs to one org.
    rows = (
        db.query(Assignment, Event, Person)
        .outerjoin(Event, Event.id == Assignment.event_id)
        .outerjoin(Person, Person.id == Assignment.person_id)
        .filter(Assignment.solution_id == solution_id)
        .filter((Event.org_id == solution.org_id) | (Event.org_id.is_(None)))
        .filter((Person.org_id == solution.org_id) | (Person.org_id.is_(None)))
        .order_by(Event.start_time.asc().nullslast(), Event.id.asc())
        .all()
    )

    by_event: dict[str, SolutionAssignmentEntry] = {}
    for assignment, event, person in rows:
        entry = by_event.get(assignment.event_id)
        if entry is None:
            entry = SolutionAssignmentEntry(
                event_id=assignment.event_id,
                event_type=event.type if event else None,
                event_start=event.start_time if event else None,
                event_end=event.end_time if event else None,
                assignees=[],
            )
            by_event[assignment.event_id] = entry
        entry.assignees.append(
            SolutionAssignmentAssignee(
                person_id=assignment.person_id,
                person_name=person.name if person else None,
                assignment_id=assignment.id,
                assigned_at=assignment.assigned_at,
            )
        )

    return SolutionAssignmentsResponse(
        solution_id=solution_id,
        events=list(by_event.values()),
        total_assignments=sum(len(e.assignees) for e in by_event.values()),
    )


@router.post("/", response_model=SolutionResponse, status_code=status.HTTP_201_CREATED)
def create_manual_solution(
    solution_data: dict,
    db: Session = Depends(get_db),
):
    """
    Create a manual solution record (for testing or external import).
    Note: This does not create assignments, just the solution metadata.
    """
    # Verify organization exists
    org_id = solution_data.get("org_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail=f"Organization {org_id} not found")

    new_solution = Solution(
        org_id=org_id,
        solve_ms=solution_data.get("solve_ms", 0.0),
        hard_violations=solution_data.get("hard_violations", 0),
        soft_score=solution_data.get("soft_score", 0.0),
        health_score=solution_data.get("health_score", 0.0),
        metrics=solution_data.get("metrics", {}),
        created_at=utcnow(),
    )

    db.add(new_solution)
    db.commit()
    db.refresh(new_solution)

    response = SolutionResponse.model_validate(new_solution)
    response.assignment_count = 0
    return response


@router.post("/{solution_id}/export")
def export_solution(
    solution_id: int,
    export_format: ExportFormat,
    db: Session = Depends(get_db),
):
    """Export solution in various formats (CSV, ICS, JSON)."""
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )

    # Load assignments
    assignments_db = db.query(Assignment).filter(Assignment.solution_id == solution_id).all()
    if not assignments_db:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solution has no assignments",
        )

    # Group assignments by event
    event_assignments = {}
    for a in assignments_db:
        if a.event_id not in event_assignments:
            event_assignments[a.event_id] = []
        event_assignments[a.event_id].append(a.person_id)

    # Load events and people
    event_ids = list(event_assignments.keys())
    events_db = db.query(Event).filter(Event.id.in_(event_ids)).all()
    people_db = db.query(Person).filter(Person.org_id == solution.org_id).all()

    # Convert to core models
    events = [
        EventModel(
            id=e.id,
            type=e.type,
            start=e.start_time,
            end=e.end_time,
            resource_id=e.resource_id,
            team_ids=[],
            required_roles=[],
        )
        for e in events_db
    ]

    people = [
        PersonModel(id=p.id, name=p.name, roles=p.roles or [], skills=[], teams=[])
        for p in people_db
    ]

    assignments = [
        AssignmentModel(event_id=event_id, assignees=person_ids)
        for event_id, person_ids in event_assignments.items()
    ]

    # Create a minimal solution object for export
    from datetime import date

    from api.core.models import (
        FairnessMetrics,
        Metrics,
        SolutionBundle,
        SolutionMeta,
        SolverMeta,
        StabilityMetrics,
        Violations,
    )

    solution_obj = SolutionBundle(
        meta=SolutionMeta(
            generated_at=solution.created_at,
            range_start=date.today(),
            range_end=date.today(),
            mode="greedy",
            change_min=False,
            solver=SolverMeta(name="greedy-solver", version="1.0", strategy="greedy"),
        ),
        assignments=assignments,
        metrics=Metrics(
            hard_violations=solution.hard_violations,
            soft_score=solution.soft_score,
            health_score=solution.health_score,
            solve_ms=solution.solve_ms,
            fairness=FairnessMetrics(
                stdev=(
                    solution.metrics.get("fairness", {}).get("stdev", 0.0)
                    if solution.metrics
                    else 0.0
                ),
                per_person_counts=(
                    solution.metrics.get("fairness", {}).get("per_person_counts", {})
                    if solution.metrics
                    else {}
                ),
            ),
            stability=StabilityMetrics(moves_from_published=0, affected_persons=0),
        ),
        violations=Violations(hard=[], soft=[]),
    )

    # Apply scope filtering if needed
    if export_format.scope.startswith("person:"):
        person_id = export_format.scope.split(":", 1)[1]
        assignments = [a for a in assignments if person_id in a.assignees]
    elif export_format.scope.startswith("team:"):
        # Would need team member lookup
        pass

    # Generate export
    if export_format.format == "json":
        # Return solution as JSON directly
        import json

        content = json.dumps(solution_obj.model_dump(), indent=2, default=str)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=solution_{solution_id}.json"},
        )

    elif export_format.format == "csv":
        # Generate CSV directly without using write_assignments_csv (has StringIO bug)
        import csv
        from io import StringIO

        output = StringIO()
        writer = csv.writer(output)

        # Header
        writer.writerow(["Event ID", "Event Type", "Date", "Time", "Assignees"])

        # Data rows
        for assignment in assignments:
            event = next((e for e in events if e.id == assignment.event_id), None)
            if event:
                assignees = ", ".join([p.name for p in people if p.id in assignment.assignees])
                writer.writerow(
                    [event.id, event.type, event.start.date(), event.start.time(), assignees]
                )

        content = output.getvalue()
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=solution_{solution_id}.csv"},
        )

    elif export_format.format == "ics":
        # Build an event-per-VEVENT calendar (mirrors the CSV shape). The
        # per-person scope filter was already applied above, so ``assignments``
        # here is already restricted to the requested slice.
        from api.utils.calendar_utils import generate_ics_from_events

        person_by_id = {p.id: p for p in people}
        included_event_ids = {a.event_id for a in assignments}
        event_dicts: list[dict] = []
        for e in events_db:
            if e.id not in included_event_ids:
                continue
            assignee_ids = event_assignments.get(e.id, [])
            if export_format.scope.startswith("person:"):
                assignee_ids = [
                    pid for pid in assignee_ids if pid == export_format.scope.split(":", 1)[1]
                ]
            event_dicts.append(
                {
                    "id": e.id,
                    "type": e.type,
                    "start_time": e.start_time,
                    "end_time": e.end_time,
                    "extra_data": e.extra_data or {},
                    "assignments": [
                        {
                            "person": {
                                "name": (person_by_id[pid].name if pid in person_by_id else pid)
                            },
                            "role": None,
                        }
                        for pid in assignee_ids
                    ],
                }
            )

        # Calendar name reflects the org id and (when set) the person scope.
        calendar_name = f"Solution {solution_id} — {solution.org_id}"
        if export_format.scope.startswith("person:"):
            person_id = export_format.scope.split(":", 1)[1]
            person_name = person_by_id[person_id].name if person_id in person_by_id else person_id
            calendar_name = f"Solution {solution_id} — {person_name}"

        content = generate_ics_from_events(
            event_dicts, calendar_name=calendar_name, include_assignments=True
        )
        return Response(
            content=content,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=solution_{solution_id}.ics"},
        )

    elif export_format.format == "pdf":
        # Get organization name
        org = db.query(Organization).filter(Organization.id == solution.org_id).first()
        org_name = org.name if org else solution.org_id

        # Prepare data for PDF generation
        pdf_events = []
        for event in events:
            pdf_events.append(
                {
                    "id": event.id,
                    "type": event.type,
                    "start_time": event.start,
                    "end_time": event.end,
                }
            )

        # Create people mapping (id -> {name, roles})
        people_map = {p.id: {"name": p.name, "roles": p.roles or []} for p in people}

        # Create assignments mapping (event_id -> [person_ids])
        assignments_map = {a.event_id: a.assignees for a in assignments}

        # Get event role requirements
        events_db_map = {e.id: e for e in events_db}

        # Get blocked dates for all people
        from api.models import Availability, VacationPeriod

        blocked_dates_map = {}  # person_id -> list of {start, end}
        for person in people:
            vacations = (
                db.query(VacationPeriod)
                .join(Availability, VacationPeriod.availability_id == Availability.id)
                .filter(Availability.person_id == person.id)
                .all()
            )
            blocked_dates_map[person.id] = [
                {"start": v.start_date, "end": v.end_date} for v in vacations
            ]

        # Generate PDF
        pdf_buffer = generate_schedule_pdf(
            org_name, pdf_events, people_map, assignments_map, events_db_map, blocked_dates_map
        )

        return Response(
            content=pdf_buffer.getvalue(),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=schedule_{solution_id}.pdf"},
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown format: {export_format.format}. Must be json, csv, ics, or pdf",
        )


@router.post("/{solution_id}/publish", response_model=SolutionResponse)
def publish_solution(
    solution_id: int,
    http_request: Request,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Publish a solution (admin only). Unpublishes any prior published in the same org."""
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )
    verify_org_member(current_admin, solution.org_id)

    # Unpublish any prior in the same org.
    prior = (
        db.query(Solution)
        .filter(
            Solution.org_id == solution.org_id,
            Solution.is_published.is_(True),
            Solution.id != solution.id,
        )
        .all()
    )
    for s in prior:
        s.is_published = False
        s.published_at = None

    now = utcnow()
    solution.is_published = True
    solution.published_at = now
    db.commit()
    db.refresh(solution)

    log_audit_event(
        db,
        action=AuditAction.SOLUTION_PUBLISHED,
        user_id=current_admin.id,
        user_email=current_admin.email,
        organization_id=solution.org_id,
        resource_type="solution",
        resource_id=str(solution.id),
        details={"unpublished_prior_ids": [s.id for s in prior]},
        ip_address=http_request.client.host if http_request.client else None,
        user_agent=http_request.headers.get("user-agent"),
    )

    assignment_count = db.query(Assignment).filter(Assignment.solution_id == solution.id).count()
    response = SolutionResponse.model_validate(solution)
    response.assignment_count = assignment_count
    return response


@router.post("/{solution_id}/unpublish", response_model=SolutionResponse)
def unpublish_solution(
    solution_id: int,
    http_request: Request,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Unpublish a solution (admin only)."""
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )
    verify_org_member(current_admin, solution.org_id)

    solution.is_published = False
    solution.published_at = None
    db.commit()
    db.refresh(solution)

    log_audit_event(
        db,
        action=AuditAction.SOLUTION_UNPUBLISHED,
        user_id=current_admin.id,
        user_email=current_admin.email,
        organization_id=solution.org_id,
        resource_type="solution",
        resource_id=str(solution.id),
        ip_address=http_request.client.host if http_request.client else None,
        user_agent=http_request.headers.get("user-agent"),
    )

    assignment_count = db.query(Assignment).filter(Assignment.solution_id == solution.id).count()
    response = SolutionResponse.model_validate(solution)
    response.assignment_count = assignment_count
    return response


@router.get("/{solution_a_id}/compare/{solution_b_id}", response_model=SolutionDiffResponse)
def compare_solutions(
    solution_a_id: int,
    solution_b_id: int,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Diff two solutions (admin only). Both must belong to the same org as the caller."""
    sol_a = db.query(Solution).filter(Solution.id == solution_a_id).first()
    if not sol_a:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_a_id} not found",
        )
    sol_b = db.query(Solution).filter(Solution.id == solution_b_id).first()
    if not sol_b:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_b_id} not found",
        )
    verify_org_member(current_admin, sol_a.org_id)
    verify_org_member(current_admin, sol_b.org_id)

    a_rows = db.query(Assignment).filter(Assignment.solution_id == sol_a.id).all()
    b_rows = db.query(Assignment).filter(Assignment.solution_id == sol_b.id).all()

    a_keys = {(r.event_id, r.person_id, r.role) for r in a_rows}
    b_keys = {(r.event_id, r.person_id, r.role) for r in b_rows}

    removed = a_keys - b_keys
    added = b_keys - a_keys
    unchanged_count = len(a_keys & b_keys)

    affected_persons = sorted({pid for (_, pid, _) in removed | added})

    return SolutionDiffResponse(
        solution_a_id=sol_a.id,
        solution_b_id=sol_b.id,
        added=[AssignmentChange(event_id=e, person_id=p, role=r) for (e, p, r) in added],
        removed=[AssignmentChange(event_id=e, person_id=p, role=r) for (e, p, r) in removed],
        unchanged_count=unchanged_count,
        affected_persons=affected_persons,
        moves=len(added) + len(removed),
    )


@router.post("/{solution_id}/rollback", response_model=SolutionResponse)
def rollback_solution(
    solution_id: int,
    http_request: Request,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Rollback to a previously-published solution (admin only).

    Republishes the target and unpublishes whatever is currently published in
    the same org. The target must have been published at some point before
    (i.e. an audit row recording its publish/rollback exists); otherwise 400.
    """
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )
    verify_org_member(current_admin, solution.org_id)

    # Eligibility: existing publish_solution nulls published_at on the prior
    # when it replaces, so published_at is unreliable. Use the audit trail.
    was_ever_published = (
        db.query(AuditLog)
        .filter(
            AuditLog.action.in_([AuditAction.SOLUTION_PUBLISHED, AuditAction.SOLUTION_ROLLED_BACK]),
            AuditLog.resource_id == str(solution.id),
        )
        .count()
        > 0
    )
    if not was_ever_published:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot roll back to a solution that has never been published",
        )

    prior = (
        db.query(Solution)
        .filter(
            Solution.org_id == solution.org_id,
            Solution.is_published.is_(True),
            Solution.id != solution.id,
        )
        .all()
    )
    for s in prior:
        s.is_published = False
        s.published_at = None

    now = utcnow()
    solution.is_published = True
    solution.published_at = now
    db.commit()
    db.refresh(solution)

    log_audit_event(
        db,
        action=AuditAction.SOLUTION_ROLLED_BACK,
        user_id=current_admin.id,
        user_email=current_admin.email,
        organization_id=solution.org_id,
        resource_type="solution",
        resource_id=str(solution.id),
        details={"unpublished_prior_ids": [s.id for s in prior]},
        ip_address=http_request.client.host if http_request.client else None,
        user_agent=http_request.headers.get("user-agent"),
    )

    assignment_count = db.query(Assignment).filter(Assignment.solution_id == solution.id).count()
    response = SolutionResponse.model_validate(solution)
    response.assignment_count = assignment_count
    return response


@router.get("/{solution_id}/stats", response_model=SolutionStatsResponse)
def get_solution_stats(
    solution_id: int,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Stats endpoint (admin only): fairness histogram + stability + workload distribution."""
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )
    verify_org_member(current_admin, solution.org_id)

    metrics = solution.metrics or {}
    fairness_raw = metrics.get("fairness", {})
    stability_raw = metrics.get("stability", {})

    per_person_counts: dict[str, int] = fairness_raw.get("per_person_counts", {}) or {}

    # Histogram: count_bucket → num_people_with_that_count.
    histogram: dict[str, int] = {}
    for c in per_person_counts.values():
        key = str(c)
        histogram[key] = histogram.get(key, 0) + 1

    counts = list(per_person_counts.values())
    if counts:
        sorted_counts = sorted(counts)
        n = len(sorted_counts)
        if n % 2 == 1:
            median = float(sorted_counts[n // 2])
        else:
            median = (sorted_counts[n // 2 - 1] + sorted_counts[n // 2]) / 2.0
        max_v = max(counts)
        min_v = min(counts)
        total = sum(counts)
        distinct = len(per_person_counts)
    else:
        median = 0.0
        max_v = 0
        min_v = 0
        total = 0
        distinct = 0

    return SolutionStatsResponse(
        solution_id=int(solution.id),
        fairness=FairnessStats(
            stdev=float(fairness_raw.get("stdev", 0.0)),
            per_person_counts=per_person_counts,
            histogram=histogram,
        ),
        stability=StabilityMetrics(
            moves_from_published=int(stability_raw.get("moves_from_published", 0)),
            affected_persons=int(stability_raw.get("affected_persons", 0)),
        ),
        workload=WorkloadStats(
            max_events_per_person=max_v,
            min_events_per_person=min_v,
            median_events_per_person=median,
            total_events_assigned=total,
            distinct_persons_assigned=distinct,
        ),
    )


@router.delete("/{solution_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_solution(solution_id: int, db: Session = Depends(get_db)):
    """Delete solution and all assignments."""
    solution = db.query(Solution).filter(Solution.id == solution_id).first()
    if not solution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Solution {solution_id} not found",
        )

    db.delete(solution)
    db.commit()
    return None
