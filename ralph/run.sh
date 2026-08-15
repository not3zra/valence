#!/usr/bin/env bash
set -euo pipefail

MAX_ITERS="${1:-20}"
PROMPT_FILE="ralph/prompt.md"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# ---------------------------------------------------------------------------
# frontier_issues: prints the numbers of open, ready-for-agent issues whose
# blockers (parsed from the "## Blocked by" section of "- #N (title)" bullets,
# as written by /to-tickets) are ALL closed. An issue with no such section is
# unblocked. This is what /to-tickets' blocking-edge contract actually
# requires the runner to respect.
# ---------------------------------------------------------------------------
frontier_issues() {
  local candidates
  candidates="$(gh issue list \
    --state open \
    --label ready-for-agent \
    --limit 100 \
    --json number,title \
    --jq '.[] | select(.title | test("^[0-9]+ — ")) | .number')"

  while IFS= read -r n; do
    [[ -z "$n" ]] && continue
    local body blockers all_closed=true
    body="$(gh issue view "$n" --json body --jq '.body // ""')"
    blockers="$(printf '%s\n' "$body" \
      | sed -n '/^#\+[[:space:]]*Blocked by/,/^#\+[[:space:]]*[A-Z]/p' \
      | grep -oE '#[0-9]+' \
      | tr -d '#' || true)"

    if [[ -n "$blockers" ]]; then
      while IFS= read -r b; do
        [[ -z "$b" ]] && continue
        state="$(gh issue view "$b" --json state --jq .state 2>/dev/null || echo UNKNOWN)"
        if [[ "$state" != "CLOSED" ]]; then
          all_closed=false
          break
        fi
      done <<< "$blockers"
    fi

    [[ "$all_closed" == true ]] && echo "$n"
  done <<< "$candidates"
}

# ---------------------------------------------------------------------------
# run_security_audit_if_due: invokes /security-audit once the loop has run
# out of frontier tickets, unless THREAT_MODEL.md already shows the current
# HEAD as audited (so re-running the loop with nothing new to do doesn't
# re-spend six sub-agent calls on an unchanged tree).
# ---------------------------------------------------------------------------
run_security_audit_if_due() {
  local last_audited="" head_sha
  head_sha="$(git rev-parse HEAD)"

  if [[ -f THREAT_MODEL.md ]]; then
    last_audited="$(grep -m1 '^audited-through:' THREAT_MODEL.md | awk '{print $2}' || true)"
  fi

  if [[ -n "$last_audited" && "$last_audited" == "$head_sha" ]]; then
    echo "=== Security audit already covers HEAD ($head_sha) -- skipping. ==="
    return 0
  fi

  echo "=== Feature run complete -- running /security-audit (fixed point: ${last_audited:-repo/branch root}) ==="
  local audit_prompt
  audit_prompt="$(cat <<EOF
Run /security-audit.

Fixed point: ${last_audited:-none recorded -- use the branch's root commit for a feature audit, or ask which scope if that's ambiguous}
Current HEAD: $head_sha

Update THREAT_MODEL.md's audited-through marker to $head_sha when done.
Route any new or carried-over hard finding to the issue tracker per the
skill's process -- label security + needs-human-review, not ready-for-agent.
EOF
)"
  opencode run --model opencode/deepseek-v4-flash-free "$audit_prompt" | tee "$TMPDIR/audit_output.txt"
}

for ((i=1; i<=MAX_ITERS; i++)); do
  echo "=== Ralph iteration $i ==="

  commits_file="$TMPDIR/commits.txt"
  issues_file="$TMPDIR/issues.txt"
  prs_file="$TMPDIR/prs.txt"
  output_file="$TMPDIR/output.txt"

  git log -n 8 --format="%H%n%ad%n%B%n---" --date=short > "$commits_file" 2>/dev/null \
    || echo "No commits found" > "$commits_file"

  frontier="$(frontier_issues)"

  if [[ -z "$frontier" ]]; then
    echo "Ralph complete: no unblocked AFK GitHub issues found."
    run_security_audit_if_due
    exit 0
  fi

  : > "$issues_file"
  while IFS= read -r issue_number; do
    [[ -z "$issue_number" ]] && continue
    {
      echo "===== ISSUE #$issue_number (frontier: all blockers closed) ====="
      gh issue view "$issue_number" \
        --comments \
        --json number,title,body,labels,comments
      echo
    } >> "$issues_file"
  done <<< "$frontier"

  gh pr list \
    --state open \
    --limit 50 \
    --json number,title,headRefName,author,isDraft,reviewDecision,labels \
    > "$prs_file" || true

  prompt="$(cat "$PROMPT_FILE")"

  full_prompt="$(
    cat <<EOF
Recent commits:
$(cat "$commits_file")

Unblocked AFK GitHub issues (frontier only -- every "Blocked by" issue is closed):
$(cat "$issues_file")

Open Pull Requests:
$(cat "$prs_file")

$prompt
EOF
  )"

  opencode run --model opencode/deepseek-v4-flash-free "$full_prompt" | tee "$output_file"

  if grep -q "<promise>NO MORE TASKS</promise>" "$output_file"; then
    echo "Ralph complete after $i iterations."
    run_security_audit_if_due
    exit 0
  fi
done

echo "Reached max iterations ($MAX_ITERS)."
exit 1
