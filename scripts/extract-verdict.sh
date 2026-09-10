#!/usr/bin/env bash
# Extract the machine-readable verdict JSON from a PR's LLM review comment.
#
#   scripts/extract-verdict.sh <owner/repo> <pr-number>
#
# Prints the `llm-review-verdict` JSON object (the fixer LLM's input) to stdout.
# Exits 1 with a message if the PR has no LLM review comment.

set -euo pipefail

REPO="${1:?usage: extract-verdict.sh <owner/repo> <pr-number>}"
PR="${2:?usage: extract-verdict.sh <owner/repo> <pr-number>}"

body=$(gh api --paginate "repos/$REPO/issues/$PR/comments" \
  --jq '.[] | select(.body | contains("<!-- llm-pr-review -->")) | .body' || true)

if [ -z "$body" ]; then
  echo "no LLM review comment on $REPO#$PR" >&2
  exit 1
fi

printf '%s\n' "$body" \
  | awk '/^```json llm-review-verdict$/{f=1;next} f && /^```$/{exit} f'
