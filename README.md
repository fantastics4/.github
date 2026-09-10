# fantastics4/.github

Organization-wide reusable workflows and tooling.

## `llm-pr-review` — automatic LLM code review on pull requests

A reusable workflow (`workflow_call`) that reviews a PR diff with an LLM through
[OpenRouter](https://openrouter.ai) and leaves a **single sticky comment** with a
**green/red verdict**:

- ✅ **green** — nothing blocking, the code can go up.
- ❌ **red** — blocking issues, each with file, exact lines and the exact fix,
  plus the unit / integration / e2e tests to add (aggressive TDD) and the
  verifications required to turn it green.

The comment embeds a machine-readable block that a **fixer LLM** can consume:

    ```json llm-review-verdict
    { "verdict": "red", "blocking_issues": [...], "tests_to_add": [...], ... }
    ```

### How it is used

Each repository adds a thin caller workflow that points here:

```yaml
jobs:
  review:
    uses: fantastics4/.github/.github/workflows/llm-pr-review.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
    secrets:
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
```

See `develo-web/.github/workflows/pr-llm-review.yml` for a full example
(`pull_request` + `workflow_run` after CI + `workflow_dispatch`).

### PR description: the objective contract

The review judges the diff **against the objective the PR declares**, so the PR
body should start with this block (the agent rules `pr-objective` /
`/skill:pr-objective` write it automatically):

````markdown
<!-- llm-pr-objective -->
## Objective
The problem to solve: for whom, and why it matters now.

## Solution
How this PR solves it, plus the key decisions and tradeoffs.

## Acceptance criteria (optional)
- [ ] a verifiable outcome
````

`scripts/llm_review.py` extracts everything between the
`<!-- llm-pr-objective -->` marker and the next comment marker (`<!--`) or
horizontal rule, passes it to the model as the review contract, and instructs it
to flag as **blocking** anything the diff does not deliver, changes silently, or
omits relative to a stated acceptance criterion. Without the block the review
still works on the diff alone, but it notes the absence in the summary.

### Required secret

`OPENROUTER_API_KEY` must exist as a **repository secret in every repository**
that uses the caller. Organization secrets are **not** an option here: on
GitHub Free, organization secrets cannot be used by private repositories
(only public ones), and all `fantastics4` repositories are private.

Set it per repository with `gh`:

```bash
gh secret set OPENROUTER_API_KEY -R fantastics4/develo-web
# repeat for every repository that has the caller
```

or via the UI: *repository → Settings → Secrets and variables → Actions →
New repository secret*.

To set it in every repository at once (key is prompted, never stored in your
shell history):

```bash
scripts/set-openrouter-secret.sh
```

### Inputs

| Input | Required | Default | Purpose |
|---|---|---|---|
| `pr_number` | yes | — | Pull request to review |
| `model` | no | `deepseek/deepseek-v4.1-flash` | Any OpenRouter model id |
| `max_diff_chars` | no | `60000` | Diff size budget (cost control) |
| `test_conventions` | no | `""` | Repo test layout, so tests are named realistically |
| `gate` | no | `false` | When `true`, the job fails if the verdict is red |

### Rolling it out to every repository

`callers/pr-llm-review.yml` is the caller template and
`scripts/apply-callers.sh` installs it everywhere:

```bash
scripts/apply-callers.sh --dry-run   # preview
scripts/apply-callers.sh             # opens one PR per repository
scripts/apply-callers.sh --direct    # commit straight to default branches
```

It skips `.github` and `develo-web` (the latter already has a customized
caller). Remember the `OPENROUTER_API_KEY` repository secret per repository
first, otherwise the first run on a PR will fail.

### What it does on the PR

1. Upserts (creates or updates) one comment marked with `<!-- llm-pr-review -->`.
2. Adds the label `llm-review:green` or `llm-review:red`.
3. Posts a commit status `llm-review` on the PR head SHA.

> **GitHub Free + private repo:** you cannot require status checks (branch
> protection / rulesets are Pro/Team/Enterprise for private repos). The comment,
> the label and the commit status are still published, but the *hard* merge gate
> is only available on public repos (Free) or with a paid plan. Until then the
> verdict is consumed by the fixer LLM / the reviewer, not enforced by GitHub.

### Connecting a fixer LLM

The review output is designed to be consumed by a second LLM that applies the
fixes and pushes, re-triggering the review until it is green. See
**[docs/llm-fixer.md](docs/llm-fixer.md)** for the contract, the loop, the
guardrails and how to automate it. To read a PR's verdict:

```bash
scripts/extract-verdict.sh fantastics4/develo-web 12 | python3 -m json.tool
```

The reusable workflow also exposes a `verdict` output (`green` / `red`) so a
caller can chain a fixer job with `needs.review.outputs.verdict`.

### Making this repo usable

The reusable workflow checks out this repository (`fantastics4/.github`). Keep it
**public** (it contains no secrets) so callers can fetch it with their default
`GITHUB_TOKEN`. If it must be private, enable
*Settings → Actions → General → Access → "Accessible from repositories in the
fantastics4 organization"* and pass a `token:` to the checkout step with a PAT
that has read access.
