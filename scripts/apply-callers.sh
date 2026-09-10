#!/usr/bin/env bash
# Roll out the LLM PR review caller to every repository in the organization.
#
# Default: opens one pull request per repository (safe, no direct pushes to
# default branches, so existing deploy pipelines are not triggered).
#   --direct   commit straight to each default branch
#   --dry-run  only print what would happen
#
# Prerequisite: the OPENROUTER_API_KEY repository secret must exist in each
# repository (organization secrets cannot be used by private repositories on
# GitHub Free):
#   gh secret set OPENROUTER_API_KEY -R fantastics4/<repo>

set -euo pipefail

ORG="fantastics4"
BRANCH="chore/add-llm-pr-review"
FILE=".github/workflows/pr-llm-review.yml"
SOURCE="$(cd "$(dirname "$0")/.." && pwd)/callers/pr-llm-review.yml"
EXCLUDE=".github develo-web"

MODE="pr"
case "${1:-}" in
  --direct) MODE="direct" ;;
  --dry-run) MODE="dry-run" ;;
  "") ;;
  *) echo "usage: $0 [--dry-run|--direct]" >&2; exit 2 ;;
esac

[ -f "$SOURCE" ] || { echo "missing $SOURCE" >&2; exit 1; }
CONTENT="$(base64 -w0 "$SOURCE")"

for repo in $(gh repo list "$ORG" --limit 200 --json name --jq '.[].name'); do
  case " $EXCLUDE " in *" $repo "*) echo "skip  $repo"; continue ;; esac
  base=$(gh api "repos/$ORG/$repo" --jq .default_branch)
  target="$BRANCH"
  [ "$MODE" = "direct" ] && target="$base"

  branch_exists="no"
  if gh api "repos/$ORG/$repo/git/ref/heads/$target" >/dev/null 2>&1; then
    branch_exists="yes"
  fi

  sha=""
  if raw=$(gh api "repos/$ORG/$repo/contents/$FILE?ref=$target" --jq '.sha' 2>/dev/null); then
    sha="$raw"
  fi
  if [ "$MODE" = "dry-run" ]; then
    echo "plan  $repo (base=$base target=$target branch=$branch_exists file=${sha:+yes})"
    continue
  fi

  if [ "$branch_exists" = "no" ]; then
    base_sha=$(gh api "repos/$ORG/$repo/git/ref/heads/$base" --jq '.object.sha')
    gh api -X POST "repos/$ORG/$repo/git/refs" \
      -f ref="refs/heads/$target" -f sha="$base_sha" >/dev/null
    echo "branch $repo -> $target"
  fi

  args=(-X PUT "repos/$ORG/$repo/contents/$FILE"
        -f message="ci: add automatic LLM PR review"
        -f content="$CONTENT" -f branch="$target")
  [ -n "${sha:-}" ] && args+=(-f sha="$sha")
  gh api "${args[@]}" >/dev/null
  echo "file  $repo -> $target"

  if [ "$MODE" = "pr" ]; then
    url=$(gh api -X POST "repos/$ORG/$repo/pulls" \
      -f title="ci: add automatic LLM PR review" \
      -f head="$BRANCH" -f base="$base" \
      -f body="Adds the organization-wide LLM PR review. Requires the \`OPENROUTER_API_KEY\` repository secret." \
      --jq .html_url 2>/dev/null || echo "(PR already open)")
    echo "pr    $repo -> $url"
  fi
done
echo "done."
