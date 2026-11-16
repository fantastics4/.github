# 004 — Retire the legacy `@main` reviewer (T013)

## Problem

This repository is public. The legacy reviewer (`.github/workflows/llm-pr-review.yml`,
`scripts/llm_review.py`, `callers/pr-llm-review.yml`, `scripts/apply-callers.sh`) runs
from `@main` with unpinned actions. Any repository that still used it would execute
whatever lands on `main` with its `OPENROUTER_API_KEY` and a write-scoped `GITHUB_TOKEN`.
Spec 001 kept it only until every consumer migrated.

## Solution

The 2026-11-16 inventory (`gh api repos/fantastics4/<repo>/contents/.github/workflows`
for every organization repository) shows all 13 consumers on
`llm-pr-review-v2.yml@e55bd6b6…` and none on `@main`. Remove the four legacy files, drop
their lint exemption, and invert the self-check and tests so the retired paths cannot
return. The `apply_callers.py` migration detection of legacy callers in consumer repos
stays, so a stray old caller is still upgraded.

## Verification

- `ruff check scripts/reviewer_v2 && ruff format --check scripts/reviewer_v2`
- `PYTHONPATH=scripts python3 -m unittest discover -s scripts/reviewer_v2/tests -t scripts -v`
- `PYTHONPATH=scripts python3 scripts/reviewer_v2/selfcheck.py` and `--check-migration`
- Tooling CI green on the PR.

| ID | Task | Status |
|---|---|---|
| T013 | Remove legacy reviewer files, invert self-check/tests, update docs | done |
