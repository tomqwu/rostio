"""DSL data structures for constraint evaluation."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from api.core.models import Event, Person, Team


@dataclass
class EvalContext:
    """Context for evaluating constraints."""

    event: Event | None = None
    person: Person | None = None
    team: Team | None = None
    # NOTE: the field name `date` shadows the `date` symbol inside this class
    # body, so both annotations below must qualify as `datetime.date`.
    # Previously `holidays` resolved to the *field*, not the type.
    date: datetime.date | None = None
    all_events: list[Event] | None = None
    all_people: list[Person] | None = None
    all_teams: list[Team] | None = None
    holidays: dict[datetime.date, bool] | None = None
    params: dict[str, Any] | None = None
    assignments: dict[str, list[str]] | None = None  # event_id -> person_ids
    person_assignments: dict[str, list[Event]] | None = None  # person_id -> events


@dataclass
class ConstraintResult:
    """Result of constraint evaluation."""

    satisfied: bool
    penalty: float = 0.0
    reason: str = ""
