#!/usr/bin/env python3
"""Fail a PR that mixes governance (how the repo operates) with other changes.

In CI, pass GOVERNANCE_PULL_NUMBER and GITHUB_REPOSITORY. The script reads
changed paths from the GitHub API and loads .github/governance.json from the
PR's base ref only. Locally, pass paths as arguments or on stdin; only the
shared defaults are used unless --addon is given.

Never execute code from the pull request.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from governance import (
    AddonError,
    evaluate,
    load_default_config,
    load_effective_config,
    list_pr_paths,
    merge_configs,
    normalize_config,
)


def _pr_base_ref(repo: str, number: int, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", "baseRefOid"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AddonError(
            f"failed to read base ref for {repo}#{number}: {(result.stderr or result.stdout).strip()}"
        )
    payload = json.loads(result.stdout)
    ref = str((payload or {}).get("baseRefOid") or "")
    if not ref:
        raise AddonError(f"pull request {repo}#{number} has no baseRefOid")
    return ref


def _load_addon_file(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return normalize_config(json.load(handle))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Changed paths (default: stdin, or the PR file list when GOVERNANCE_PULL_NUMBER is set)",
    )
    parser.add_argument(
        "--addon",
        type=Path,
        help="Local add-on JSON to merge with the defaults (for offline checks only)",
    )
    return parser


def resolve_paths_and_config(args: argparse.Namespace, env: dict[str, str]) -> tuple[list[str], dict]:
    raw_number = (env.get("GOVERNANCE_PULL_NUMBER") or "").strip()
    if raw_number and raw_number not in {"0", "0.0"} and not args.paths and args.addon is None:
        number = int(float(raw_number))
        repo = env.get("GITHUB_REPOSITORY", "")
        if not repo:
            raise AddonError("GITHUB_REPOSITORY is required when GOVERNANCE_PULL_NUMBER is set")
        base_ref = _pr_base_ref(repo, number, env)
        config = load_effective_config(repo, base_ref, env)
        return list_pr_paths(repo, number, env), config

    paths = list(args.paths)
    if not paths:
        paths = [line for line in sys.stdin.read().splitlines() if line.strip()]
    config = load_default_config()
    if args.addon is not None:
        config = merge_configs(config, _load_addon_file(args.addon))
    return paths, config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        paths, config = resolve_paths_and_config(args, os.environ.copy())
    except AddonError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    issues = evaluate(paths, config)
    if issues:
        print("\n".join(issues))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
