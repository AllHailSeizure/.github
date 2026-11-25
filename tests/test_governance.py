"""Tests for the shared governance-separation classifier and automerge skip."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import automerge_gate as gate  # noqa: E402
import governance as gov  # noqa: E402
from check_governance_separation import resolve_paths_and_config  # noqa: E402

DEFAULT = gov.load_default_config()


def _addon(*files: str, prefixes: list[str] | None = None, globs: list[str] | None = None) -> dict:
    return {
        "governance": {
            "files": list(files),
            "prefixes": list(prefixes or []),
            "globs": list(globs or []),
        }
    }


class ClassifierTests(unittest.TestCase):
    def test_default_governance_paths(self) -> None:
        for path in (
            "AGENTS.md",
            "CONTRIBUTING.md",
            "CLAUDE.md",
            "SECURITY.md",
            "CODEOWNERS",
            ".cursor/settings.json",
            ".github/workflows/ci.yml",
            ".github/governance.json",
            ".githooks/pre-commit",
            ".cursor/rules/no-debug-loops.mdc",
            ".cursor/skills/storysteps-to-code/SKILL.md",
            ".codex/environments/environment.toml",
            ".synapse/identity.md",
            ".github",
        ):
            self.assertTrue(gov.is_governance(path, DEFAULT), path)
            self.assertEqual(gov.classify(path, DEFAULT), gov.KIND_GOVERNANCE, path)

    def test_default_nongovernance_paths(self) -> None:
        for path in (
            "README.md",
            "LICENSE",
            "SUPPORT.md",
            "src/main.py",
            "Scripts/Characters/flo.gd",
            "package.json",
            "tools/sync_labels.py",
            "tests/test_governance.py",
            "labels.yml",
            "governance.json",
            ".gitignore",
            ".mcp.json",
            ".cursor/other.json",
            "docs/guide.md",
        ):
            self.assertFalse(gov.is_governance(path, DEFAULT), path)
            self.assertEqual(gov.classify(path, DEFAULT), gov.KIND_OTHER, path)

    def test_windows_separators_normalize(self) -> None:
        self.assertTrue(gov.is_governance(".cursor\\rules\\no-debug-loops.mdc", DEFAULT))
        self.assertFalse(gov.is_governance("src\\main.py", DEFAULT))

    def test_empty_path_is_other(self) -> None:
        self.assertEqual(gov.classify("  ", DEFAULT), gov.KIND_OTHER)
        self.assertEqual(gov.classify("./", DEFAULT), gov.KIND_OTHER)

    def test_leading_dot_slash(self) -> None:
        self.assertTrue(gov.is_governance("./AGENTS.md", DEFAULT))
        self.assertTrue(gov.is_governance("./.github/workflows/ci.yml", DEFAULT))

    def test_glob_match(self) -> None:
        config = gov.merge_configs(DEFAULT, _addon(globs=["Tools/lint/*.py"]))
        self.assertTrue(gov.is_governance("Tools/lint/pr_scope.py", config))
        self.assertFalse(gov.is_governance("Tools/lint/nested/pr_scope.py", config))
        self.assertFalse(gov.is_governance("Tools/storylang/hsl_compile.py", config))


class AddonMergeTests(unittest.TestCase):
    def test_addon_extends_defaults(self) -> None:
        config = gov.merge_configs(
            DEFAULT,
            _addon(
                "Tools/lint/pr_scope.py",
                prefixes=["Tools/lint/"],
            ),
        )
        self.assertTrue(gov.is_governance("AGENTS.md", config))
        self.assertTrue(gov.is_governance("Tools/lint/pr_scope.py", config))
        self.assertTrue(gov.is_governance("Tools/lint/branch_prefix.json", config))
        self.assertFalse(gov.is_governance("Tools/storylang/hsl_compile.py", config))

    def test_addon_union_is_unique_and_ordered(self) -> None:
        config = gov.merge_configs(
            {"governance": {"files": ["AGENTS.md"], "prefixes": [], "globs": []}},
            _addon("AGENTS.md", "Tools/lint/pr_scope.py"),
        )
        self.assertEqual(
            config["governance"]["files"],
            ["AGENTS.md", "Tools/lint/pr_scope.py"],
        )

    def test_missing_addon_leaves_defaults(self) -> None:
        self.assertEqual(gov.merge_configs(DEFAULT, None), gov.normalize_config(DEFAULT))
        self.assertEqual(gov.merge_configs(DEFAULT, {}), gov.normalize_config(DEFAULT))

    def test_malformed_addon_bucket_is_empty(self) -> None:
        merged = gov.merge_configs(DEFAULT, {"not": "governance"})
        self.assertTrue(gov.is_governance("AGENTS.md", merged))
        self.assertFalse(gov.is_governance("src/main.py", merged))


class BaseBranchOnlyTests(unittest.TestCase):
    def test_fetcher_is_called_with_base_ref_only(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def fetcher(repo: str, path: str, ref: str, env: dict[str, str]) -> str | None:
            calls.append((repo, path, ref))
            self.assertNotEqual(ref, "head-sha")
            if ref == "base-sha":
                return json.dumps(_addon("Tools/lint/pr_scope.py"))
            raise AssertionError(f"must not fetch ref {ref!r}")

        config = gov.load_effective_config(
            "AllHailSeizure/hotel-kline-game",
            "base-sha",
            {},
            default=DEFAULT,
            fetcher=fetcher,
        )
        self.assertEqual(
            calls,
            [("AllHailSeizure/hotel-kline-game", ".github/governance.json", "base-sha")],
        )
        self.assertTrue(gov.is_governance("Tools/lint/pr_scope.py", config))
        self.assertTrue(gov.is_governance("AGENTS.md", config))

    def test_head_only_addon_is_not_applied(self) -> None:
        def fetcher(repo: str, path: str, ref: str, env: dict[str, str]) -> str | None:
            if ref == "base-sha":
                return None
            if ref == "head-sha":
                return json.dumps(_addon("src/main.py"))
            raise AssertionError(f"unexpected ref {ref!r}")

        config = gov.load_effective_config(
            "AllHailSeizure/weedeat",
            "base-sha",
            {},
            default=DEFAULT,
            fetcher=fetcher,
        )
        self.assertFalse(gov.is_governance("src/main.py", config))

    def test_empty_ref_does_not_fall_back(self) -> None:
        def fetcher(repo: str, path: str, ref: str, env: dict[str, str]) -> str | None:
            raise AssertionError("must not fetch without a ref")

        with self.assertRaises(gov.AddonError):
            gov.load_effective_config(
                "AllHailSeizure/weedeat",
                "",
                {},
                default=DEFAULT,
                fetcher=fetcher,
            )

    def test_invalid_addon_json_raises(self) -> None:
        def fetcher(repo: str, path: str, ref: str, env: dict[str, str]) -> str | None:
            return "{not json"

        with self.assertRaises(gov.AddonError):
            gov.load_addon_config("AllHailSeizure/weedeat", "base-sha", {}, fetcher=fetcher)

    def test_fetch_repo_file_requires_ref(self) -> None:
        with self.assertRaises(gov.AddonError):
            gov.fetch_repo_file("AllHailSeizure/weedeat", gov.ADDON_PATH, "", {})


class MixEvaluationTests(unittest.TestCase):
    def test_mix_fails_and_lists_both_groups(self) -> None:
        issues = gov.evaluate(["AGENTS.md", "src/main.py"], DEFAULT)
        self.assertTrue(issues)
        joined = "\n".join(issues)
        self.assertIn("AGENTS.md", joined)
        self.assertIn("src/main.py", joined)
        self.assertIn("Governance files (1):", joined)
        self.assertIn("Other files (1):", joined)
        self.assertIn("There is no override", joined)

    def test_governance_only_passes(self) -> None:
        self.assertEqual(
            gov.evaluate(["AGENTS.md", ".github/workflows/ci.yml", "CONTRIBUTING.md"], DEFAULT),
            [],
        )

    def test_nongovernance_only_passes(self) -> None:
        self.assertEqual(gov.evaluate(["src/main.py", "README.md"], DEFAULT), [])

    def test_empty_change_list_passes(self) -> None:
        self.assertEqual(gov.evaluate([], DEFAULT), [])

    def test_addon_can_turn_a_mix_into_governance_only(self) -> None:
        paths = ["AGENTS.md", "Tools/lint/pr_scope.py"]
        self.assertTrue(gov.evaluate(paths, DEFAULT))
        config = gov.merge_configs(DEFAULT, _addon(prefixes=["Tools/lint/"]))
        self.assertEqual(gov.evaluate(paths, config), [])

    def test_mixes_governance_helper(self) -> None:
        self.assertTrue(gov.mixes_governance(["AGENTS.md", "src/main.py"], DEFAULT))
        self.assertFalse(gov.mixes_governance(["AGENTS.md"], DEFAULT))
        self.assertFalse(gov.mixes_governance(["src/main.py"], DEFAULT))

    def test_rename_from_governance_to_other_is_a_mix(self) -> None:
        paths = gov.paths_from_pull_files(
            [
                {
                    "filename": "docs/agents.md",
                    "previous_filename": "AGENTS.md",
                    "status": "renamed",
                }
            ]
        )
        self.assertEqual(paths, ["docs/agents.md", "AGENTS.md"])
        self.assertTrue(gov.mixes_governance(paths, DEFAULT))


class GateSkipTests(unittest.TestCase):
    def _ready(self, **overrides):
        kwargs = dict(
            labels=["automerge"],
            draft=False,
            state="OPEN",
            cross_repository=False,
            head_sha="abc",
            check_runs={
                "test": {
                    "name": "test",
                    "head_sha": "abc",
                    "status": "completed",
                    "conclusion": "success",
                }
            },
            mergeable="MERGEABLE",
            required=gate.required_labels("automerge"),
            required_checks=("test",),
            paths=["src/main.py"],
            config=DEFAULT,
        )
        kwargs.update(overrides)
        return gate.skip_reason(**kwargs)

    def test_unmixed_product_pr_is_ready(self) -> None:
        self.assertIsNone(self._ready(paths=["src/main.py"]))

    def test_unmixed_governance_pr_is_ready(self) -> None:
        self.assertIsNone(self._ready(paths=["AGENTS.md", ".github/workflows/ci.yml"]))

    def test_mixed_pr_is_skipped(self) -> None:
        self.assertEqual(
            self._ready(paths=["AGENTS.md", "src/main.py"]),
            "mixes governance and non-governance",
        )

    def test_gate_uses_the_same_addon_classifier(self) -> None:
        config = gov.merge_configs(DEFAULT, _addon(prefixes=["Tools/lint/"]))
        self.assertIsNone(
            self._ready(paths=["AGENTS.md", "Tools/lint/pr_scope.py"], config=config)
        )
        self.assertEqual(
            self._ready(paths=["AGENTS.md", "Tools/lint/pr_scope.py"], config=DEFAULT),
            "mixes governance and non-governance",
        )


class LocalCliConfigTests(unittest.TestCase):
    def test_local_paths_use_defaults_without_env_pull(self) -> None:
        class Args:
            paths = ["AGENTS.md"]
            addon = None

        paths, config = resolve_paths_and_config(Args(), {})
        self.assertEqual(paths, ["AGENTS.md"])
        self.assertTrue(gov.is_governance("AGENTS.md", config))
        self.assertFalse(gov.is_governance("Tools/lint/pr_scope.py", config))

    def test_local_addon_flag_merges(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            addon_path = Path(tmp) / "governance.json"
            addon_path.write_text(json.dumps(_addon(prefixes=["Tools/lint/"])), encoding="utf-8")

            class Args:
                paths = ["Tools/lint/pr_scope.py"]
                addon = addon_path

            paths, config = resolve_paths_and_config(Args(), {})
            self.assertEqual(paths, ["Tools/lint/pr_scope.py"])
            self.assertTrue(gov.is_governance("Tools/lint/pr_scope.py", config))


if __name__ == "__main__":
    unittest.main()
