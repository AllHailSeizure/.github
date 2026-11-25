# AllHailSeizure defaults

A public [`.github`](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file) repository. GitHub copies the community health files here into any AllHailSeizure repository that does not already have its own copy.

It has to stay public for that inheritance to work. Default branch is `master` (lowercase).

## What GitHub applies automatically

These files inherit into a repo that does not already define them:

- [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)
- [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) (bug and feature forms; blank issues are still allowed)
- [`CONTRIBUTING.md`](CONTRIBUTING.md)
- [`SECURITY.md`](SECURITY.md)
- [`SUPPORT.md`](SUPPORT.md)

A file already in a repo always wins. Today that means:

- **hotel-kline-game** keeps its own pull request template and `CONTRIBUTING.md`.
- **synapse** keeps its own pull request template. (It still lists type `fix` and priorities `p0`–`p3`. Those should be updated to `bug` and `P1`/`P2`/`P3` in that repo when you are ready; this repository will not edit it.)

Issue forms, `SECURITY.md`, and `SUPPORT.md` will apply even on those two repos, because they do not have their own copies.

## What does not inherit

GitHub will not copy these from this repository, no matter how they are named:

- **Labels.** Personal accounts have no default-label setting. [`labels.yml`](labels.yml) is the scheme; [the label-sync workflow](.github/workflows/label-sync.yml) pushes it to each owned repo when you run it.
- **CI.** Each repo keeps its own test workflow files. The existing `ci.yml` files are different stacks (Godot, Devvit/Node, Python, mixed Node+Python), so there is no shared test workflow. Automerge and the governance-separation check are reusable workflows here, but a repo only runs them if you add a short caller (see below).
- **LICENSE** and **README.** Every repo needs its own.

Forks (Nestor, willow-2.0) are not in the sync list.

## Labels

The shared set is eight type labels (`bug`, `feat`, `docs`, `refactor`, `tooling`, `test`, `assets`, `question`), three priorities (`P1`, `P2`, `P3`), and `automerge` (`#87BF5E`). Type and priority names match hotel-kline-game, whose CI depends on those spellings.

There are no status labels. Close a duplicate or won't-fix issue with GitHub's close reasons, and write `blocked by #N` in the issue if work is waiting on something else.

A few repos have extras, noted in `labels.yml`: `area:formality` on register-study, and `governance` on hotel-kline-game. Those extras are applied only to those repos. hotel-kline-game's must-keep labels (including `automerge`) are never deleted, and its existing type/priority colours are left alone.

### Running the sync

The workflow is [manual only](https://github.com/AllHailSeizure/.github/actions/workflows/label-sync.yml) (`workflow_dispatch`), and `dry_run` defaults to **true**. Nothing is applied until you uncheck that. Deletes are a separate checkbox and stay off unless you turn them on.

It needs a repository secret named `LABEL_SYNC_TOKEN`. `GITHUB_TOKEN` cannot write labels on other repos. Create a fine-grained personal access token owned by AllHailSeizure, limited to the twelve owned repos listed in `labels.yml`, with **Issues: Read and write**, and store it as that secret. Do not run the sync until the token is in place and you have reviewed a dry-run.

You can also run the script locally, still in dry-run by default:

```bash
pip install pyyaml
python3 tools/sync_labels.py --dry-run
```

Renames use GitHub's rename API, so existing issues and PRs keep the label.


## Automerge

A reusable workflow squash-merges a pull request when it has the `automerge` label and every required status check is green. Otherwise it is a no-op. It never bypasses branch protection or rulesets (`gh pr merge` is called without `--admin`). It also skips (does not merge) a pull request that mixes governance and non-governance paths, using the same classifier and base-branch add-on as the governance check below.

**Do not opt a repo in unless it has CI and required status checks on `master`** (a ruleset or classic branch protection). If the gate cannot see at least one required check, it will not merge. A `ci.yml` alone is not enough.

hotel-kline-game already has its own automerge workflow, with extra game-specific gates. Leave that in place; this repository does not add a caller there, or in any other repo, until you choose to.

After this pull request has merged, a repo that is safe to opt in can add `.github/workflows/automerge.yml` like this. Pin `@master`, or a SHA if you want it frozen to a known revision. Pass the check names your ruleset or branch protection already requires:

```yaml
name: Automerge

on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
  pull_request_target:
    types: [labeled, ready_for_review]

permissions:
  contents: write
  pull-requests: write
  actions: read
  checks: read

concurrency:
  group: automerge-${{ github.event.pull_request.number || github.event.workflow_run.head_sha }}
  cancel-in-progress: false

jobs:
  automerge:
    if: >-
      (
        github.event_name == 'pull_request_target' &&
        github.event.pull_request.head.repo.full_name == github.repository
      ) || (
        github.event_name == 'workflow_run' &&
        github.event.workflow_run.event == 'pull_request' &&
        github.event.workflow_run.conclusion == 'success' &&
        github.event.workflow_run.head_repository.full_name == github.repository
      )
    uses: AllHailSeizure/.github/.github/workflows/automerge.yml@master
    with:
      event_name: ${{ github.event_name }}
      pull_number: ${{ github.event.pull_request.number || 0 }}
      head_sha: ${{ github.event.workflow_run.head_sha || '' }}
      required_checks: test, lint   # names from that repo's ruleset or branch protection
```

The job token (`GITHUB_TOKEN`) is enough. Do not pass a personal token that can bypass rulesets.


## Governance separation

A pull request that changes how the repo operates may not also change product or other files. Rule changes cannot ride along with code changes. Split them; there is no override.

The default governance paths are in [`governance.json`](governance.json): files such as `AGENTS.md`, `CONTRIBUTING.md`, `CLAUDE.md`, `SECURITY.md`, `CODEOWNERS`, and `.cursor/settings.json`, and prefixes `.github/`, `.githooks/`, `.cursor/rules/`, `.cursor/skills/`, `.codex/`, and `.synapse/`. A calling repo may extend that list with a `.github/governance.json` of the same shape (`files`, `prefixes`, `globs`). The add-on is read from the pull request's **base** branch, never from the PR head, so a PR cannot redefine governance to slip through.

This `.github` repository is all about repo defaults, so the split is moot here and this repo does not run the check against itself. No callers are added to other repos in this pull request. hotel-kline-game already has its own check, with extra game-specific paths; leave that in place until you choose to switch it (it would then list those extra paths in its add-on file).

After this pull request has merged, a repo can opt in by adding `.github/workflows/governance.yml` like this. Pin `@master`, or a SHA if you want it frozen to a known revision:

```yaml
name: Governance

on:
  pull_request:

permissions:
  contents: read
  pull-requests: read

jobs:
  governance:
    uses: AllHailSeizure/.github/.github/workflows/governance.yml@master
    with:
      pull_number: ${{ github.event.pull_request.number }}
```

The reusable workflow checks out this defaults repo at `github.workflow_sha` and never executes the pull request's code. The job token (`GITHUB_TOKEN`) is enough to read the PR file list and the base-branch add-on.

## Changing something

Edit the file here and open a pull request against `master`. After it merges, GitHub starts using the new templates and docs on repos that do not override them. Label changes take effect only when you run the sync.
