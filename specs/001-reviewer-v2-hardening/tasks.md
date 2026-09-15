# 001 — Reviewer v2 hardening (T001–T010)

Reference plan: `copilot/planes/automated-pr-review-hardening-2026-09/plan.md`
Audit: `copilot/docs/automated-pr-review-audit-2026-09-15.md`
Inventory: `planes/automated-pr-review-hardening-2026-09/evidence.md`

## Problem

The organization-wide PR reviewer never ran for the five audited PRs (caller missing on
`develop` and on PR merge refs; secondary `workflow_run` trigger listened for a workflow
name `CI` that no repository uses). Beyond the trigger gap the audited revision has
correctness defects that can turn invalid model output into a green verdict, truncate the
diff at the first oversized file, accept `finish_reason=length`, never retry timeouts,
trust any comment containing the marker, and deploy from `@main` (workflow and executed
script can drift). Rollout tooling only deploys to the default branch.

## Solution

Stage a versioned v2 reviewer under `scripts/reviewer_v2/` plus a v2 reusable workflow
`.github/workflows/llm-pr-review-v2.yml`, leaving the legacy workflow/script/caller paths
untouched so existing consumers keep working until migration. v2:

- triggers on `pull_request` (`opened`, `synchronize`, `reopened`, `ready_for_review`,
  `edited`) + `workflow_dispatch` with a validated canonical PR number, with a small
  admission job before the review concurrency slot;
- validates the model response against a canonical JSON schema before any verdict;
- bounds and retries HTTP work with typed errors and a global deadline;
- accounts for every file/hunk (coverage manifest) and refuses green on incomplete coverage;
- binds results to head/base/inputs, sets `pending` before the model call, and never
  publishes a stale/mixed result;
- publishes one trusted, actor-authenticated comment plus a versioned result artifact with
  a digest, and an authenticated extractor for the fixer;
- installs callers from a branch-aware inventory with a real dry run and drift check.

## Tasks

| ID | Task | Status |
|---|---|---|
| T001 | Refresh GitHub facts and build the installation inventory | done |
| T002 | Strict schema + parser (`schema.py`, `config.py`) | done |
| T003 | HTTP reliability and budgets (`net.py`) | done |
| T004 | Complete diff accounting (`diffcoverage.py`) | done |
| T005 | Revision freshness + status lifecycle (`review.py`) | done |
| T006 | Authenticated comments/artifacts + fixer extraction (`result.py`, `extract.py`) | done |
| T007 | Trigger template + reusable v2 workflow (`callers/`, `.github/workflows/`) | done |
| T008 | Branch-aware rollout tooling (`apply_callers.py`, `drift_check.py`) | done |
| T009 | Tooling CI + immutable release bundle | done |
| T010 | Documentation and operating procedure | done |

## Verification

- `python3 -m unittest discover -s scripts/reviewer_v2/tests -t .` (offline, no credentials,
  no model calls).
- `ruff check scripts/reviewer_v2` and `ruff format --check scripts/reviewer_v2`.
- `actionlint` + `shellcheck` on changed workflow/shell files.
- Dry-run installer: no writes; second run is a no-op.
- Live canary/rollout stay separate rollout steps (T012/T013) with their own evidence.