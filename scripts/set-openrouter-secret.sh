#!/usr/bin/env bash
# Set the OPENROUTER_API_KEY repository secret in every organization repository.
#
# Organization secrets cannot be used by private repositories on GitHub Free,
# so each repository needs its own copy.
#
# The key is read from $OPENROUTER_API_KEY, or prompted for (hidden input) so it
# never ends up in your shell history:
#
#   scripts/set-openrouter-secret.sh

set -euo pipefail

ORG="fantastics4"
EXCLUDE=".github"

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  read -r -s -p "OpenRouter API key: " OPENROUTER_API_KEY
  echo
fi

# Strip stray whitespace / carriage returns that clipboard pastes introduce.
OPENROUTER_API_KEY="$(printf '%s' "$OPENROUTER_API_KEY" \
  | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
[ -n "$OPENROUTER_API_KEY" ] || { echo "empty key" >&2; exit 1; }

# Validate the key first: better to fail here than to store a broken secret in
# every repository.
code=$(curl -s -o /dev/null -w '%{http_code}' \
  https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY")
if [ "$code" != "200" ]; then
  echo "OpenRouter rejected the key (HTTP $code)." >&2
  echo "Create or verify one at https://openrouter.ai/keys" >&2
  exit 1
fi

for repo in $(gh repo list "$ORG" --limit 200 --json name --jq '.[].name'); do
  case " $EXCLUDE " in *" $repo "*) echo "skip   $repo"; continue ;; esac
  printf '%s' "$OPENROUTER_API_KEY" \
    | gh secret set OPENROUTER_API_KEY -R "$ORG/$repo" >/dev/null
  echo "secret $repo"
done
echo "done."
