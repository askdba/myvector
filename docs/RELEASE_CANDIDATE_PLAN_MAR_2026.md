# Release Candidate Plan (March 2026)

This plan defines the Release Candidate (RC) process for the March 2026
minor release.

## Release intent and scope rules

- Release type: minor version release.
- Scope baseline: include all merged changes since the last released tag.
- Exclusion rule: no Component PRs are allowed in this RC.
- Post-cut policy: only blocker bug fixes are allowed after RC1 cut.

## Working assumptions

- Date window: March 19 to March 31, 2026.
- Branch policy: release branch and tag are created from current mainline state.
- Quality bar: zero open blocker issues at sign-off.

## March 19-22: RC scope and checklist

### 1) Confirm what is in this RC

- Build a change inventory from the last release tag to `HEAD`.
- Classify each change as:
  - `must-ship` (already merged and required for this minor release),
  - `deferred` (safe to move to next release if high risk),
  - `excluded` (Component PRs by policy).
- Assign an owner for each `must-ship` area.

### 2) Freeze non-essential changes

- Start soft freeze on March 19:
  - no refactors unrelated to defects,
  - no opportunistic dependency churn,
  - no non-critical feature tweaks.
- Start hard freeze on March 20 EOD:
  - only release manager-approved exceptions can merge,
  - exception requires linked blocker bug and rollback plan.

### 3) RC acceptance checklist

Use this checklist for RC sign-off:

- Core flows
  - build from clean checkout,
  - install/load plugin path,
  - key vector create/query flow,
  - update/delete flow with expected behavior.
- Regression focus
  - modules touched since last release,
  - previously unstable paths and recent bugfix areas.
- Performance smoke
  - startup sanity,
  - representative query latency sanity,
  - memory growth sanity during repeated operations.
- Platform matrix
  - Linux x86_64 (release baseline),
  - Linux arm64 (if release artifact is produced),
  - macOS Apple Silicon for local compatibility checks.
- Browser matrix
  - not applicable unless a web UI/test harness is included in release scope.
- Release gates
  - CI green on release branch,
  - no open blocker bugs,
  - release notes draft complete,
  - documentation updates completed for user-facing changes,
  - upgrade/migration notes completed if behavior changed,
  - known issues section reviewed.

### 4) RC cut schedule

- RC1 cut: March 21 EOD.
- Validation window: March 22-24.
- RC2 cut: only if blockers remain, target March 25.
- Final go/no-go: March 26 after blocker review.
- Target public release: March 27-31 depending on blocker status.

## Operating rules during RC

- No Component PR merges during RC for this release line.
- Any approved post-cut fix must:
  - reference a blocker issue,
  - include focused test evidence,
  - include rollback/revert instructions.
- Keep release notes updated with every accepted RC fix.
- Keep documentation updated with every accepted RC fix that changes behavior.

## Deliverables by March 22 EOD

- Final include/exclude change list from last tag to `HEAD`.
- Freeze announcement and merge exception process.
- Completed RC acceptance checklist with owners.
- Published RC1/RC2 decision calendar.
- Release notes draft updated and reviewed.
- Documentation delta reviewed and merged.

## Release manager runbook

Use this runbook to generate the scope inventory and draft release notes.

### 1) Identify release range

```bash
# Last released tag (most recent reachable tag)
LAST_TAG="$(git describe --tags --abbrev=0)"
echo "Last tag: ${LAST_TAG}"
```

If your release train uses a specific previous tag, set it manually:

```bash
LAST_TAG="vX.Y.Z"
```

### 2) Build change inventory (last tag to HEAD)

```bash
# Commit list for review
git log --oneline "${LAST_TAG}..HEAD"

# PR-style subjects if merge commits are used
git log --merges --pretty=format:'%h %s' "${LAST_TAG}..HEAD"

# File-level scope
git diff --name-status "${LAST_TAG}..HEAD"
```

### 3) Enforce "no Component PRs" rule

```bash
# Detect component-related paths (adjust patterns if needed)
git diff --name-only "${LAST_TAG}..HEAD" | rg -i '(^|/)(component|component_src)(/|$)'
```

- If any matches are found, classify them as `excluded` for this RC.
- Track exceptions only by explicit release-manager approval.

### 4) Draft release notes from commits

```bash
mkdir -p release
git log --pretty=format:'- %s (%h)' "${LAST_TAG}..HEAD" > release/RELEASE_NOTES_DRAFT.md
```

Then edit `release/RELEASE_NOTES_DRAFT.md` into sections:

- Added
- Changed
- Fixed
- Known issues

### 5) Documentation update audit

```bash
# Review user-facing changes and touched docs
git diff --name-status "${LAST_TAG}..HEAD"
git diff --name-only "${LAST_TAG}..HEAD" | rg -i '^(docs/|README|CHANGELOG|release/)'
```

For each user-visible behavior or workflow change in scope:

- update an existing document under `docs/` or `README`,
- add a short note in release notes under Added/Changed/Fixed,
- include migration guidance if upgrade behavior changed.

### 6) Create RC branch and tag candidates

```bash
# Example branch name
git checkout -b "release/next-minor-rc1"

# After validation, create RC tag
git tag -a "vX.Y.Z-rc1" -m "Release candidate 1 for vX.Y.Z"
```

Use `rc2` only if blockers remain after RC1 validation.

