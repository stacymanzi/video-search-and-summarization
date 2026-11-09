#!/usr/bin/env bash
# Fails when git ignores a file under a shipped directory. Those files exist on
# the author's machine, so every local test passes, but a clone (CI, the
# production image, a devbox) never receives them. Run from inside the git
# checkout, before committing. Caches are the only ignored paths allowed.
set -euo pipefail
repo="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not inside a git checkout; run this from the clone, not the working tree" >&2; exit 2; }
cd "$repo"
shipped=(task1/assessment task1/lab task1/tests task1/deploy task1/dind task1/scripts)
present=()
for d in "${shipped[@]}"; do [ -d "$d" ] && present+=("$d"); done
hidden="$(git ls-files --others --ignored --exclude-standard --directory -- "${present[@]}" \
  | grep -v -E '(^|/)(__pycache__|\.pytest_cache|\.ipynb_checkpoints)(/|$)' || true)"
if [ -n "$hidden" ]; then
  printf 'ignored but shipped (fix .gitignore or move the file):\n%s\n' "$hidden" >&2
  exit 1
fi
echo "no shipped path is git-ignored"
