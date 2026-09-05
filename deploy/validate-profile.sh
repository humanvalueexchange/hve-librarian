#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
metadata="$repo_root/PROFILE.yaml"
identity="$repo_root/$(awk -F': ' '/^deployable_identity:/{print $2; exit}' "$metadata")"
expected="${1:-}"

test -s "$metadata"
test -s "$identity"

profile_id="$(awk -F': ' '/^profile_id:/{print $2; exit}' "$metadata")"
test -n "$profile_id"
if [[ -n "$expected" && "$profile_id" != "$expected" ]]; then
  printf 'profile_id mismatch: expected %s, found %s\n' "$expected" "$profile_id" >&2
  exit 1
fi

while IFS= read -r marker; do
  [[ -z "$marker" ]] && continue
  if grep -Fq "$marker" "$identity"; then
    printf 'forbidden identity marker in %s: %s\n' "$identity" "$marker" >&2
    exit 1
  fi
done < <(awk '/^forbidden_identity_markers:/{in_list=1; next} in_list && /^  - /{sub(/^  - /, ""); print; next} in_list{exit}' "$metadata")

printf 'validated profile %s (%s)\n' "$profile_id" "$identity"
