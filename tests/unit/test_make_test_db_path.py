"""Keep test database reset and runtime paths aligned and isolated per run."""

import os
import subprocess
from pathlib import Path

from api import database

ROOT = Path(__file__).resolve().parents[2]


def test_test_database_is_not_a_shared_global_file():
    assert database.DATABASE_URL == os.environ["SIGNUPFLOW_TEST_DATABASE_URL"]
    assert database.DATABASE_URL != "sqlite:////tmp/signupflow_test.db"


def test_makefile_exports_the_reset_database_to_pytest():
    source = (ROOT / "Makefile").read_text()
    assert "export SIGNUPFLOW_TEST_DATABASE_URL := $(TEST_DB_URL)" in source
    assert "export DATABASE_URL := $(TEST_DB_URL)" in source
    assert "$(shell mktemp -d" in source
    assert "@rm -f $(TEST_DB_PATH) $(TEST_DB_PATH)-shm $(TEST_DB_PATH)-wal" in source


def test_database_has_no_global_test_override():
    source = (ROOT / "api/database.py").read_text()
    assert "TESTING_FORCE_MEMORY" not in source
    assert "/tmp/signupflow_test.db" not in source


def test_independent_make_runs_use_different_databases():
    recipe = 'test-print-db:\n\t@printf "%s" "$(TEST_DB_URL)"\n'
    urls = []
    for _ in range(2):
        result = subprocess.run(
            ["make", "-s", "-f", "Makefile", "-f", "-", "test-print-db"],
            cwd=ROOT,
            input=recipe,
            text=True,
            capture_output=True,
            check=True,
        )
        urls.append(result.stdout)
    assert urls[0] != urls[1]
    assert all(url.startswith("sqlite:////tmp/signupflow-tests.") for url in urls)


def test_non_test_make_targets_preserve_database_url():
    result = subprocess.run(
        ["make", "-s", "-f", "Makefile", "-f", "-", "print-database"],
        cwd=ROOT,
        input='print-database:\n\t@printf "%s" "$$DATABASE_URL"\n',
        env={**os.environ, "DATABASE_URL": "sqlite:///development-sentinel.db"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == "sqlite:///development-sentinel.db"
