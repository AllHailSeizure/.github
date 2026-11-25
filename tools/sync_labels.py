#!/usr/bin/env python3
"""Sync labels.yml to every owned, non-fork, non-archived repository.

Discovers target repos from the GitHub API (current names), excluding
forks, archived repos, and the `.github` repo itself. Per-repo extras
and overrides live under `repos` in labels.yml; shared labels apply to
every discovered repo.

Dry-run by default. Renames go through GitHub's rename API so labels
stay on issues and PRs. Deletes run only with --allow-deletes, and
never remove a repo's must-keep labels (hotel-kline-game in particular).
Scheduled Actions runs always apply and never delete.

Auth uses the gh CLI (GH_TOKEN / a logged-in gh session). In Actions,
set GH_TOKEN from the LABEL_SYNC_TOKEN secret (a fine-grained PAT with
Issues read/write on the target repos). GITHUB_TOKEN cannot write
labels on other repositories.

Usage:
  python3 tools/sync_labels.py --dry-run
  python3 tools/sync_labels.py --dry-run --repo synapse
  python3 tools/sync_labels.py --apply --allow-deletes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("This script needs PyYAML. Install it with: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEME = ROOT / "labels.yml"
DOT_GITHUB_REPO = ".github"
REDIRECT_STATUSES = {301, 302, 307, 308}
MAX_REDIRECTS = 5
TYPE_AND_PRIORITY_NAMES = frozenset(
    {
        "bug",
        "feat",
        "docs",
        "refactor",
        "tooling",
        "test",
        "assets",
        "question",
        "P1",
        "P2",
        "P3",
    }
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env_dry = os.environ.get("DRY_RUN", "true").lower() not in ("false", "0", "no")
    env_deletes = os.environ.get("ALLOW_DELETES", "false").lower() in ("true", "1", "yes")
    env_repos = [r.strip() for r in os.environ.get("REPOS", "").split(",") if r.strip()]

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", help="Plan changes without applying them (default)")
    mode.add_argument("--apply", dest="dry_run", action="store_false", help="Apply planned creates, renames, and updates")
    parser.set_defaults(dry_run=env_dry)
    parser.add_argument(
        "--allow-deletes",
        action="store_true",
        default=env_deletes,
        help="Also delete labels listed under delete_when_enabled (never must-keep)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repo_flags",
        default=None,
        help="Limit to one repo name (repeatable). Default: every discovered owned repo",
    )
    parser.add_argument(
        "--repos",
        dest="repos_csv",
        default=None,
        help="Optional comma-separated repo names (same as the REPOS input)",
    )
    parser.add_argument("--scheme", type=Path, default=Path(os.environ.get("LABEL_SYNC_FILE", DEFAULT_SCHEME)))
    args = parser.parse_args(argv)
    args.repos = _merge_repo_filters(env_repos, args.repo_flags, args.repos_csv)
    return args


def _merge_repo_filters(
    from_env: list[str],
    repo_flags: list[str] | None,
    repos_csv: str | None,
) -> list[str] | None:
    names: list[str] = []
    for source in (from_env, repo_flags, [p.strip() for p in (repos_csv or "").split(",") if p.strip()]):
        for name in source or []:
            if name not in names:
                names.append(name)
    return names or None


def resolve_modes(event_name: str | None, dry_run: bool, allow_deletes: bool) -> tuple[bool, bool]:
    """Scheduled runs always apply and never delete, ignoring flags and inputs."""
    if (event_name or "").lower() == "schedule":
        return False, False
    return dry_run, allow_deletes


def load_scheme(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not data or "owner" not in data or "shared" not in data:
        sys.exit(f"{path} must define owner and shared")
    for label in data["shared"]:
        _require_label(label, f"{path} shared")
    data.setdefault("renames", {})
    data.setdefault("delete_when_enabled", [])
    data.setdefault("repos", {})
    if data["repos"] is None:
        data["repos"] = {}
    for name, cfg in data["repos"].items():
        if cfg is None:
            data["repos"][name] = cfg = {}
        cfg.setdefault("extras", [])
        cfg.setdefault("must_keep", [])
        cfg.setdefault("keep_existing_type_and_priority_colours", False)
        for label in cfg["extras"]:
            _require_label(label, f"{path} repos.{name}.extras")
    return data


def repo_config(scheme: dict[str, Any], repo: str) -> dict[str, Any]:
    cfg = dict((scheme.get("repos") or {}).get(repo) or {})
    cfg.setdefault("extras", [])
    cfg.setdefault("must_keep", [])
    cfg.setdefault("keep_existing_type_and_priority_colours", False)
    return cfg


def _require_label(label: dict[str, Any], where: str) -> None:
    for key in ("name", "color", "description"):
        if key not in label:
            sys.exit(f"{where}: label missing {key!r}: {label}")
    color = str(label["color"]).removeprefix("#")
    if len(color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in color):
        sys.exit(f"{where}: invalid colour {label['color']!r} on {label['name']}")
    label["color"] = color.upper()


def desired_for(scheme: dict[str, Any], repo: str) -> dict[str, dict[str, str]]:
    labels = {item["name"]: item for item in scheme["shared"]}
    for item in repo_config(scheme, repo).get("extras") or []:
        labels[item["name"]] = item
    return labels


def norm_color(value: str | None) -> str:
    return (value or "").removeprefix("#").lower()


def colours_differ(actual: str | None, wanted: str | None) -> bool:
    return norm_color(actual) != norm_color(wanted)


def descriptions_differ(actual: str | None, wanted: str | None) -> bool:
    return (actual or "") != (wanted or "")


def quote_label(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def repo_from_api_path(path: str) -> str | None:
    parts = path.split("?")[0].strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "repos":
        return urllib.parse.unquote(parts[2])
    return None


def api_path_from_url(url: str) -> str:
    prefix = "https://api.github.com/"
    if url.startswith(prefix):
        return url[len(prefix) :]
    if url.startswith("http://api.github.com/"):
        return url[len("http://api.github.com/") :]
    return url.lstrip("/")


def parse_http_status(text: str) -> int | None:
    match = re.search(r"HTTP(?:/[0-9.]+)?\s+([1-5]\d\d)\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\(HTTP (\d{3})\)", text)
    if match:
        return int(match.group(1))
    return None


def redirect_target(stdout: str, stderr: str) -> str | None:
    text = f"{stdout}\n{stderr}"
    for line in text.splitlines():
        if line.lower().startswith("location:"):
            return api_path_from_url(line.split(":", 1)[1].strip())
    for blob in (stdout, stderr):
        stripped = blob.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("url"):
            return api_path_from_url(str(data["url"]))
    return None


def _run_gh(cmd: list[str], body: dict[str, Any] | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=json.dumps(body) if body is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )


def gh_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    paginate: bool = False,
    repo: str | None = None,
) -> Any:
    current = path
    seen: set[str] = set()
    last_detail = ""
    for _ in range(MAX_REDIRECTS + 1):
        cmd = ["gh", "api", "--method", method]
        if paginate:
            cmd.append("--paginate")
        cmd.append(current)
        if body is not None:
            cmd.extend(["--input", "-"])
        result = _run_gh(cmd, body)
        if result.returncode == 0:
            if not result.stdout.strip():
                return None
            return json.loads(result.stdout)

        last_detail = (result.stderr or result.stdout or "").strip()
        status = parse_http_status(f"{result.stdout}\n{result.stderr}")
        target = redirect_target(result.stdout, result.stderr)
        if status not in REDIRECT_STATUSES and target is None:
            break
        if target is None:
            # gh often omits headers on failure; fetch them so we can follow.
            probe = _run_gh(["gh", "api", "--method", method, "--include", current] + (
                ["--input", "-"] if body is not None else []
            ), body)
            target = redirect_target(probe.stdout, probe.stderr)
            last_detail = (probe.stderr or probe.stdout or last_detail).strip()
            status = parse_http_status(f"{probe.stdout}\n{probe.stderr}") or status
        if not target or target in seen or target == current:
            name = repo or repo_from_api_path(path) or "unknown"
            raise RuntimeError(
                f"HTTP {status or 'redirect'} for repo {name} on {method} {path}: "
                f"the repository may have been renamed. {last_detail}"
            )
        seen.add(current)
        current = target
    else:
        name = repo or repo_from_api_path(path) or "unknown"
        raise RuntimeError(f"Too many redirects for repo {name} on {method} {path}")

    name = repo or repo_from_api_path(path) or "unknown"
    raise RuntimeError(f"API request for {name} failed: {method} {path}: {last_detail}")


def flatten_pages(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        if data and all(isinstance(item, list) for item in data):
            return [item for page in data for item in page]
        return data
    raise RuntimeError(f"unexpected API payload: {type(data).__name__}")


def discover_owned_repos(owner: str, payload: Any) -> list[str]:
    names: list[str] = []
    for item in flatten_pages(payload):
        if not isinstance(item, dict):
            continue
        if item.get("fork") or item.get("archived"):
            continue
        name = item.get("name") or ""
        if name == DOT_GITHUB_REPO or not name:
            continue
        login = ((item.get("owner") or {}) or {}).get("login")
        if login and login != owner:
            continue
        names.append(name)
    return names


def list_owned_repos(owner: str) -> list[str]:
    data = gh_api("GET", "user/repos?affiliation=owner&per_page=100", paginate=True)
    return discover_owned_repos(owner, data)


def select_targets(
    discovered: list[str],
    extras_keys: list[str],
    filters: list[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (targets, warnings, unknown_filters)."""
    known = set(discovered)
    warnings = [
        f"labels.yml has extras for {name!r}, which was not discovered; ignoring them"
        for name in extras_keys
        if name not in known
    ]
    if not filters:
        return list(discovered), warnings, []
    targets: list[str] = []
    unknown: list[str] = []
    for name in filters:
        if name in known:
            targets.append(name)
        else:
            unknown.append(name)
    return targets, warnings, unknown


def list_labels(owner: str, repo: str) -> dict[str, dict[str, Any]]:
    data = gh_api(
        "GET",
        f"repos/{owner}/{quote_label(repo)}/labels?per_page=100",
        paginate=True,
        repo=repo,
    )
    items = flatten_pages(data)
    return {item["name"]: item for item in items if isinstance(item, dict) and "name" in item}


def label_usage(owner: str, repo: str, name: str) -> int:
    query = """
    query ($owner: String!, $name: String!, $label: String!) {
      repository(owner: $owner, name: $name) {
        label(name: $label) {
          issues { totalCount }
          pullRequests { totalCount }
        }
      }
    }
    """
    data = gh_api(
        "POST",
        "graphql",
        {"query": query, "variables": {"owner": owner, "name": repo, "label": name}},
        repo=repo,
    )
    label = (data or {}).get("data", {}).get("repository", {}) or {}
    label = label.get("label") or {}
    issues = (label.get("issues") or {}).get("totalCount", 0)
    prs = (label.get("pullRequests") or {}).get("totalCount", 0)
    return int(issues) + int(prs)


class Plan:
    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.renames: list[tuple[str, str]] = []
        self.creates: list[dict[str, str]] = []
        self.updates: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.skipped: list[str] = []
        self.blocked: list[str] = []

    @property
    def has_changes(self) -> bool:
        return bool(self.renames or self.creates or self.updates or self.deletes)


def plan_repo(scheme: dict[str, Any], repo: str) -> Plan:
    owner = scheme["owner"]
    cfg = repo_config(scheme, repo)
    desired = desired_for(scheme, repo)
    # Only type and priority colours are left alone on hotel-kline-game.
    must_keep = set(cfg.get("must_keep") or [])
    keep_colours = bool(cfg.get("keep_existing_type_and_priority_colours"))
    existing = list_labels(owner, repo)
    plan = Plan(repo)

    remaining = dict(existing)
    for old, new in scheme["renames"].items():
        if old not in remaining:
            continue
        if new in remaining:
            plan.skipped.append(
                f"cannot rename {old!r} to {new!r}: {new!r} already exists; "
                f"{old!r} becomes a delete candidate"
            )
            continue
        if new not in desired:
            plan.skipped.append(f"rename map {old!r} -> {new!r} skipped: {new!r} is not a desired label here")
            continue
        plan.renames.append((old, new))
        entry = dict(remaining.pop(old))
        entry["name"] = new
        wanted = desired[new]
        if not (keep_colours and new in TYPE_AND_PRIORITY_NAMES):
            entry["color"] = wanted["color"]
        entry["description"] = wanted["description"]
        remaining[new] = entry

    for name, wanted in desired.items():
        if name not in remaining:
            plan.creates.append({"name": name, "color": wanted["color"], "description": wanted["description"]})
            continue
        actual = remaining[name]
        color_change = colours_differ(actual.get("color"), wanted["color"])
        desc_change = descriptions_differ(actual.get("description"), wanted["description"])
        skip_color = keep_colours and name in TYPE_AND_PRIORITY_NAMES and color_change
        if skip_color:
            plan.skipped.append(
                f"keep existing colour on {name} ({actual.get('color')}, not {wanted['color']})"
            )
            color_change = False
        if not color_change and not desc_change:
            continue
        update = {
            "name": name,
            "color": wanted["color"] if color_change else actual.get("color", wanted["color"]),
            "description": wanted["description"] if desc_change else (actual.get("description") or ""),
            "change_color": color_change,
            "change_description": desc_change,
        }
        plan.updates.append(update)

    delete_candidates = set(scheme["delete_when_enabled"])
    for old, new in scheme["renames"].items():
        # Blocked rename: old name still present and the target already existed.
        if old in remaining and new in remaining and old not in desired:
            delete_candidates.add(old)

    for name in sorted(delete_candidates):
        if name not in remaining:
            continue
        if name in desired:
            plan.skipped.append(f"will not delete {name!r}: it is a desired label")
            continue
        if name in must_keep:
            plan.blocked.append(f"refusing to delete must-keep label {name!r}")
            continue
        uses = label_usage(owner, repo, name)
        plan.deletes.append({"name": name, "uses": uses})

    return plan


def format_plan(plan: Plan, *, allow_deletes: bool, dry_run: bool) -> str:
    lines = [f"{plan.repo}"]
    if not plan.has_changes and not plan.skipped and not plan.blocked:
        lines.append("  (no changes)")
        return "\n".join(lines)
    for old, new in plan.renames:
        lines.append(f"  rename  {old} -> {new}")
    for item in plan.creates:
        lines.append(f"  create  {item['name']} #{item['color']} — {item['description']}")
    for item in plan.updates:
        bits = []
        if item["change_color"]:
            bits.append(f"color #{item['color']}")
        if item["change_description"]:
            bits.append(f"description {item['description']!r}")
        lines.append(f"  update  {item['name']} ({', '.join(bits)})")
    for item in plan.deletes:
        uses = f"{item['uses']} issue/PR use(s)"
        if allow_deletes:
            lines.append(f"  delete  {item['name']} ({uses})")
        else:
            lines.append(f"  delete  {item['name']} ({uses}; held until --allow-deletes)")
    for note in plan.blocked:
        lines.append(f"  blocked {note}")
    for note in plan.skipped:
        lines.append(f"  skip    {note}")
    if dry_run:
        lines.append("  (dry-run: nothing applied)")
    return "\n".join(lines)


def apply_plan(scheme: dict[str, Any], plan: Plan, *, allow_deletes: bool) -> None:
    owner = scheme["owner"]
    repo = plan.repo
    base = f"repos/{owner}/{quote_label(repo)}/labels"
    for old, new in plan.renames:
        wanted = desired_for(scheme, repo)[new]
        gh_api(
            "PATCH",
            f"{base}/{quote_label(old)}",
            {"new_name": new, "color": wanted["color"], "description": wanted["description"]},
            repo=repo,
        )
        print(f"  renamed {old} -> {new}", flush=True)
    for item in plan.creates:
        gh_api(
            "POST",
            base,
            {"name": item["name"], "color": item["color"], "description": item["description"]},
            repo=repo,
        )
        print(f"  created {item['name']}", flush=True)
    for item in plan.updates:
        gh_api(
            "PATCH",
            f"{base}/{quote_label(item['name'])}",
            {"color": item["color"], "description": item["description"]},
            repo=repo,
        )
        print(f"  updated {item['name']}", flush=True)
    if allow_deletes:
        for item in plan.deletes:
            gh_api("DELETE", f"{base}/{quote_label(item['name'])}", repo=repo)
            print(f"  deleted {item['name']}", flush=True)


def print_summary(
    plans: list[Plan],
    *,
    allow_deletes: bool,
    dry_run: bool,
    failures: list[tuple[str, str]] | None = None,
) -> None:
    held = not allow_deletes
    delete_word = "delete (held)" if held else "delete"
    print()
    print(f"{'repo':<22} {'create':>7} {'rename':>7} {'update':>7} {delete_word:>13}")
    totals = [0, 0, 0, 0]
    for plan in plans:
        counts = (len(plan.creates), len(plan.renames), len(plan.updates), len(plan.deletes))
        for i, n in enumerate(counts):
            totals[i] += n
        print(f"{plan.repo:<22} {counts[0]:7d} {counts[1]:7d} {counts[2]:7d} {counts[3]:13d}")
    for name, _ in failures or []:
        if not any(plan.repo == name for plan in plans):
            print(f"{name:<22} {'FAILED':>7}")
    print(f"{'TOTAL':<22} {totals[0]:7d} {totals[1]:7d} {totals[2]:7d} {totals[3]:13d}")
    mode = "dry-run, nothing applied" if dry_run else "applied"
    deletes = "deletes held (pass --allow-deletes to apply them)" if held and totals[3] else (
        "deletes included" if allow_deletes else "no deletes planned"
    )
    print(f"{mode}; {deletes}")
    if failures:
        print("FAILED repos:")
        for name, detail in failures:
            print(f"  {name}: {detail}")


def ensure_auth() -> None:
    try:
        gh_api("GET", "user")
    except RuntimeError as exc:
        sys.exit(
            "Could not authenticate with gh. In Actions, set the LABEL_SYNC_TOKEN "
            "repository secret (fine-grained PAT, Issues read/write on the target "
            f"repos) and pass it as GH_TOKEN. {exc}"
        )


def sync_repos(
    scheme: dict[str, Any],
    repos: list[str],
    *,
    dry_run: bool,
    allow_deletes: bool,
) -> tuple[list[Plan], list[tuple[str, str]]]:
    plans: list[Plan] = []
    failures: list[tuple[str, str]] = []
    for repo in repos:
        try:
            plan = plan_repo(scheme, repo)
            print(format_plan(plan, allow_deletes=allow_deletes, dry_run=dry_run))
            print()
            if not dry_run:
                apply_plan(scheme, plan, allow_deletes=allow_deletes)
            plans.append(plan)
        except (RuntimeError, OSError, json.JSONDecodeError) as exc:
            failures.append((repo, str(exc)))
            print(f"{repo}\n  failed  {exc}\n", flush=True)
    return plans, failures


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run, allow_deletes = resolve_modes(os.environ.get("GITHUB_EVENT_NAME"), args.dry_run, args.allow_deletes)
    scheme = load_scheme(args.scheme)

    ensure_auth()
    discovered = list_owned_repos(scheme["owner"])
    targets, warnings, unknown = select_targets(discovered, list(scheme.get("repos") or {}), args.repos)
    for note in warnings:
        print(f"warning: {note}")
    failures: list[tuple[str, str]] = [(name, "not a discovered owned repo (check the name, or that it is not a fork or archived)") for name in unknown]
    for name, detail in failures:
        print(f"{name}\n  failed  {detail}\n")

    mode = "DRY-RUN" if dry_run else "APPLY"
    deletes = "deletes enabled" if allow_deletes else "deletes held"
    print(f"{mode} label sync for {scheme['owner']} ({deletes})")
    print(f"scheme: {args.scheme}")
    print(f"discovered: {', '.join(discovered) if discovered else '(none)'}")
    if args.repos:
        print(f"filter: {', '.join(args.repos)}")
    print()

    plans, loop_failures = sync_repos(scheme, targets, dry_run=dry_run, allow_deletes=allow_deletes)
    failures.extend(loop_failures)

    print_summary(plans, allow_deletes=allow_deletes, dry_run=dry_run, failures=failures)
    if failures or any(plan.blocked for plan in plans):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
