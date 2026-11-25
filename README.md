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

- **[Automerge](.github/workflows/automerge.yml)** squash-merges a PR labelled `automerge` once every required check passes. It never bypasses branch protection.
- **[Governance separation](.github/workflows/governance.yml)** fails a PR that changes governance files (repo rules, CI, agent config; see [`governance.json`](governance.json)) together with anything else. A repo can add its own paths in `.github/governance.json`.

Repos opt in with a short caller workflow that references these with `uses: AllHailSeizure/.github/.github/workflows/<name>.yml@master`.
