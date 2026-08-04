"""Guard: the Makefile must reset the database the tests actually use.

`tests/conftest.py` forces `TESTING_FORCE_MEMORY=true`, which makes
`api/database.py` bind to `/tmp/signupflow_test.db` regardless of
`DATABASE_URL`. The Makefile used to define its test DB as
`./test_roster.db` and `rm` that file between runs — a file the suite
never reads.

Combined with the Makefile's `SKIP_TEST_DB_FIXTURES=true` (which skips the
per-test truncation fixture), nothing ever reset the real database, so
`/tmp/signupflow_test.db` accumulated rows across every run and
fixed-ID create-tests eventually collided with 409 CONFLICT. `make
test-all` was red on any machine that had run the suite before, while CI
— which runs bare `pytest` on a fresh runner — stayed green.

These tests pin the two halves of that contract so the paths cannot drift
apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
CONFTEST = REPO_ROOT / "tests" / "conftest.py"

# The path api/database.py binds to when TESTING_FORCE_MEMORY is set.
FORCED_TEST_DB = "/tmp/signupflow_test.db"


def _makefile_var(name: str) -> str:
    """Return the literal right-hand side of a `NAME := value` assignment."""
    match = re.search(rf"^{re.escape(name)}\s*:?=\s*(.+)$", MAKEFILE.read_text(), re.MULTILINE)
    assert match is not None, f"{name} not found in Makefile"
    return match.group(1).strip()


def test_database_module_forces_the_tmp_path_under_testing() -> None:
    """`api/database.py` redirects to the /tmp DB when TESTING_FORCE_MEMORY is set."""
    source = (REPO_ROOT / "api" / "database.py").read_text()

    assert 'os.getenv("TESTING_FORCE_MEMORY") == "true"' in source
    assert FORCED_TEST_DB in source


def test_conftest_sets_testing_force_memory() -> None:
    """conftest turns that redirect on for every test run."""
    source = CONFTEST.read_text()

    assert 'os.environ["TESTING_FORCE_MEMORY"] = "true"' in source


def test_makefile_test_db_path_matches_the_db_tests_use() -> None:
    """The Makefile's TEST_DB_PATH must be the DB the suite actually binds to."""
    assert _makefile_var("TEST_DB_PATH") == FORCED_TEST_DB


def test_makefile_never_targets_the_unused_roster_db() -> None:
    """No Makefile recipe should still be resetting the stale test_roster.db."""
    offenders = [
        line.strip()
        for line in MAKEFILE.read_text().splitlines()
        if "test_roster.db" in line and not line.lstrip().startswith("#")
    ]

    assert offenders == [], (
        "Makefile still references test_roster.db, which the test suite never "
        f"reads (it binds to {FORCED_TEST_DB}):\n" + "\n".join(offenders)
    )
