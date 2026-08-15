# ISSUES

The frontier issues passed to you above are the source of truth. They are
already filtered to open, `ready-for-agent`, AFK issues whose blockers
(per the "Blocked by" line /to-tickets writes) are all closed. Do not
pick a task outside this list, and do not second-guess the blocking
filter -- if you believe a listed issue is actually still blocked, say so
and stop rather than working it.

You have also been passed the last few commits and the open PRs. Review
both before picking anything, so you don't duplicate work already in
flight on another branch.

If the frontier list is empty, or every frontier issue already has an
open PR and none are actionable per TASK SELECTION, output exactly:

<promise>NO MORE TASKS</promise>

# TASK SELECTION

Pick the next task from the frontier only. Prioritize in this order:

1. Critical bugfixes
2. Development infrastructure needed for feature work (tests, types, dev scripts)
3. Tracer-bullet vertical slices for new features
4. Polish and quick wins
5. Refactors

Pick only a single task. If an open PR already exists for a frontier
issue, skip it -- don't start a second one.

# EXPLORATION

Explore the repository before making changes. Read the relevant code
paths, tests, types, `CONTEXT.md`/ADRs, and the full issue thread first.

# BRANCHING

Create (or switch to, if one already exists for this issue) a branch
named `issue-<number>-<short-slug>`. Never commit directly to the
repo's default branch.

# IMPLEMENTATION

Use /implement to build the selected task. It already drives /tdd at
pre-agreed seams, runs typechecking and the relevant test files as it
goes, runs the full suite once at the end, and runs /code-review before
committing -- do not manually re-derive those steps, and do not skip
straight to writing code without it.

If the task is too large for one slice, complete the smallest coherent
vertical slice and update the GitHub issue with what remains -- do not
partially start a second task in the same iteration.

# SECURITY REVIEW (conditional)

After /implement's /code-review has run, decide whether the diff
touches a security-sensitive surface:

- authentication or authorization logic
- secrets, tokens, or environment/config handling
- input validation or sanitization of user-supplied data
- SQL, shell, or template construction from external input
- deserialization of untrusted data
- file or network I/O, especially with externally-supplied paths/URLs
- cryptography or hashing
- dependency or lockfile changes

If yes: run /security-review (skills/engineering/security-review/SKILL.md)
as an additional gate before opening the PR.

If no: skip it, but say so explicitly in your output -- the loop log
should show this was a deliberate judgment call, not an omission.

# VALIDATION

Confirm typechecking and the full test suite are green on the branch
before opening a PR. This should already be true coming out of
/implement -- re-run only if you made changes after it (e.g. to
address a review finding).

# PULL REQUEST

Open a PR from the branch (do not push to the default branch):

- Title: the issue title.
- Body: a short summary of what changed, how it was validated, and
  `Closes #<issue-number>`.

Merge gating:

- If /code-review (and /security-review, when it ran) reported **zero
  hard violations**, mark the PR ready for review and enable auto-merge:
  `gh pr merge --auto --squash`.
- Otherwise, leave the PR as a draft, apply the `needs-human-review`
  label, and post the flagged findings as a PR comment. Do not merge.
  Judgment-call findings (as opposed to hard violations) don't block
  merge on their own -- note them in the PR body for a human to weigh.

# ISSUE MANAGEMENT

If a PR was opened for the task:

- Comment on the issue linking the PR and summarizing what was done and
  how it was validated, using the Conventional Comments standard.
- Do NOT close the issue yourself -- it closes automatically when the
  PR merges via `Closes #n`.

If the task is not complete, or you decided not to open a PR:

- Add a short progress comment to the issue: what was done, what
  remains, any blockers. Use the Conventional Comments standard.
- Leave the `ready-for-agent` label on unless the issue is genuinely
  blocked on something outside the agent's control (e.g. a missing
  credential, an ambiguous spec) -- in that case, swap it for a
  `blocked` label and say why, so the loop stops re-selecting it.

# GITHUB CLI

Examples:

gh issue view <number> --comments
gh issue comment <number> --body "..."
gh pr create --title "..." --body "..." --head <branch>
gh pr merge --auto --squash <number>
gh issue edit <number> --add-label blocked --remove-label ready-for-agent

# FINAL RULES

ONLY WORK ON A SINGLE TASK. Do NOT begin another task in the same
iteration. If blocked, say clearly what blocked progress.
