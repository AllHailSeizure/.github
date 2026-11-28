#!/usr/bin/env python3
"""Squash-merge a pull request when the automerge label and required checks are green.

Never bypasses branch protection or rulesets. An empty required-check list
never merges: a repo without required checks (no ruleset or classic branch
protection) must not opt in. A PR that mixes governance and non-governance
paths is skipped, using the same classifier and base-branch add-on as the
governance-separation check.

When the base branch requires branches to be up to date and an otherwise
merge-ready PR is behind it, the gate brings the branch up to date with the
update-branch API. GitHub starts the pull_request CI run for that push but holds
it as action_required because the push came from a workflow, so the gate
approves that run. (Starting CI with workflow_dispatch does not work: its check
runs are not counted for the pull request, and it does not start the caller's
workflow_run trigger.) The CI run's completion does not reliably start the
gate again, so the gate waits for the checks on the new head itself and then
merges. It never loosens a check: the required checks must be green on the new
head. After a merge it updates the next labelled PR the merge left behind, one
at a time, and carries it through the same way.

The script is self-contained. It does not import hotel-kline-game Tools/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, NamedTuple

from governance import AddonError, load_effective_config, list_pr_paths, mixes_governance

# Every listed label must be on the pull request. Override with AUTOMERGE_LABELS
# (comma-separated). An empty list never merges.
DEFAULT_REQUIRED_LABELS = "automerge"

# CI runs the gate reacts to (the pull_request run of a PR).
WORKFLOW_RUN_EVENTS = frozenset({"pull_request"})

# How long the gate waits for CI on an updated head (AUTOMERGE_WAIT_MINUTES
# overrides it), how often it looks, and how long one queue run may last.
CI_WAIT_SECONDS = 40 * 60
CI_POLL_SECONDS = 30
QUEUE_TOTAL_SECONDS = 140 * 60
MAX_QUEUE_STEPS = 60
# Seconds to wait for update-branch (asynchronous) to move the PR head.
UPDATE_WAIT_SECONDS = 90
UPDATE_POLL_SECONDS = 3

NOTICE_MARKER = "<!-- automerge-gate:{kind}:{sha} -->"


def required_labels(raw: str | None = None) -> frozenset[str]:
    if raw is None:
        raw = os.environ.get("AUTOMERGE_LABELS", DEFAULT_REQUIRED_LABELS)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def split_check_names(raw: str) -> list[str]:
    """Split a caller-supplied check list.

    Semicolons or newlines are treated as separators when present, so names
    that contain commas (for example "Tests (ubuntu-latest, Python 3.14)")
    stay intact. Otherwise commas split only at the top level, not inside
    parentheses.
    """
    if not raw or not raw.strip():
        return []
    if "\n" in raw or ";" in raw:
        chunks = raw.replace("\n", ";").split(";")
        return [part.strip() for part in chunks if part.strip()]
    names: list[str] = []
    buf: list[str] = []
    depth = 0
    for char in raw:
        if char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            buf.append(char)
        elif char == "," and depth == 0:
            name = "".join(buf).strip()
            if name:
                names.append(name)
            buf = []
        else:
            buf.append(char)
    name = "".join(buf).strip()
    if name:
        names.append(name)
    return names


def configured_check_names(raw: str | None = None) -> tuple[str, ...]:
    """Check names the caller passed in AUTOMERGE_REQUIRED_CHECKS."""
    if raw is None:
        raw = os.environ.get("AUTOMERGE_REQUIRED_CHECKS", "")
    return unique_names(split_check_names(raw))


def unique_names(names: list[str] | tuple[str, ...] | Any) -> tuple[str, ...]:
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def collect_required_checks(
    *, configured: tuple[str, ...] = (), discovered: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Union of GitHub-reported required checks and any caller-supplied names."""
    return unique_names(list(discovered) + list(configured))


def latest_check_runs_by_name(runs: list[object]) -> dict[str, dict]:
    """Keep the newest check run for each name (by started_at).

    Cancelled runs are superseded runs and are ignored, the same as skipped.
    """
    latest: dict[str, dict] = {}
    for item in runs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        if str(item.get("conclusion") or "") in {"skipped", "cancelled"}:
            continue
        previous = latest.get(name)
        if previous is None or str(item.get("started_at") or "") > str(
            previous.get("started_at") or ""
        ):
            latest[name] = item
    return latest


def required_names_from_protection(payload: object) -> tuple[str, ...]:
    """Classic branch-protection required_status_checks body."""
    if not isinstance(payload, dict):
        return ()
    names: list[str] = []
    for item in payload.get("checks") or []:
        if isinstance(item, dict) and item.get("context"):
            names.append(str(item["context"]))
    for item in payload.get("contexts") or []:
        if item:
            names.append(str(item))
    return unique_names(names)


def required_names_from_rules(payload: object) -> tuple[str, ...]:
    """Effective rules from GET /repos/.../rules/branches/{branch}."""
    if not isinstance(payload, list):
        return ()
    names: list[str] = []
    for rule in payload:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        for item in params.get("required_status_checks") or []:
            if isinstance(item, dict) and item.get("context"):
                names.append(str(item["context"]))
    return unique_names(names)


def required_names_from_rollup(contexts: list[object]) -> tuple[str, ...]:
    """Check-run / status context nodes that GitHub marks isRequired."""
    names: list[str] = []
    for item in contexts:
        if not isinstance(item, dict) or not item.get("isRequired"):
            continue
        name = item.get("name") or item.get("context")
        if name:
            names.append(str(name))
    return unique_names(names)


def skip_reason(
    *,
    labels: list[str],
    draft: bool,
    state: str,
    cross_repository: bool,
    head_sha: str,
    check_runs: dict[str, dict],
    mergeable: str,
    required: frozenset[str],
    required_checks: tuple[str, ...],
    paths: list[str] | None = None,
    config: dict | None = None,
) -> str | None:
    if cross_repository:
        return "pull request is from a fork"
    if state != "OPEN":
        return "pull request is not open"
    if draft:
        return "pull request is a draft"
    if mixes_governance(paths or [], config):
        return "mixes governance and non-governance"
    if not required:
        return "no automerge labels configured"
    missing_labels = sorted(required - {label.strip() for label in labels})
    if missing_labels:
        return "missing labels: " + ", ".join(missing_labels)
    if not required_checks:
        return "no required checks configured"
    if not head_sha:
        return "CI run does not match the pull request head"
    missing_checks = [name for name in required_checks if name not in check_runs]
    if missing_checks:
        return "required checks missing: " + ", ".join(missing_checks)
    mismatched = [
        name
        for name in required_checks
        if str(check_runs[name].get("head_sha") or "") != head_sha
    ]
    if mismatched:
        return "CI run does not match the pull request head"
    not_green = [
        name
        for name in required_checks
        if str(check_runs[name].get("status") or "") != "completed"
        or str(check_runs[name].get("conclusion") or "") != "success"
    ]
    if not_green:
        return "required checks not green: " + ", ".join(not_green)
    if mergeable == "CONFLICTING":
        return "merge conflict"
    return None


def strict_from_rules(payload: object) -> bool:
    """True when an effective ruleset requires branches to be up to date."""
    if not isinstance(payload, list):
        return False
    for rule in payload:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        if params.get("strict_required_status_checks_policy"):
            return True
    return False


def strict_from_protection(payload: object) -> bool:
    """True when classic branch protection requires branches to be up to date."""
    return isinstance(payload, dict) and bool(payload.get("strict"))


def needs_update(*, behind_by: int, merge_state: str, strict: bool) -> bool:
    """Whether a merge-ready PR must be brought up to date before it can merge.

    Only a PR that is actually behind its base counts. GitHub's BEHIND state, or a
    ruleset/protection that is known to require up-to-date branches, makes it
    count. A repo with strict off never gets an update (a behind PR merges as is).
    """
    if behind_by <= 0:
        return False
    return merge_state == "BEHIND" or strict


def is_not_up_to_date_error(text: str) -> bool:
    lowered = text.lower()
    return "not up to date" in lowered or "head branch is behind" in lowered


def is_stale_head_error(text: str) -> bool:
    """update-branch refused because the PR head moved after we read it."""
    return "expected head sha" in text.lower()


def is_conflict_error(text: str) -> bool:
    lowered = text.lower()
    return "conflict" in lowered


def runs_needing_approval(runs: object) -> list[int]:
    """Ids of pull_request runs held as action_required (list of run dicts)."""
    if not isinstance(runs, list):
        return []
    ids: list[int] = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        if item.get("event") != "pull_request":
            continue
        if str(item.get("conclusion") or item.get("status") or "") != "action_required":
            continue
        run_id = item.get("id")
        if isinstance(run_id, int):
            ids.append(run_id)
    return ids


def has_pull_request_run(runs: object) -> bool:
    return isinstance(runs, list) and any(
        isinstance(item, dict) and item.get("event") == "pull_request" for item in runs
    )


def notice_marker(kind: str, sha: str) -> str:
    return NOTICE_MARKER.format(kind=kind, sha=sha)


def notice_body(kind: str, sha: str, text: str) -> str:
    return f"{notice_marker(kind, sha)}\n{text}"


def approve_command(repo: str, run_id: int) -> list[str]:
    return ["gh", "api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/approve"]


def update_branch_command(repo: str, number: int, sha: str) -> list[str]:
    """update-branch with the default merge method (never a rebase) pinned to sha."""
    return [
        "gh",
        "api",
        "-X",
        "PUT",
        f"repos/{repo}/pulls/{number}/update-branch",
        "-f",
        f"expected_head_sha={sha}",
    ]


def merge_command(number: int, sha: str, repo: str) -> list[str]:
    """gh pr merge invocation. Never includes --admin (would bypass protection).

    Always pass -R so the command targets the calling repo. This script is
    checked out from AllHailSeizure/.github, and gh would otherwise infer
    that repository from the working tree.
    """
    return [
        "gh",
        "pr",
        "merge",
        str(number),
        "-R",
        repo,
        "--squash",
        "--delete-branch",
        "--match-head-commit",
        sha,
    ]


def issue_repository(ref: object) -> str:
    """Return owner/name from a closingIssuesReferences entry, or empty."""
    if not isinstance(ref, dict):
        return ""
    repo = ref.get("repository")
    if isinstance(repo, str) and "/" in repo:
        return repo
    if not isinstance(repo, dict):
        return ""
    owner = repo.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else owner
    name = repo.get("name")
    if login and name:
        return f"{login}/{name}"
    return ""


def same_repo_linked_issue_numbers(refs: object, repo: str) -> tuple[int, ...]:
    """Same-repo issue numbers from closingIssuesReferences. Other repos are skipped.

    An entry with no repository is treated as belonging to repo.
    """
    if not repo or not isinstance(refs, list):
        return ()
    numbers: list[int] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        home = issue_repository(ref)
        if home and home != repo:
            continue
        number = ref.get("number")
        if isinstance(number, int) and number > 0 and number not in numbers:
            numbers.append(number)
    return tuple(numbers)


def linked_issue_comment(pull_number: int) -> str:
    return f"Closed automatically after automerge of #{pull_number}."


def close_issue_command(number: int, repo: str, comment: str) -> list[str]:
    """gh issue close invocation. Always pass -R so gh targets the calling repo."""
    return [
        "gh",
        "issue",
        "close",
        str(number),
        "-R",
        repo,
        "--comment",
        comment,
    ]


def candidate_numbers_from_env(env: dict[str, str]) -> tuple[list[int], str]:
    """Return PR numbers from reusable-workflow inputs, plus a SHA to look up."""
    raw = (env.get("AUTOMERGE_PULL_NUMBER") or "").strip()
    if raw and raw not in {"0", "0.0"}:
        try:
            number = int(float(raw))
        except ValueError:
            number = 0
        if number > 0:
            return [number], ""
    sha = (env.get("AUTOMERGE_HEAD_SHA") or "").strip()
    return [], sha


def trigger_from_event(event_name: str, event: dict) -> tuple[list[int], str]:
    """Return pull request numbers, or a commit SHA to look up when the event has none."""
    if event_name in {"pull_request", "pull_request_target"}:
        pull = event.get("pull_request") or {}
        head = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
        base = ((pull.get("base") or {}).get("repo") or {}).get("full_name")
        if not head or head != base or "number" not in pull:
            return [], ""
        return [int(pull["number"])], ""

    if event_name != "workflow_run":
        return [], ""

    run = event.get("workflow_run") or {}
    if run.get("event") not in WORKFLOW_RUN_EVENTS or run.get("conclusion") != "success":
        return [], ""
    head_repo = (run.get("head_repository") or {}).get("full_name")
    repo = (event.get("repository") or {}).get("full_name")
    if not head_repo or head_repo != repo:
        return [], ""
    numbers = [
        int(pull["number"])
        for pull in (run.get("pull_requests") or [])
        if "number" in pull
    ]
    if numbers:
        return numbers, ""
    return [], str(run.get("head_sha") or "")


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)


def _gh_json(cmd: list[str], env: dict[str, str]) -> object:
    result = _run(cmd, env)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout)


def _gh_json_optional(path: str, env: dict[str, str]) -> object | None:
    """GET a REST path; return None on 404/403/other failure without exiting."""
    result = _run(["gh", "api", path], env)
    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _quote(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def _pull_request(number: int, env: dict[str, str]) -> dict:
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    payload = _gh_json(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "isDraft,state,mergeable,mergeStateStatus,headRefOid,headRefName,baseRefName,baseRefOid,labels,isCrossRepository,closingIssuesReferences",
        ],
        env,
    )
    if not isinstance(payload, dict):
        raise SystemExit(f"unexpected pull request payload for #{number}")
    return payload


def _check_runs(sha: str, env: dict[str, str]) -> dict[str, dict]:
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo or not sha:
        return {}
    result = _run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
            "--jq",
            ".check_runs[]",
        ],
        env,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    runs: list[object] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            runs.append(json.loads(line))
    return latest_check_runs_by_name(runs)


def _pulls_for_commit(sha: str, env: dict[str, str]) -> list[int]:
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo or not sha:
        return []
    result = _run(
        ["gh", "api", f"repos/{repo}/commits/{sha}/pulls", "--jq", ".[].number"],
        env,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    numbers: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            numbers.append(int(line))
    return numbers


def discover_required_checks(repo: str, branch: str, env: dict[str, str]) -> tuple[str, ...]:
    """Best-effort required check names from classic protection, rulesets, and the PR rollup.

    Classic protection and rulesets often cannot be read (no administration
    permission). Failures are ignored; callers should also pass required_checks.
    """
    names: list[str] = []
    if repo and branch:
        protection = _gh_json_optional(
            f"repos/{repo}/branches/{_quote(branch)}/protection/required_status_checks",
            env,
        )
        names.extend(required_names_from_protection(protection))
        rules = _gh_json_optional(f"repos/{repo}/rules/branches/{_quote(branch)}", env)
        names.extend(required_names_from_rules(rules))
    return unique_names(names)


def rollup_required_checks(repo: str, number: int, env: dict[str, str]) -> tuple[str, ...]:
    if not repo or not number:
        return ()
    owner, _, name = repo.partition("/")
    query = """
    query ($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          commits(last: 1) {
            nodes {
              commit {
                statusCheckRollup {
                  contexts(first: 100) {
                    nodes {
                      __typename
                      ... on CheckRun { name isRequired }
                      ... on StatusContext { context isRequired }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    result = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ],
        env,
    )
    if result.returncode != 0:
        return ()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ()
    try:
        nodes = (
            payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0][
                "commit"
            ]["statusCheckRollup"]["contexts"]["nodes"]
        )
    except (TypeError, KeyError, IndexError):
        return ()
    return required_names_from_rollup(nodes or [])


def _squash_merge(number: int, sha: str, env: dict[str, str]) -> tuple[bool, str]:
    """Run gh pr merge. Returns (ok, error text)."""
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("GITHUB_REPOSITORY is not set", file=sys.stderr)
        return False, "GITHUB_REPOSITORY is not set"
    result = _run(merge_command(number, sha, repo), env)
    if result.returncode != 0:
        text = result.stderr or result.stdout
        print(text, file=sys.stderr)
        return False, text
    if result.stdout.strip():
        print(result.stdout.strip())
    return True, ""


def _behind_by(repo: str, base_branch: str, sha: str, env: dict[str, str]) -> int:
    """Commits on the base branch that the head does not have (0 when unknown)."""
    if not repo or not base_branch or not sha:
        return 0
    result = _run(
        [
            "gh",
            "api",
            f"repos/{repo}/compare/{_quote(base_branch)}...{sha}",
            "--jq",
            ".behind_by",
        ],
        env,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _strict_required(repo: str, branch: str, env: dict[str, str]) -> bool:
    """Best-effort: does the base branch require up-to-date branches?"""
    rules = _gh_json_optional(f"repos/{repo}/rules/branches/{_quote(branch)}", env)
    if strict_from_rules(rules):
        return True
    protection = _gh_json_optional(
        f"repos/{repo}/branches/{_quote(branch)}/protection/required_status_checks", env
    )
    return strict_from_protection(protection)


def _runs_for_head(repo: str, sha: str, env: dict[str, str]) -> list[dict]:
    result = _run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
            "--jq",
            "[.workflow_runs[]|{id,event,status,conclusion}]",
        ],
        env,
    )
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _has_notice(number: int, marker: str, repo: str, env: dict[str, str]) -> bool:
    result = _run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/comments?per_page=100",
            "--jq",
            ".[].body",
        ],
        env,
    )
    return result.returncode == 0 and marker in result.stdout


def _post_notice(
    number: int, kind: str, sha: str, text: str, repo: str, env: dict[str, str]
) -> None:
    """Comment once per (PR, kind, head sha) so a stuck PR is explained, not spammed."""
    marker = notice_marker(kind, sha)
    if _has_notice(number, marker, repo, env):
        return
    result = _run(
        [
            "gh",
            "pr",
            "comment",
            str(number),
            "-R",
            repo,
            "--body",
            notice_body(kind, sha, text),
        ],
        env,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)


def _head_sha(number: int, repo: str, env: dict[str, str]) -> str:
    result = _run(
        ["gh", "pr", "view", str(number), "-R", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
        env,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


class Outcome(NamedTuple):
    """Result of handling one PR. ok False fails the run."""

    ok: bool
    updated_sha: str = ""  # head after the gate updated the branch (CI released)
    merged: bool = False


def update_and_approve(
    number: int,
    sha: str,
    base_branch: str,
    repo: str,
    env: dict[str, str],
) -> Outcome:
    """Merge the base branch into the PR branch and release the CI run it starts.

    Returns ok False only when the PR is left stuck by a failure the gate caused
    (the update landed but the CI run could not be released). Conflicts and
    refused updates are reported on the PR and are not failures. updated_sha is
    set when the branch was updated and CI is running on the new head.
    """
    result = _run(update_branch_command(repo, number, sha), env)
    if result.returncode != 0:
        text = result.stderr or result.stdout
        if is_stale_head_error(text):
            print(f"#{number}: head moved while updating; a later run will handle it")
            return Outcome(True)
        print(text, file=sys.stderr)
        if is_conflict_error(text):
            reason = f"it conflicts with `{base_branch}`. Resolve the conflicts and push."
            kind = "conflict"
        else:
            reason = "GitHub refused the automatic update (for example the new commits change workflow files, which a workflow cannot push). Use Update branch by hand."
            kind = "update-refused"
        print(f"#{number}: not updated ({kind})")
        _post_notice(
            number,
            kind,
            sha,
            f"Automerge could not bring this branch up to date: {reason}",
            repo,
            env,
        )
        return Outcome(True)
    print(f"#{number}: updating branch from {base_branch}")
    new_sha = sha
    deadline = time.monotonic() + UPDATE_WAIT_SECONDS
    while time.monotonic() < deadline:
        new_sha = _head_sha(number, repo, env) or sha
        if new_sha != sha:
            break
        time.sleep(UPDATE_POLL_SECONDS)
    if new_sha == sha:
        print(f"#{number}: update requested but the head did not move", file=sys.stderr)
        return Outcome(False)
    runs: list[dict] = []
    deadline = time.monotonic() + UPDATE_WAIT_SECONDS
    while time.monotonic() < deadline:
        runs = _runs_for_head(repo, new_sha, env)
        if has_pull_request_run(runs):
            break
        time.sleep(UPDATE_POLL_SECONDS)
    approved = 0
    for run_id in runs_needing_approval(runs):
        released = _run(approve_command(repo, run_id), env)
        if released.returncode != 0:
            print(released.stderr or released.stdout, file=sys.stderr)
            _post_notice(
                number,
                "approve-failed",
                new_sha,
                "Automerge updated this branch but could not release the CI run it started "
                "(it is waiting for approval). Approve it in the Actions tab.",
                repo,
                env,
            )
            return Outcome(False)
        approved += 1
    if not has_pull_request_run(runs):
        print(f"#{number}: no pull_request CI run appeared on {new_sha[:7]}", file=sys.stderr)
    print(
        f"#{number}: updated {sha[:7]} -> {new_sha[:7]}; released {approved} held CI run(s)"
    )
    return Outcome(True, updated_sha=new_sha)


def _issue_state(number: int, repo: str, env: dict[str, str]) -> str:
    result = _run(
        ["gh", "api", f"repos/{repo}/issues/{number}", "--jq", ".state"],
        env,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return ""
    return result.stdout.strip().lower()


def close_linked_issues(
    pull_number: int, refs: object, repo: str, env: dict[str, str]
) -> bool:
    """Close still-open same-repo issues linked by closingIssuesReferences.

    Already-closed issues are left alone. A failed lookup or close is an error.
    Cross-repo references are skipped.
    """
    if not repo:
        print("GITHUB_REPOSITORY is not set", file=sys.stderr)
        return False
    ok = True
    comment = linked_issue_comment(pull_number)
    for number in same_repo_linked_issue_numbers(refs, repo):
        state = _issue_state(number, repo, env)
        if state == "closed":
            print(f"#{pull_number}: issue #{number} already closed")
            continue
        if state != "open":
            print(
                f"#{pull_number}: failed to close issue #{number} "
                f"(state {state or 'unknown'})",
                file=sys.stderr,
            )
            ok = False
            continue
        result = _run(close_issue_command(number, repo, comment), env)
        if result.returncode != 0:
            print(result.stderr or result.stdout, file=sys.stderr)
            ok = False
            continue
        if result.stdout.strip():
            print(result.stdout.strip())
        print(f"#{pull_number}: closed issue #{number}")
    return ok


def consider(number: int, env: dict[str, str], *, allow_merge: bool = True) -> Outcome:
    """Merge a ready PR, or bring it up to date when it is behind its base.

    With allow_merge false (picking the next PR after a merge) a ready PR is
    only updated when behind; it is never merged.
    """
    pull = _pull_request(number, env)
    sha = str(pull.get("headRefOid") or "")
    runs = _check_runs(sha, env) if sha else {}
    labels = [
        str(item.get("name") or "")
        for item in (pull.get("labels") or [])
        if isinstance(item, dict)
    ]
    repo = env.get("GITHUB_REPOSITORY", "")
    branch = str(pull.get("baseRefName") or "")
    discovered = collect_required_checks(
        discovered=discover_required_checks(repo, branch, env),
        configured=rollup_required_checks(repo, number, env),
    )
    required_checks = collect_required_checks(
        configured=configured_check_names(),
        discovered=discovered,
    )
    base_ref = str(pull.get("baseRefOid") or pull.get("baseRefName") or "")
    try:
        config = load_effective_config(repo, base_ref, env) if repo and base_ref else None
        paths = list_pr_paths(repo, number, env) if repo else []
    except AddonError as exc:
        print(f"#{number}: skip (governance config: {exc})")
        return Outcome(True)
    reason = skip_reason(
        labels=labels,
        draft=bool(pull.get("isDraft")),
        state=str(pull.get("state") or ""),
        cross_repository=bool(pull.get("isCrossRepository")),
        head_sha=sha,
        check_runs=runs,
        mergeable=str(pull.get("mergeable") or ""),
        required=required_labels(),
        required_checks=required_checks,
        paths=paths,
        config=config,
    )
    if reason:
        print(f"#{number}: skip ({reason})")
        if reason == "merge conflict":
            _post_notice(
                number,
                "conflict",
                sha,
                f"Automerge cannot merge this: it conflicts with `{branch}`. "
                "Resolve the conflicts and push; automerge retries when CI passes.",
                repo,
                env,
            )
        return Outcome(True)
    if _behind_and_must_update(pull, sha, branch, repo, env):
        return update_and_approve(number, sha, branch, repo, env)
    if not allow_merge:
        print(f"#{number}: not behind {branch}; left for its own run")
        return Outcome(True)
    print(f"#{number}: squash-merging")
    merged, error = _squash_merge(number, sha, env)
    if not merged:
        if is_not_up_to_date_error(error):
            print(f"#{number}: base moved before the merge; updating instead")
            return update_and_approve(number, sha, branch, repo, env)
        return Outcome(False)
    ok = close_linked_issues(number, pull.get("closingIssuesReferences"), repo, env)
    return Outcome(ok, merged=True)


def _behind_and_must_update(
    pull: dict, sha: str, branch: str, repo: str, env: dict[str, str]
) -> bool:
    behind = _behind_by(repo, branch, sha, env)
    if behind <= 0:
        return False
    return needs_update(
        behind_by=behind,
        merge_state=str(pull.get("mergeStateStatus") or ""),
        strict=_strict_required(repo, branch, env),
    )


def labelled_open_numbers(branch: str, env: dict[str, str]) -> list[int]:
    repo = env.get("GITHUB_REPOSITORY", "")
    labels = sorted(required_labels())
    if not repo or not labels:
        return []
    cmd = ["gh", "pr", "list", "-R", repo, "--state", "open", "--base", branch,
           "--limit", "100", "--json", "number", "--jq", ".[].number"]
    for label in labels:
        cmd += ["--label", label]
    result = _run(cmd, env)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return []
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def next_behind(
    merged_number: int, branch: str, env: dict[str, str]
) -> tuple[int, Outcome] | None:
    """After a merge, update the first other labelled PR the merge left behind.

    One at a time keeps the queue serial: each update costs a CI run, and the
    next merge would leave every other updated PR behind again. Returns that
    PR's number and outcome, or None when nothing was updated. Problems are
    reported but never fail the merge that led here.
    """
    for other in labelled_open_numbers(branch, env):
        if other == merged_number:
            continue
        try:
            outcome = consider(other, env, allow_merge=False)
        except SystemExit as exc:
            print(f"#{other}: skipped (exit {exc.code})", file=sys.stderr)
            continue
        if outcome.updated_sha:
            return other, outcome
    return None


def _wait_seconds(env: dict[str, str]) -> float:
    raw = (env.get("AUTOMERGE_WAIT_MINUTES") or "").strip()
    try:
        minutes = float(raw)
    except ValueError:
        return CI_WAIT_SECONDS
    return minutes * 60 if minutes > 0 else CI_WAIT_SECONDS


def checks_finished(
    runs: dict[str, dict], required_checks: tuple[str, ...], sha: str
) -> bool:
    """True when every required check has a completed run on sha."""
    if not required_checks:
        return False
    for name in required_checks:
        run = runs.get(name)
        if not run or str(run.get("head_sha") or "") != sha:
            return False
        if str(run.get("status") or "") != "completed":
            return False
    return True


def wait_for_ci(number: int, sha: str, deadline: float, env: dict[str, str]) -> bool:
    """Wait for CI on sha to finish. False on timeout.

    Returns early when the PR head moves or the PR closes; the caller looks at
    the PR again either way.
    """
    repo = env.get("GITHUB_REPOSITORY", "")
    limit = min(deadline, time.monotonic() + _wait_seconds(env))
    while time.monotonic() < limit:
        pull = _pull_request(number, env)
        if str(pull.get("state") or "") != "OPEN":
            return True
        head = str(pull.get("headRefOid") or "")
        if head and head != sha:
            print(f"#{number}: head moved to {head[:7]} while waiting for CI")
            return True
        required = collect_required_checks(
            configured=configured_check_names(),
            discovered=discover_required_checks(
                repo, str(pull.get("baseRefName") or ""), env
            ),
        )
        if checks_finished(_check_runs(sha, env), required, sha):
            return True
        time.sleep(CI_POLL_SECONDS)
    print(f"#{number}: CI on {sha[:7]} did not finish in time; leaving it", file=sys.stderr)
    return False


def process(number: int, env: dict[str, str]) -> bool:
    """Carry a PR through: merge it, or update it, wait for CI, then merge it.

    After a merge the next behind PR is updated and carried through the same
    way, until nothing is left or the time budget is spent.
    """
    deadline = time.monotonic() + QUEUE_TOTAL_SECONDS
    current = number
    allow_merge = True
    for _ in range(MAX_QUEUE_STEPS):
        outcome = consider(current, env, allow_merge=allow_merge)
        if not outcome.ok:
            return False
        if outcome.updated_sha:
            if not wait_for_ci(current, outcome.updated_sha, deadline, env):
                return True
            allow_merge = True
            continue
        if not outcome.merged or time.monotonic() >= deadline:
            return True
        branch = str(_pull_request(current, env).get("baseRefName") or "")
        following = next_behind(current, branch, env)
        if following is None:
            return True
        current, outcome = following
        if not wait_for_ci(current, outcome.updated_sha, deadline, env):
            return True
        allow_merge = True
    return True


def candidate_numbers(event_name: str, event: dict, env: dict[str, str]) -> list[int]:
    numbers, lookup_sha = candidate_numbers_from_env(env)
    if not numbers and not lookup_sha:
        numbers, lookup_sha = trigger_from_event(event_name, event)
    if numbers or not lookup_sha:
        return numbers
    return _pulls_for_commit(lookup_sha, env)


def main() -> None:
    env = os.environ.copy()
    event_name = env.get("AUTOMERGE_EVENT_NAME") or env.get("GITHUB_EVENT_NAME", "")
    event: dict = {}
    event_path = env.get("GITHUB_EVENT_PATH", "")
    if event_path:
        try:
            loaded = json.loads(Path(event_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                event = loaded
        except (OSError, json.JSONDecodeError):
            event = {}
    numbers = candidate_numbers(event_name, event, env)
    if not numbers:
        print("No pull request to consider.")
        return
    failed = False
    seen: set[int] = set()
    for number in numbers:
        if number in seen:
            continue
        seen.add(number)
        if not process(number, env):
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
