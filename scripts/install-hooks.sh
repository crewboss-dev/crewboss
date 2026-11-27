#!/bin/sh
# Install the Crewboss git hooks into THIS checkout (worktrees share the repo's hooks). Run once after cloning:
#   scripts/install-hooks.sh
# It points core.hooksPath at scripts/hooks, so the hooks are versioned with the code and nothing is copied into .git.
set -e
root="$(git rev-parse --show-toplevel)"
chmod +x "$root/scripts/hooks/"* "$root/scripts/commit_guard.py" "$root/scripts/push_guard.py"
git -C "$root" config core.hooksPath scripts/hooks
echo "hooks installed: core.hooksPath = scripts/hooks (pre-commit runs scripts/commit_guard.py, pre-push runs scripts/push_guard.py)"
echo "put your instance-only values, one per line, in \${CREWBOSS_HOME:-~/.crewboss}/denylist.txt"
