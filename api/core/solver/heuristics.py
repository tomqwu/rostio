"""Simple greedy heuristic solver implementation."""

import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from api.core.constraints.dsl import EvalContext
from api.core.constraints.eval import evaluate_constraint
from api.core.models import (
    Assignment,
    Event,
    FairnessMetrics,
    Metrics,
    Patch,
    Person,
    SolutionBundle,
    SolutionMeta,
    SolverMeta,
    StabilityMetrics,
    Violation,
    Violations,
)
from api.core.solver.adapter import SolveContext, SolverAdapter


class GreedyHeuristicSolver(SolverAdapter):
    """Feasible-first greedy solver."""

    def __init__(self) -> None:
        self.context: SolveContext | None = None
        self.weights: dict[str, int] = {}
        self.change_min_enabled: bool = False
        self.change_min_weight: int = 100
        # Loose match (event_id, person_id) — see specs/020-solver-quality-changemin.
        # Solver writes Assignment.role=NULL so a role-strict match would never hit.
        self._prior_published_keys: set[tuple[str, str]] = set()

    def build_model(self, context: SolveContext) -> None:
        """Build internal model from context."""
        self.context = context

    def solve(self, timeout_s: int | None = None) -> SolutionBundle:
        """Solve and return solution bundle."""
        if not self.context:
            raise RuntimeError("Must call build_model first")

        start_time = time.time()

        # Build assignments
        assignments: list[Assignment] = []
        assignment_map: dict[str, list[str]] = {}  # event_id -> person_ids
        person_events: dict[str, list[Event]] = defaultdict(list)

        # Build holiday map
        holiday_map: dict[Any, bool] = {}
        if self.context.holidays:
            for h in self.context.holidays:
                holiday_map[h.date] = h.is_long_weekend

        # Build per-person vacation map: person_id -> list[(start_date, end_date)]
        # so we can filter out unavailable candidates per event.
        self._vacation_by_person: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
        # Per-person exception dates (rrule expansions + one-off AvailabilityException rows).
        self._exception_dates_by_person: dict[str, set[Any]] = defaultdict(set)
        for avail in self.context.availability or []:
            if avail.person_id is None:
                continue
            for vac in avail.vacations:
                self._vacation_by_person[avail.person_id].append((vac.start, vac.end))
            for exc_date in avail.exceptions:
                self._exception_dates_by_person[avail.person_id].add(exc_date)

        # Filter events in range
        events_in_range = [
            e
            for e in self.context.events
            if self.context.from_date <= e.start.date() <= self.context.to_date
        ]

        # Sort events by start time
        sorted_events = sorted(events_in_range, key=lambda e: e.start)

        violations = Violations()

        # Assign each event
        for event in sorted_events:
            assigned = self._assign_event(
                event, person_events, assignment_map, holiday_map, violations
            )
            if assigned:
                assignments.append(assigned)
                for person_id in assigned.assignees:
                    person_events[person_id].append(event)

        # Compute metrics
        solve_time = (time.time() - start_time) * 1000
        metrics = self._compute_metrics(
            solve_time, assignments, person_events, violations, len(self.context.people)
        )

        # Build solution
        meta = SolutionMeta(
            generated_at=datetime.now(),
            range_start=self.context.from_date,
            range_end=self.context.to_date,
            mode=self.context.mode,
            change_min=self.context.change_min,
            solver=SolverMeta(name="greedy_heuristic", version="1.0.0", strategy="feasible_first"),
        )

        return SolutionBundle(
            meta=meta, assignments=assignments, metrics=metrics, violations=violations
        )

    def _assign_event(
        self,
        event: Event,
        person_events: dict[str, list[Event]],
        assignment_map: dict[str, list[str]],
        holiday_map: dict[Any, bool],
        violations: Violations,
    ) -> Assignment | None:
        """Assign people to an event."""
        if not self.context:
            return None

        event_date = event.start.date()
        hard_constraints = [c for c in self.context.constraints if c.severity == "hard"]
        soft_constraints = [c for c in self.context.constraints if c.severity == "soft"]

        # Check event-level hard constraints first
        ctx = EvalContext(
            event=event,
            date=event_date,
            holidays=holiday_map,
            all_events=self.context.events,
            all_people=self.context.people,
            assignments=assignment_map,
            person_assignments=person_events,
        )

        for constraint in hard_constraints:
            if constraint.scope == "event" and event.type in constraint.applies_to:
                result = evaluate_constraint(constraint, ctx)
                if not result.satisfied:
                    violations.hard.append(
                        Violation(
                            constraint_key=constraint.key,
                            severity="hard",
                            message=result.reason,
                            entities=[event.id],
                        )
                    )
                    return None  # Cannot schedule this event

        # Find required roles
        required_roles = event.required_roles
        if not required_roles:
            # No specific requirements, assign from team or skip
            if event.team_ids and self.context.teams:
                team_map = {t.id: t for t in self.context.teams}
                assignees: list[str] = []
                for team_id in event.team_ids:
                    if team_id in team_map:
                        assignees.extend(team_map[team_id].members[:2])  # Take first 2
                return Assignment(
                    event_id=event.id,
                    assignees=assignees[:2],
                    resource_id=event.resource_id,
                    team_ids=event.team_ids,
                )
            return Assignment(event_id=event.id, assignees=[], resource_id=event.resource_id)

        # Assign people to roles. Re-binds the name declared above; the
        # no-required-roles branch always returns before reaching here.
        assignees = []
        people_map = {p.id: p for p in self.context.people}

        for req_role in required_roles:
            candidates = [p for p in self.context.people if req_role.role in p.roles]

            # Score candidates
            scored: list[tuple[float, Person]] = []
            for person in candidates:
                if person.id in assignees:
                    continue  # Already assigned to this event

                # Skip if person is on vacation/time-off covering the event date.
                # Vacation periods are inclusive on both ends.
                vacations = getattr(self, "_vacation_by_person", {}).get(person.id, [])
                if any(v_start <= event_date <= v_end for v_start, v_end in vacations):
                    continue
                # Skip if event date matches a one-off AvailabilityException OR
                # any rrule-expanded blocked date for the person.
                exception_dates = getattr(self, "_exception_dates_by_person", {}).get(
                    person.id, set()
                )
                if event_date in exception_dates:
                    continue

                # Check person-level hard constraints
                ctx.person = person
                hard_ok = True
                for constraint in hard_constraints:
                    if constraint.scope == "person" and event.type in constraint.applies_to:
                        result = evaluate_constraint(constraint, ctx)
                        if not result.satisfied:
                            hard_ok = False
                            break

                if not hard_ok:
                    continue

                # Score soft constraints
                penalty = 0.0
                for constraint in soft_constraints:
                    if constraint.scope == "person" and event.type in constraint.applies_to:
                        result = evaluate_constraint(constraint, ctx)
                        penalty += result.penalty

                # Add fairness: prefer people with fewer assignments
                assignment_count = len(person_events.get(person.id, []))
                penalty += assignment_count * 10

                # Change-minimization bonus: subtract weight if this (event, person)
                # was in the prior published solution. Lower penalty wins.
                if self.change_min_enabled and (event.id, person.id) in self._prior_published_keys:
                    penalty -= self.change_min_weight

                scored.append((penalty, person))

            # Pick best candidates
            scored.sort(key=lambda x: x[0])
            for i in range(min(req_role.count, len(scored))):
                assignees.append(scored[i][1].id)

        # Check if we met role requirements
        for req_role in required_roles:
            count = sum(1 for pid in assignees if req_role.role in people_map[pid].roles)
            if count < req_role.count:
                violations.hard.append(
                    Violation(
                        constraint_key="require_role_coverage",
                        severity="hard",
                        message=f"Role {req_role.role} needs {req_role.count}, got {count}",
                        entities=[event.id],
                    )
                )

        return Assignment(
            event_id=event.id,
            assignees=assignees,
            resource_id=event.resource_id,
            team_ids=event.team_ids,
        )

    def _compute_metrics(
        self,
        solve_ms: float,
        assignments: list[Assignment],
        person_events: dict[str, list[Event]],
        violations: Violations,
        total_people: int,
    ) -> Metrics:
        """Compute solution metrics."""
        import math

        # Count per person
        per_person_counts = {pid: len(events) for pid, events in person_events.items()}

        # Fairness: stdev of counts
        counts = list(per_person_counts.values())
        if counts:
            mean = sum(counts) / len(counts)
            variance = sum((c - mean) ** 2 for c in counts) / len(counts)
            stdev = math.sqrt(variance)
        else:
            stdev = 0.0

        fairness = FairnessMetrics(stdev=stdev, per_person_counts=per_person_counts)

        # Soft score
        soft_score = sum(v.penalty if hasattr(v, "penalty") else 0.0 for v in violations.soft)

        # Health score: 100 if no hard violations, scaled down by soft
        hard_violations = len(violations.hard)
        if hard_violations > 0:
            health_score = 0.0
        else:
            health_score = max(0.0, 100.0 - soft_score / 10)

        stability = StabilityMetrics()

        return Metrics(
            solve_ms=solve_ms,
            hard_violations=hard_violations,
            soft_score=soft_score,
            fairness=fairness,
            stability=stability,
            health_score=health_score,
        )

    def set_objective(self, weights: dict[str, int]) -> None:
        """Set objective function weights."""
        self.weights = weights

    def enable_change_minimization(self, enabled: bool, weight_move_published: int) -> None:
        """Enable/disable change minimization."""
        self.change_min_enabled = enabled
        self.change_min_weight = weight_move_published

    def set_prior_published_keys(self, keys: set[tuple[str, str]]) -> None:
        """Provide ``(event_id, person_id)`` keys from the prior published solution.

        When ``change_min_enabled``, candidates whose tuple is in this set get a
        score bonus equal to ``change_min_weight``. Match is loose (no role) — see
        specs/020-solver-quality-changemin/spec.md.
        """
        self._prior_published_keys = keys

    def incremental_update(self, changes: Patch) -> None:
        """Apply incremental changes to model."""
        # Not implemented for heuristic solver
        pass
