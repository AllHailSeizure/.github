#!/usr/bin/env python3
"""Classify PR paths as governance (repo-operating rules) or not.

The default path list is governance.json in this repository. A calling repo
may extend it with .github/governance.json, which is always read from the
PR's base ref, never from the PR head.
"""

from __future__ import annotations

import base64
import json
import subprocess
import urllib.parse
from pathlib import Path
from typing import Callable

KIND_GOVERNANCE = "governance"
KIND_OTHER = "other"

ADDON_PATH = ".github/governance.json"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "governance.json"

Fetcher = Callable[[str, str, str, dict[str, str]], str | None]


def normalize(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _glob_match(path: str, pattern: str) -> bool:
    """Match a glob whose * does not cross '/'."""
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")
    if len(path_parts) != len(pattern_parts):
        return False
    for part, expected in zip(path_parts, pattern_parts):
        if expected == "*":
            continue
        if "*" in expected or "?" in expected or "[" in expected:
            import fnmatch

            if not fnmatch.fnmatch(part, expected):
                return False
        elif part != expected:
            return False
    return True


def empty_bucket() -> dict[str, list[str]]:
    return {"files": [], "prefixes": [], "globs": []}


def normalize_config(data: dict | None) -> dict:
    bucket = (data or {}).get("governance") if isinstance(data, dict) else None
    if not isinstance(bucket, dict):
        bucket = {}
    return {
        "governance": {
            "files": [str(item) for item in (bucket.get("files") or []) if item],
            "prefixes": [str(item) for item in (bucket.get("prefixes") or []) if item],
            "globs": [str(item) for item in (bucket.get("globs") or []) if item],
        }
    }


def _union(left: list[str], right: list[str]) -> list[str]:
    seen: list[str] = []
    for item in list(left) + list(right):
        if item not in seen:
            seen.append(item)
    return seen


def merge_configs(default: dict, addon: dict | None) -> dict:
    """Extend the default governance lists with a per-repo add-on."""
    base = normalize_config(default)
    extra = normalize_config(addon)
    return {
        "governance": {
            "files": _union(base["governance"]["files"], extra["governance"]["files"]),
            "prefixes": _union(base["governance"]["prefixes"], extra["governance"]["prefixes"]),
            "globs": _union(base["governance"]["globs"], extra["governance"]["globs"]),
        }
    }


def load_default_config(path: Path | None = None) -> dict:
    config_path = path if path is not None else DEFAULT_CONFIG_PATH
    with config_path.open(encoding="utf-8") as handle:
        return normalize_config(json.load(handle))


def _matches_governance(normalized: str, bucket: dict) -> bool:
    if normalized in bucket.get("files", []):
        return True
    for prefix in bucket.get("prefixes", []):
        directory = prefix.rstrip("/")
        if normalized == directory or normalized.startswith(prefix):
            return True
    for pattern in bucket.get("globs", []):
        if _glob_match(normalized, pattern):
            return True
    return False


def classify(path: str, config: dict | None = None) -> str:
    """Return 'governance' or 'other'."""
    normalized = normalize(path)
    if not normalized:
        return KIND_OTHER
    data = config if config is not None else load_default_config()
    if _matches_governance(normalized, data["governance"]):
        return KIND_GOVERNANCE
    return KIND_OTHER


def is_governance(path: str, config: dict | None = None) -> bool:
    return classify(path, config) == KIND_GOVERNANCE


def mixes_governance(paths: list[str], config: dict | None = None) -> bool:
    """True when a change list includes both governance and non-governance paths."""
    kinds = {classify(path, config) for path in paths if path.strip()}
    return KIND_GOVERNANCE in kinds and KIND_OTHER in kinds


def partition(paths: list[str], config: dict | None = None) -> tuple[list[str], list[str]]:
    normalized = [normalize(path) for path in paths if path.strip()]
    governance = sorted({path for path in normalized if is_governance(path, config)})
    other = sorted({path for path in normalized if path and not is_governance(path, config)})
    return governance, other


def mix_message(governance: list[str], other: list[str]) -> list[str]:
    lines = [
        "This PR mixes governance paths (how the repo operates) with other changes.",
        "",
        "Governance files ({}):".format(len(governance)),
    ]
    lines.extend("  {}".format(path) for path in governance)
    lines.append("")
    lines.append("Other files ({}):".format(len(other)))
    lines.extend("  {}".format(path) for path in other)
    lines.extend(
        [
            "",
            "A pull request that touches governance may touch only governance.",
            "Split them. There is no override.",
        ]
    )
    return lines


def evaluate(paths: list[str], config: dict | None = None) -> list[str]:
    """Return failure messages. Empty means the change is acceptable."""
    governance, other = partition(paths, config)
    if governance and other:
        return mix_message(governance, other)
    return []


class AddonError(Exception):
    """The base-branch add-on exists but could not be used."""


def fetch_repo_file(repo: str, path: str, ref: str, env: dict[str, str]) -> str | None:
    """Return a file's text from an explicit git ref, or None if it is missing.

    `ref` is required. This never falls back to the repo default branch or to
    a PR head, so a pull request cannot redefine governance to slip through.
    """
    if not repo or not path or not ref:
        raise AddonError("repo, path, and ref are required to read a governance add-on")
    quoted = urllib.parse.quote(path, safe="/")
    url = f"repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref, safe='')}"
    result = subprocess.run(
        ["gh", "api", url],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").lower()
        if "404" in detail or "not found" in detail:
            return None
        raise AddonError(f"failed to read {path} at {ref}: {(result.stderr or result.stdout).strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AddonError(f"invalid contents payload for {path} at {ref}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "file":
        raise AddonError(f"{path} at {ref} is not a file")
    content = payload.get("content") or ""
    encoding = payload.get("encoding") or "base64"
    if encoding == "base64":
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AddonError(f"could not decode {path} at {ref}: {exc}") from exc
    return str(content)


def load_addon_config(
    repo: str,
    base_ref: str,
    env: dict[str, str],
    fetcher: Fetcher | None = None,
) -> dict | None:
    """Load .github/governance.json from the PR base ref only."""
    if not repo or not base_ref:
        raise AddonError("repo and base ref are required to read a governance add-on")
    reader = fetcher or fetch_repo_file
    raw = reader(repo, ADDON_PATH, base_ref, env)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AddonError(
            f"invalid JSON in {ADDON_PATH} on the base branch ({base_ref}): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise AddonError(f"{ADDON_PATH} on the base branch ({base_ref}) must be a JSON object")
    return normalize_config(data)


def load_effective_config(
    repo: str,
    base_ref: str,
    env: dict[str, str],
    *,
    default: dict | None = None,
    fetcher: Fetcher | None = None,
) -> dict:
    """Defaults plus the base-branch add-on. Never reads the PR head."""
    defaults = default if default is not None else load_default_config()
    addon = load_addon_config(repo, base_ref, env, fetcher=fetcher)
    return merge_configs(defaults, addon)


def paths_from_pull_files(payload: list[object]) -> list[str]:
    """Collect current and previous paths from the pull-files API."""
    paths: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        for key in ("filename", "previous_filename"):
            name = normalize(str(item.get(key) or ""))
            if name and name not in seen:
                seen.add(name)
                paths.append(name)
    return paths


def list_pr_paths(repo: str, number: int, env: dict[str, str]) -> list[str]:
    if not repo or not number:
        return []
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{number}/files?per_page=100",
            "--jq",
            ".[]",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AddonError(
            f"failed to list files for {repo}#{number}: {(result.stderr or result.stdout).strip()}"
        )
    rows: list[object] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return paths_from_pull_files(rows)
