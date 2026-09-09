# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The cross-agent baseline lives in [`AGENTS.md`](./AGENTS.md). Read it first when rules in the two files appear to overlap; Claude-specific addenda below take precedence inside Claude Code sessions.

**Read the constitution:** `.specify/memory/constitution.md` — single source of truth for project principles.

## Agent instruction hierarchy

When rules overlap, follow the more specific and safer one. Precedence:

1. The user's request in the current task.
2. This file (`CLAUDE.md`) and `AGENTS.md`.
3. Path-scoped rules under `.github/instructions/` (Copilot) when applicable.
4. General guidance in `docs/ai-agent-coding-strategy.md`.
5. Inferred best practice.

## Project

SignUpFlow is a headless volunteer scheduling and sign-up management API + CLI (churches, sports leagues, non-profits). It uses a greedy heuristic solver with constraint-based optimization to auto-generate fair schedules.

- **Backend:** FastAPI + SQLAlchemy 2.0 + Pydantic 2.x (Python 3.11+)
- **CLI:** YAML workspace in, JSON solution out (`api.cli.main`)
- **Database:** SQLite (dev: `roster.db`), PostgreSQL (prod via Docker)
- **Auth:** JWT (HS256, 24h expiry) + bcrypt password hashing

### Disabled Features

Billing (Stripe), email (SendGrid), SMS (Twilio), and notification routers are **not registered** in `api/main.py`. Their service files and models remain in the codebase but are inactive. Tests for these features are skipped via `pytestmark`.

## Commands

```bash
make setup                # First-time: install deps, run migrations, seed data
make run                  # Dev server on :8000 (uvicorn --reload)
make test                 # Backend comprehensive tests
make test-all             # Full suite: unit + api + cli + integration
make test-unit            # Python unit tests only
make test-unit-fast       # Unit tests excluding slow bcrypt tests (~7s)

# Individual test commands
poetry run pytest tests/unit/test_events.py -v                    # One test file
poetry run pytest tests/unit/test_events.py::test_create_event -v # One test function

# Database
make migrate              # Run Alembic migrations
# Reset: rm roster.db && make migrate
```

## Architecture

### Active API Routers (registered in `api/main.py`)

```
/api/v1/auth           — signup, login, refresh, email check
/api/v1/organizations  — CRUD for organizations
/api/v1/people         — CRUD for people, /me profile
/api/v1/teams          — CRUD for teams + membership
/api/v1/events         — CRUD for events + assignments
/api/v1/constraints    — CRUD for scheduling constraints (DSL-based)
/api/v1/solver         — POST /solve to generate schedules
/api/v1/solutions      — list/view/stats, compare, publish/unpublish/rollback
/api/v1/availability   — time-off / blocked dates
/api/v1/conflicts      — conflict checking between person+event
/api/v1/invitations    — create/verify/accept invitation tokens
/api/v1/calendar       — ICS export of personal schedules
/api/v1/analytics      — volunteer stats, event stats
/api/v1/password-reset — request/confirm password reset
/api/v1/notifications  — list / read / unread-count, email preferences (mobile Inbox)

Bare `/api` is a 308 redirect to `/api/v1` for one release.
```

### Key Files

```
api/main.py              # App entry, router registration
api/cli/                 # YAML workspace CLI (init, solve)
api/dependencies.py      # Auth: get_current_user(), get_current_admin_user(), verify_org_member()
api/security.py          # JWT creation/verification, bcrypt hashing
api/models.py            # SQLAlchemy ORM (Organization, Person, Event, Team, Assignment, Constraint, ...)
api/database.py          # Engine, session factory, get_db() dependency
api/core/solver/         # GreedyHeuristicSolver + constraint evaluation engine
api/core/models.py       # In-memory domain models for solver (separate from ORM models)
api/core/constraints/    # Constraint DSL: eval.py (evaluator), predicates.py (built-in predicates)
api/schemas/             # Pydantic request/response models per domain
```

### Tests

```
tests/conftest.py              # Fixtures: auto-mocks auth for unit, real auth for integration
tests/unit/                    # Fast, mocked auth
tests/api/                     # Real HTTP + JWT against in-memory DB
tests/cli/                     # Subprocess CLI: YAML in, JSON out
tests/integration/             # Real DB tests
tests/comprehensive_test_suite.py  # Full API workflow tests
tests/setup_test_data.py       # Seed data for test DB
```

## Key Patterns

**Authentication:** Backend uses `Depends(get_current_user)` or `Depends(get_current_admin_user)` for route protection.

**Multi-tenancy:** Every query MUST filter by `org_id`. Use `verify_org_member(person, org_id)` from `api/dependencies.py` to enforce org isolation.

**Organizations:** Keep `POST /api/v1/organizations/` public only for creating an empty onboarding organization. Require membership for organization reads/listing and same-tenant admin access for update/delete/cancel/restore. Commit each lifecycle mutation and its audit record together. Public signup membership hardening remains tracked in #255.

**RBAC:** Two roles: `volunteer` (view own data, manage availability) and `admin` (full CRUD, solver, invitations). Roles stored as JSON array on Person model.

**Test auth mocking:** Unit tests auto-mock authentication via `conftest.py` (returns a test admin user). Integration tests use real auth. Mark tests with `@pytest.mark.no_mock_auth` to opt out of mocking.

## Testing Rules

Write tests first (TDD), implement to make them pass, then run `make test-unit` to verify no regressions. All tests must pass before committing.

Pytest markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.slow`, `@pytest.mark.no_mock_auth`.

## PR rules

1. **Run tests after every code change.** After any edit to code or tests, run `make test-unit` (or `make test-unit-fast` during iteration). The change is not "done" until local tests pass. Run `make test-all` before pushing a PR.
2. **Commit and let CI run.** After local tests pass, commit and push. Do not declare a change shippable based on local results alone — wait for CI on the branch.
3. **Merge only when CI is green and Codex local review reports no blocking issues** (see next section).

## PR Workflow With Codex Review

PR review is run automatically by `openai/codex-action` in CI
(`.github/workflows/codex-review.yml`). On every PR push, the action
checks out the merge ref, runs Codex against the diff, and posts the
verdict as a PR comment.

Prereq: the `OPENAI_API_KEY` repo secret must be set. Without it the
precondition job emits a warning and skips review; the rest of CI
still runs.

If you need to re-run a review (e.g., after fixing a finding), just
push another commit — the workflow's concurrency group cancels the
prior run and starts a fresh review on the new head.

For local iteration before pushing, the legacy
`openai/codex-plugin-cc` plugin still works:

```
git fetch origin main
/codex:review --base origin/main
```

Use it when you want a verdict without opening a PR, or when CI is
unavailable. For routine PR review, lean on the CI action — it runs
without anyone having to remember.

Do not self-approve by posting `LGTM` markers.
Do not require or wait for the old GitHub `codex-pr-review-gate` check.

A PR may merge only when:
1. CI is green.
2. GitHub says the PR is mergeable.
3. Codex local review reports no blocking issues.
4. There are no unresolved review comments or merge conflicts.

If Codex review reports blockers:
1. Keep the PR open.
2. Fix the issues.
3. Run relevant local checks.
4. Push a follow-up commit.
5. Run Codex review again.

If the PR has merge conflicts:
1. Update the branch against the latest base branch.
2. Resolve conflicts carefully.
3. Run relevant local checks.
4. Push the resolution.
5. Run Codex review again.

If CI passes and Codex review passes:
- Merge the PR using the repository's normal merge method.
- Do not manually close the PR as the success path.

If GitHub blocks the merge:
- Report the exact blocker.
- Leave the PR open.

Only close without merging if the work is abandoned, duplicated, or superseded,
and leave a PR comment explaining why.

## Common Gotchas

- Missing `org_id` filter in DB queries → cross-tenant data leaks
- Password-related tests slow → use `make test-unit-fast` to skip bcrypt tests during iteration
