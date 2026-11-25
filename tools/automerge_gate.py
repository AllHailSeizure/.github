#!/usr/bin/env python3
"""Squash-merge a pull request when the automerge label and required checks are green.

Never bypasses branch protection or rulesets. An empty required-check list
never merges: a repo without required checks (no ruleset or classic branch
protection) must not opt in.

The script is self-contained. It does not import hotel-kline-game Tools/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

# Every listed label must be on the pull request. Override with AUTOMERGE_LABELS
# (comma-separated). An empty list never merges.
DEFAULT_REQUIRED_LABELS = "automerge"


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
) -> str | None:
    if cross_repository:
        return "pull request is from a fork"
    if state != "OPEN":
        return "pull request is not open"
    if draft:
        return "pull request is a draft"
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


def merge_command(number: int, sha: str) -> list[str]:
    """gh pr merge invocation. Never includes --admin (would bypass protection)."""
    return [
        "gh",
        "pr",
        "merge",
        str(number),
        "--squash",
        "--delete-branch",
        "--match-head-commit",
        sha,
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
    if run.get("event") != "pull_request" or run.get("conclusion") != "success":
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
    payload = _gh_json(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "isDraft,state,mergeable,headRefOid,headRefName,baseRefName,labels,isCrossRepository",
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

    GITHUB_TOKEN often cannot read protection or rulesets (no administration
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


def _squash_merge(number: int, sha: str, env: dict[str, str]) -> bool:
    result = _run(merge_command(number, sha), env)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return False
    if result.stdout.strip():
        print(result.stdout.strip())
    return True


def consider(number: int, env: dict[str, str]) -> bool:
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
    )
    if reason:
        print(f"#{number}: skip ({reason})")
        return True
    print(f"#{number}: squash-merging")
    return _squash_merge(number, sha, env)


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
        if not consider(number, env):
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
