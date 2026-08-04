"""Strict-typing gate for `api/core` and `api/schemas` (Sprint 4 PR 4.8).

PR 4.7 made `api/utils` a blocking strict-mypy step in CI. This module
extends the same treatment to the solver domain (`api/core`) and the
Pydantic contract layer (`api/schemas`), and pins the behaviour of the
two latent bugs the strict pass surfaced:

- `EvalContext.date` shadowed the imported `datetime.date` type, so
  `holidays: dict[date, bool]` silently resolved to the *field*, not the
  type.
- `GreedyHeuristicSolver._assign_event` bound `assignees` twice, the
  second time with a conflicting annotation.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime

import pytest

from api.core.constraints.dsl import EvalContext
from api.core.timeutils import parse_rrule

pytestmark = pytest.mark.unit


STRICT_PACKAGES = ["api/core", "api/schemas"]


def test_mypy_strict_clean_for_core_and_schemas() -> None:
    """`mypy api/core api/schemas` must be clean — this gate blocks merges."""
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *STRICT_PACKAGES],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "strict mypy regressed for "
        f"{' '.join(STRICT_PACKAGES)}:\n{result.stdout}\n{result.stderr}"
    )


def test_eval_context_date_field_accepts_a_real_date() -> None:
    """The `date` field holds a `datetime.date`, not the shadowed type alias."""
    ctx = EvalContext(date=date(2026, 8, 4))

    assert ctx.date == date(2026, 8, 4)
    assert isinstance(ctx.date, date)


def test_eval_context_holidays_is_keyed_by_date() -> None:
    """`holidays` maps `datetime.date` -> bool despite the field name clash."""
    holiday = date(2026, 12, 25)
    ctx = EvalContext(date=holiday, holidays={holiday: True})

    assert ctx.holidays is not None
    assert ctx.holidays[holiday] is True
    assert all(isinstance(key, date) for key in ctx.holidays)


def test_parse_rrule_returns_a_concrete_sequence() -> None:
    """`parse_rrule` returns a list — `between()` never yields an iterator."""
    start = datetime(2026, 8, 3)
    until = datetime(2026, 8, 31)

    occurrences = parse_rrule("FREQ=WEEKLY;BYDAY=MO", start, until)

    assert isinstance(occurrences, list)
    assert all(isinstance(occ, datetime) for occ in occurrences)
    # Mondays in the window: Aug 3, 10, 17, 24, 31.
    assert len(occurrences) == 5
    # Re-iterating a list is safe; an exhausted iterator would be empty.
    assert list(occurrences) == list(occurrences)
