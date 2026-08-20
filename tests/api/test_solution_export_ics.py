"""ICS export for a Solution — /api/v1/solutions/{id}/export with format=ics.

The endpoint historically returned ``501 Not Implemented`` for
``format: "ics"`` with a stale TODO citing a StringIO bug. The utility
``api.utils.calendar_utils.generate_ics_from_events`` already produces
well-formed ICS content for organization-level exports, so this test
suite pins the calendar behaviour we want the endpoint to deliver:

- ``format: "ics"`` returns HTTP 200 with ``text/calendar`` content and
  a body that starts with ``BEGIN:VCALENDAR`` and includes one
  ``VEVENT`` per event in the solution.
- ``scope: "person:{id}"`` filters the calendar to that person's events
  only (no ``VEVENT`` for events they are not assigned to).
- The empty-solution guard (``400`` when the solution has no
  assignments) still applies before format branching.
"""

from datetime import datetime, timedelta

from api.models import Assignment, Event, Person, Solution
from tests.api.conftest import seed_org


def _seed_solution(db, org_id: str) -> Solution:
    sol = Solution(
        org_id=org_id,
        solve_ms=10.0,
        hard_violations=0,
        soft_score=1.0,
        health_score=1.0,
        metrics={},
    )
    db.add(sol)
    db.commit()
    db.refresh(sol)
    return sol


def _seed_person(db, org_id: str, person_id: str, name: str | None = None) -> Person:
    p = Person(id=person_id, org_id=org_id, name=name or person_id.title(), roles=[])
    db.add(p)
    db.commit()
    return p


def _seed_event(
    db,
    org_id: str,
    event_id: str,
    *,
    event_type: str = "Sunday Service",
    days_from_now: int = 7,
) -> Event:
    start = datetime.utcnow().replace(microsecond=0) + timedelta(days=days_from_now)
    e = Event(
        id=event_id,
        org_id=org_id,
        type=event_type,
        start_time=start,
        end_time=start + timedelta(hours=1),
    )
    db.add(e)
    db.commit()
    return e


def _seed_assignment(
    db,
    *,
    solution_id: int,
    event_id: str,
    person_id: str,
    role: str | None = None,
) -> Assignment:
    a = Assignment(
        solution_id=solution_id,
        event_id=event_id,
        person_id=person_id,
        role=role,
    )
    db.add(a)
    db.commit()
    return a


class TestSolutionIcsExport:
    def test_ics_export_returns_calendar_content(self, client, db):
        org_id = "ics-org"
        seed_org(client, org_id)
        _seed_person(db, org_id, "p1", name="Alice")
        _seed_person(db, org_id, "p2", name="Bob")
        _seed_event(db, org_id, "e1", event_type="Sunday Service")
        _seed_event(db, org_id, "e2", event_type="Youth Group")

        sol = _seed_solution(db, org_id)
        _seed_assignment(db, solution_id=sol.id, event_id="e1", person_id="p1", role="usher")
        _seed_assignment(db, solution_id=sol.id, event_id="e2", person_id="p2", role="host")

        resp = client.post(
            f"/api/v1/solutions/{sol.id}/export",
            json={"format": "ics", "scope": "org"},
        )
        assert resp.status_code == 200, resp.text

        content_type = resp.headers.get("content-type", "")
        assert "text/calendar" in content_type, content_type

        disposition = resp.headers.get("content-disposition", "")
        assert f"solution_{sol.id}.ics" in disposition

        body = resp.text
        assert body.startswith("BEGIN:VCALENDAR")
        assert body.rstrip().endswith("END:VCALENDAR")
        # One VEVENT per event in the solution.
        assert body.count("BEGIN:VEVENT") == 2
        assert "SUMMARY:Sunday Service" in body
        assert "SUMMARY:Youth Group" in body

    def test_ics_export_scope_person_filters_to_that_person(self, client, db):
        org_id = "ics-scope"
        seed_org(client, org_id)
        _seed_person(db, org_id, "p1", name="Alice")
        _seed_person(db, org_id, "p2", name="Bob")
        _seed_event(db, org_id, "e-alice", event_type="Alice Slot")
        _seed_event(db, org_id, "e-bob", event_type="Bob Slot")

        sol = _seed_solution(db, org_id)
        _seed_assignment(db, solution_id=sol.id, event_id="e-alice", person_id="p1")
        _seed_assignment(db, solution_id=sol.id, event_id="e-bob", person_id="p2")

        resp = client.post(
            f"/api/v1/solutions/{sol.id}/export",
            json={"format": "ics", "scope": "person:p1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert body.count("BEGIN:VEVENT") == 1
        assert "SUMMARY:Alice Slot" in body
        assert "Bob Slot" not in body

    def test_ics_export_requires_assignments(self, client, db):
        org_id = "ics-empty"
        seed_org(client, org_id)
        sol = _seed_solution(db, org_id)

        resp = client.post(
            f"/api/v1/solutions/{sol.id}/export",
            json={"format": "ics", "scope": "org"},
        )
        assert resp.status_code == 400, resp.text
        assert "no assignments" in resp.json().get("detail", "").lower()

    def test_ics_export_missing_solution_returns_404(self, client, db):
        resp = client.post(
            "/api/v1/solutions/999999/export",
            json={"format": "ics", "scope": "org"},
        )
        assert resp.status_code == 404, resp.text
