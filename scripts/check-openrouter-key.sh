#!/usr/bin/env bash
# Verify an OpenRouter API key before storing it as a GitHub secret.
#
#   ./scripts/check-openrouter-key.sh
#
# Reads the key from $OPENROUTER_API_KEY, or prompts for it (hidden). Prints the
# captured length first, so an empty/failed paste is obvious (a valid OpenRouter
# key looks like "sk-or-v1-" + 64 hex chars).

set -euo pipefail

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  printf 'Pegá la key (no se muestra) y Enter: '
  read -rs OPENROUTER_API_KEY
  echo
fi

# Strip stray whitespace / carriage returns that clipboard pastes introduce.
OPENROUTER_API_KEY="$(printf '%s' "$OPENROUTER_API_KEY" \
  | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

echo "longitud capturada: ${#OPENROUTER_API_KEY}"
if [ -z "$OPENROUTER_API_KEY" ]; then
  echo "No se capturó nada: el pegado falló. Probá de nuevo." >&2
  exit 2
fi

body=$(curl -s https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY")
echo "$body"

case "$body" in
  *'"data"'*)
    echo "OK: la key es válida."
    ;;
  *)
    echo "La key fue rechazada por OpenRouter." >&2
    echo "Generá una nueva en https://openrouter.ai/keys" >&2
    exit 1
    ;;
esac
