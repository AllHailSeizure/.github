# AllHailSeizure defaults

Shared defaults for AllHailSeizure repositories. GitHub uses this public `.github` repo to fill in community health files for any repo that doesn't have its own copy, and it also holds the reusable workflows and label scheme that the other repos opt into.

## Community health files

- [Pull request template](.github/PULL_REQUEST_TEMPLATE.md)
- [Issue forms](.github/ISSUE_TEMPLATE/) for bugs and feature requests
- [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), [`SUPPORT.md`](SUPPORT.md)

A file in an individual repo always takes precedence over the one here.

## Labels

[`labels.yml`](labels.yml) defines the account-wide label scheme: eight type labels, three priorities (`P1`, `P2`, `P3`) and `automerge`, plus a few repo-specific extras. Labels don't inherit on GitHub, so the [label-sync workflow](.github/workflows/label-sync.yml) discovers owned repos and pushes the scheme to each. A nightly run applies updates without deletes; manual runs default to a dry run.

## Reusable workflows

- **[Automerge](.github/workflows/automerge.yml)** squash-merges a PR labelled `automerge` once every required check passes. It never bypasses branch protection. Callers must grant `contents: write`, `pull-requests: write`, `issues: write`, and `actions: write` (plus `checks: read`); without `issues: write`, GitHub silently skips auto-closing linked issues when the workflow performs the merge. When the base branch requires up-to-date branches, a merge-ready PR that has fallen behind is brought up to date with the update-branch API and the caller's CI workflow (`ci_workflow`, default `ci.yml`, which needs a `workflow_dispatch` trigger) is dispatched on the branch, because a push made by a workflow does not start pull-request runs. The gate runs again when that CI finishes and merges once the required checks are green on the new head. After a merge it updates the other labelled PRs the merge left behind. A conflicting PR is left alone with a comment.
- **[Governance separation](.github/workflows/governance.yml)** fails a PR that changes governance files (repo rules, CI, agent config; see [`governance.json`](governance.json)) together with anything else. A repo can add its own paths in `.github/governance.json`.

Repos opt in with a short caller workflow that references these with `uses: AllHailSeizure/.github/.github/workflows/<name>.yml@master`.
