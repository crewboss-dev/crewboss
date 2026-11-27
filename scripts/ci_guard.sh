#!/usr/bin/env bash
# The CI half of the leak guard, kept in a script so tests/test_push_guard.py can run exactly what the workflow runs.
#
#   scripts/ci_guard.sh range   prints key=value lines (range, base, exclude, tip_only, meta_only, log_opts) for the checked-out repo
#   scripts/ci_guard.sh run     runs scripts/commit_guard.py with the arguments those values call for
#
# `range` reads: EVENT (push or pull_request), PR_BASE, PR_HEAD, PUSH_BEFORE, PUSH_AFTER, DEFAULT_BRANCH and REF (the full ref being
# pushed, e.g. refs/heads/main or refs/tags/v1). `run` reads RANGE, BASE, EXCLUDE, TIP_ONLY, META_ONLY and REF_NAME.
#
# What the range means:
#   * a pull request, or a push that moves a ref: the commits it adds, built on the old tip / the PR base;
#   * a NEW ref (tag, branch) that shares history with the default branch: the commits since the merge-base. A ref at an existing
#     commit adds nothing, so only its name and tag text are checked (META_ONLY);
#   * an ORPHAN branch (no merge-base): its whole history, compared with the default branch's tip: a line that already exists
#     there is not "added", everything else is, and the baseline and binary allowlist are the default branch's, never its own;
#   * the FIRST push of the default branch to a repo with no other refs: the whole history; a single commit is judged by its tree
#     (TIP_ONLY), which is the public release cut.
set -u

zero() { echo "$1" | grep -qE '^0*$'; }
# "A --not B C": the commits reachable from A and from none of B, C
mkrange() { local tip="$1"; shift; if [ $# -gt 0 ]; then echo "$tip --not $*"; else echo "$tip"; fi; }
# the commit ids of every ref on the server except the one being pushed
other_refs() {
  local self=""
  case "${REF:-}" in
    refs/heads/*) self="refs/remotes/origin/${REF#refs/heads/}" ;;
    refs/tags/*) self="$REF" ;;
  esac
  git for-each-ref --format='%(refname) %(objectname)' refs/remotes/origin refs/tags \
    | awk -v self="$self" '$1 != self && $1 != "refs/remotes/origin/HEAD" { print $2 }' | sort -u | tr '\n' ' '
}
die() { echo "ci_guard: $*" >&2; exit 1; }

cmd_range() {
  local range="" base="" exclude="" tip_only="" meta_only=""
  # Every check below compares with history: a shallow clone cannot tell what is new, so it must never be trusted.
  if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
    die "this checkout is shallow (no full history): run actions/checkout with fetch-depth: 0, or ask a maintainer"
  fi
  # A previous tip that is not here (a force push, a re-cut) is fetched explicitly; if it cannot be, the job fails rather than guessing.
  if [ "${EVENT:-}" != "pull_request" ] && ! zero "${PUSH_BEFORE:-}" && ! git cat-file -e "${PUSH_BEFORE}^{commit}" 2>/dev/null; then
    git fetch -q origin "$PUSH_BEFORE" 2>/dev/null || true
    git cat-file -e "${PUSH_BEFORE}^{commit}" 2>/dev/null \
      || die "cannot find the previous tip ${PUSH_BEFORE}; run with full history or ask a maintainer"
  fi
  if [ "${EVENT:-}" = "pull_request" ]; then
    range="${PR_BASE}..${PR_HEAD}"; base="$PR_BASE"
  else
    # CI never subtracts other branches or tags: the refs a checkout shows were read AFTER the push, so refs pushed together (main and a
    # tag on the same commit) or racing each other would each subtract the other and nobody would scan the commit.
    #   * an updated ref: exactly PUSH_BEFORE..PUSH_AFTER, which cannot race;
    #   * a new ref: only what the default branch does not have (its own commits get their own before..after run).
    local def mb others
    if ! zero "${PUSH_BEFORE:-}"; then
      range="${PUSH_BEFORE}..${PUSH_AFTER}"; base="$PUSH_BEFORE"
    elif [ "${REF:-}" = "refs/heads/${DEFAULT_BRANCH}" ]; then
      # the default branch itself, newly created (the previous tip is all zeros): its whole history is new
      range="$PUSH_AFTER"
      others="$(other_refs)"      # only to recognise the first push of a repo: no other branch or tag exists yet
      if [ -z "$others" ] && [ "$(git rev-list --count "$PUSH_AFTER")" = "1" ]; then tip_only=1; fi
    else
      range="$PUSH_AFTER"
      if def="$(git rev-parse --verify -q "refs/remotes/origin/${DEFAULT_BRANCH}")"; then
        range="$PUSH_AFTER --not $def"
        if [ -z "$(git rev-list -n1 $range)" ]; then
          range=""; meta_only=1                                     # a ref at commits the default branch already has adds no content
        else
          mb="$(git merge-base "$def" "$PUSH_AFTER" 2>/dev/null || true)"
          if [ -z "$mb" ]; then base="$def"; exclude="$def"         # an orphan: judged against the default branch's tree
          else base="$mb"; fi
        fi
      fi
    fi
  fi
  echo "range=${range}"
  echo "base=${base}"
  echo "exclude=${exclude}"
  echo "tip_only=${tip_only}"
  echo "meta_only=${meta_only}"
  echo "log_opts=${range:-${PUSH_AFTER:-HEAD}}"
}

cmd_run() {
  local args=(--strict --no-gitleaks "--meta-ref=${REF_NAME:-}")
  # every value is joined to its option with "=": a branch named --list or -x is then a value, never a flag
  if [ -n "${RANGE:-}" ]; then args+=("--added-in=$RANGE" "--meta-range=$RANGE" "--files-in=$RANGE"); fi
  if [ -n "${BASE:-}" ]; then args+=("--baseline-base=$BASE"); fi
  if [ -n "${EXCLUDE:-}" ]; then args+=("--added-excluding=$EXCLUDE"); fi
  if [ -n "${TIP_ONLY:-}" ]; then args+=(--tip-only); fi
  python3 "$(dirname "$0")/commit_guard.py" "${args[@]}"
}

case "${1:-}" in
  range) cmd_range ;;
  run) cmd_run ;;
  *) echo "usage: ci_guard.sh range|run" >&2; exit 2 ;;
esac
