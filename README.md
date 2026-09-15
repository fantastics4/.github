# fantastics4/.github

Organization-wide reusable workflows and tooling.

## `llm-pr-review` — automatic LLM code review on pull requests

The reviewer publishes, on every reviewed revision:

- **one trusted sticky comment** (`<!-- llm-pr-review -->`) with the findings,
- a **label** `llm-review:green` / `llm-review:red`,
- a **custom commit status** `llm-review` on the reviewed head SHA,
- a **versioned result artifact** (`llm-review-result-<pr>-<sha12>-a<attempt>`) with a
  payload digest, used for large results and as verifiable evidence. The attempt is part of
  the name because the artifact API rejects a repeated name inside one run.

### Two workflows, one migration window

| Path | State | Executed revision |
|---|---|---|
| `.github/workflows/llm-pr-review-v2.yml` + `scripts/reviewer_v2/` | **current, hardened** | `job.workflow_sha` (same commit as the caller pin) |
| `.github/workflows/llm-pr-review.yml` + `scripts/llm_review.py` | legacy, still executed by consumers pinned to `@main` | `@main` (unchanged) |

Legacy callers keep working unchanged until they migrate. Nothing in
`scripts/reviewer_v2/` changes legacy behaviour, and the legacy paths are only retired
after the installation inventory shows no remaining consumers.

### Triggers

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, edited]
  workflow_dispatch:
    inputs:
      pr_number: { description: Pull request number, required: true, type: string }
```

- `workflow_run` is **gone**: the reviewer is independent of CI and reports its own
  outcome. It never consumes CI results and CI completion never triggers a second review.
- Drafts are skipped automatically; a manual dispatch reviews them on purpose (`allow_draft`).
- `edited` runs only when the review inputs change (title, body/objective, base branch).
- Manual numbers must be **canonical decimal** (`^[1-9][0-9]*$`): padding, signs,
  whitespace and exponent notation are rejected so two spellings can never produce two
  concurrency keys for the same PR.
- A small **admission job** validates eligibility and reads the current PR metadata
  *before* the review takes its concurrency slot, so an irrelevant edit, an invalid
  dispatch or a skipped draft can never cancel a running valid review. Admission holds
  no model secret and never checks out the pull request.
- Fork / restricted-secret PRs are skipped with a reason; review them with a manual
  dispatch from the base repository instead of weakening secret visibility.

### Inputs

| Input | Required | Default | Purpose |
|---|---|---|---|
| `pr_number` | yes | — | Pull request to review (canonical decimal) |
| `model` | no | `z-ai/glm-5.3-flash` | Any OpenRouter model id that supports strict structured output |
| `max_diff_chars` | no | `500000` | Diff size budget (cost control) |
| `max_completion_tokens` | no | `65536` | Completion cap per request |
| `reasoning_effort` | no | `high` | `low`/`medium`/`high`/`max` or empty for the model default |
| `test_conventions` | no | `""` | Repo test layout so suggested tests are realistic |
| `gate` | no | `false` | When `true` a red verdict also fails the Actions job |
| `allow_draft` | no | `false` | Review a draft on purpose (manual dispatch) |
| `blocking_categories` | no | `security,data-loss,breaking` | Categories that block |
| `blocking_bug_severities` | no | `critical,high` | Bug severities that block |

Additional budgets are environment-level and validated before any paid inference:
`REQUEST_TIMEOUT_SECONDS`, `MAX_ATTEMPTS`, `TOTAL_BUDGET_SECONDS`, `MAX_CHUNKS`,
`COMPLETION_RESERVE_TOKENS`, `MODEL_CONTEXT_TOKENS`, `MAX_COMMENT_CHARS`.

### Findings are probabilistic; the verdict is not

The model supplies findings and their category/severity. **Trusted Python computes**
identity, revision binding, coverage, the `blocking` flag per issue and the verdict.
Only `security`, `data-loss` and `breaking`, plus `critical`/`high` bugs, turn a PR red;
missing tests, style, docs, performance and human/production decisions never block.

`review_state` is one of:

| State | Meaning | Verdict | Job |
|---|---|---|---|
| `complete` | every required file/hunk reviewed and findings verified | `green`/`red` | fails only when `red` and `gate=true` |
| `incomplete` | coverage or chunk failure; partial findings are kept | `null` | fails |
| `error` | API/schema/publication failure (including artifact) | `null` | fails |
| `stale` | head/base/objective changed while reviewing; nothing published | `null` | succeeds, status left as an error/never-green |
| `skipped` | draft/closed/fork/invalid dispatch; no model call | `null` | succeeds |

The reusable workflow exposes `verdict` **and** `review_state`; for non-complete
results the legacy `verdict` output is empty, so an old consumer cannot read it as approval.

### Coverage, freshness and honesty

- Every changed file is **included**, **excluded** with a documented reason
  (generated/binary artefacts), **failed** (unsplittable hunk, chunk budget) or
  **missing** (no textual patch for a file that changes). A non-complete coverage
  manifest can never be green.
- Results are bound to `head_sha`, `base_sha` and an `inputs_hash` over the PR
  title/body/objective. Any change during collection, inference or publication discards
  the result as `stale` instead of publishing it as current.
- `pending` is posted before the model call; a rerun supersedes an older success instead
  of leaving it in place. Consumers must treat an abandoned `pending` as unreviewed.
- When two open pull requests share a head SHA, the custom status is inherently ambiguous
  (statuses are keyed by SHA, not by PR). The result records `shared_head_prs`, states the
  limitation, and consumers must use the PR-specific result/artifact — never the bare status.
- Base-branch pushes invalidate reviews of PRs that target that branch. The trusted
  dispatcher `pr-llm-review-refresh.yml` enumerates the affected open PRs and requests a
  review through `workflow_dispatch` (the one token-generated event that starts a run);
  it deduplicates against the current head/base/input/config identity and never holds the
  model secret. Until it runs, an old head-SHA status can still look green — consumers
  must check metadata, not only the status.

### Required secret

`OPENROUTER_API_KEY` must exist as a **repository secret in every repository** that
installs the caller (organization secrets are not usable by private repositories on
GitHub Free). Check or set it without printing values:

```bash
scripts/check-openrouter-key.sh     # validates a key you paste
scripts/set-openrouter-secret.sh    # stores a validated key in every repository
```

### Rolling it out

`callers/pr-llm-review-v2.yml` and `callers/pr-llm-review-refresh-v2.yml` are the
templates; `scripts/reviewer_v2/apply_callers.py` installs them on **every maintained
PR-target branch** (`main`, `develop`, `master` when present, or the branches named in an
inventory file):

```bash
# preview: prints every repository/branch decision, writes nothing
python3 scripts/reviewer_v2/apply_callers.py --dry-run --release-sha <40-hex>
# install: opens one PR per (repository, branch); no direct pushes
python3 scripts/reviewer_v2/apply_callers.py --release-sha <40-hex>
# read-only drift check (coverage + pins)
python3 scripts/reviewer_v2/drift_check.py --release-sha <40-hex>
```

The installer requires an explicit reviewed release SHA (no branch default), compares
content before writing (a repeat run is a no-op), reports a missing secret by name,
preserves customized callers unless `--include-customized` is given, and reuses an
existing open rollout PR.

### Reading a verdict (fixer)

```bash
python3 scripts/reviewer_v2/extract.py fantastics4/<repo> <pr>          # fixer payload
python3 scripts/reviewer_v2/extract.py fantastics4/<repo> <pr> --format full
```

Extraction verifies the actor (`github-actions[bot]`), the marker at the start of the
comment, the run provenance (expected caller path, attempt, PR association), the artifact
and its payload digest, the current head/base/inputs, and the latest terminal `llm-review`
status. Exit codes: `0` complete result, `1` unreadable, `2` not usable
(incomplete/error/stale/legacy/expired), `3` verification failed. See
[docs/llm-fixer.md](docs/llm-fixer.md).

### Troubleshooting

| Symptom | Where to look |
|---|---|
| No run at all | the caller exists on the PR's **target branch** and on the PR merge ref; check the *Admission* job output |
| Run skipped | Admission reason (draft, fork, closed, irrelevant edit, non-canonical number) |
| Provider error / schema failure | the run log: model call lines carry model, provider, attempts, latency, finish reason; `review_state=error` and the status description |
| `review_state=incomplete` | the coverage section of the comment: failed/missing files and the reasons |
| Stale status | the result is bound to a newer head/base; wait for the run triggered by the change or dispatch the refresh |
| Missing artifact | the run's artifact list; the comment then carries the compact envelope with the failure reason |
| Check name | custom status context is `llm-review`; the Actions job is *LLM review v2* |

### Rollback

Revert the installed callers to the previous reviewed release SHA with a PR (they are
plain files). Preserve the failing/pending status instead of forcing green, then dispatch
the review once after restoration. The extractor stays schema-compatible: it rejects any
result it cannot verify instead of guessing. Rolling the caller/tooling back does not
revert unrelated application changes.

### What stays external

GitHub/OpenRouter outages; runner hard termination (an abandoned `pending` must be
treated as unreviewed); artifact expiry (default retention); and the fact that on GitHub
Free + private repositories status checks cannot be *required*, so `llm-review` remains
advisory at the platform level even though it always reports red as `failure`.

### Legacy workflow

`.github/workflows/llm-pr-review.yml`, `scripts/llm_review.py` and
`callers/pr-llm-review.yml` are untouched and still executed by consumers pinned to
`@main`. They remain in the repository until the installation inventory shows no
consumers, and `scripts/apply-callers.sh` (default branch only) is likewise kept for the
legacy rollout.
