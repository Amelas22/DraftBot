#!/bin/bash
# Runs the orphaned-function check against HEAD, using the project venv.
# Kept out of .git/hooks so it is version-controlled and reviewable; the hook
# there is a one-line shim pointing at this.
set -e
root="$(git rev-parse --show-toplevel)"
py="$(cd "$root" && pipenv --py 2>/dev/null || echo python3)"
exec "$py" "$root/scripts/check_orphaned_functions.py" --base HEAD
