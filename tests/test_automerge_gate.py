"""Unit tests for tools/automerge_gate.py. No GitHub API calls."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import automerge_gate as gate  # noqa: E402


def _check(
    name: str,
    *,
    head_sha: str = "abc",
    status: str = "completed",
    conclusion: str = "success",
    started_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
    }


def _green(head_sha: str = "abc", names: tuple[str, ...] = ("test", "lint")) -> dict[str, dict]:
    return {name: _check(name, head_sha=head_sha) for name in names}


def _ready(**overrides) -> str | None:
    kwargs = dict(
        labels=["automerge"],
        draft=False,
        state="OPEN",
        cross_repository=False,
        head_sha="abc",
        check_runs=_green(),
        mergeable="MERGEABLE",
        required=gate.required_labels("automerge"),
        required_checks=("test", "lint"),
    )
    kwargs.update(overrides)
    return gate.skip_reason(**kwargs)


class RequiredLabelTests(unittest.TestCase):
    def test_default_is_automerge(self) -> None:
        self.assertEqual(gate.required_labels(), frozenset({"automerge"}))

    def test_empty_raw_is_empty(self) -> None:
        self.assertEqual(gate.required_labels(""), frozenset())


class CollectChecksTests(unittest.TestCase):
    def test_union_preserves_order_and_uniques(self) -> None:
        self.assertEqual(
            gate.collect_required_checks(
                discovered=("Lint", "test"),
                configured=("test", "scope"),
            ),
            ("Lint", "test", "scope"),
        )

    def test_empty(self) -> None:
        self.assertEqual(gate.collect_required_checks(), ())

    def test_configured_from_env_string(self) -> None:
        self.assertEqual(
            gate.configured_check_names("Lint, Tests (ubuntu-latest, Python 3.14), Lint"),
            ("Lint", "Tests (ubuntu-latest, Python 3.14)"),
        )

    def test_configured_semicolon_separator(self) -> None:
        self.assertEqual(
            gate.configured_check_names("Lint; Tests (ubuntu-latest, Python 3.14)"),
            ("Lint", "Tests (ubuntu-latest, Python 3.14)"),
        )


class DiscoveryParseTests(unittest.TestCase):
    def test_classic_protection(self) -> None:
        payload = {
            "strict": True,
            "contexts": ["test", "lint"],
            "checks": [{"context": "test", "app_id": 1}, {"context": "scope", "app_id": 1}],
        }
        self.assertEqual(
            gate.required_names_from_protection(payload),
            ("test", "scope", "lint"),
        )

    def test_ruleset_rules(self) -> None:
        payload = [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "Lint", "integration_id": 15368},
                        {"context": "Tests (ubuntu-latest, Python 3.14)"},
                    ]
                },
            },
        ]
        self.assertEqual(
            gate.required_names_from_rules(payload),
            ("Lint", "Tests (ubuntu-latest, Python 3.14)"),
        )

    def test_rollup_only_required(self) -> None:
        contexts = [
            {"name": "test", "isRequired": True},
            {"name": "optional", "isRequired": False},
            {"context": "lint", "isRequired": True},
            {"name": "skipped-required", "isRequired": False},
        ]
        self.assertEqual(gate.required_names_from_rollup(contexts), ("test", "lint"))


class LatestCheckRunTests(unittest.TestCase):
    def test_ignores_cancelled_and_skipped_keeps_newest(self) -> None:
        runs = [
            _check("lint", conclusion="cancelled", started_at="2026-01-01T00:00:02Z"),
            _check("lint", conclusion="success", started_at="2026-01-01T00:00:01Z"),
            _check("test", conclusion="skipped", started_at="2026-01-01T00:00:03Z"),
            _check("test", conclusion="success", started_at="2026-01-01T00:00:00Z"),
        ]
        latest = gate.latest_check_runs_by_name(runs)
        self.assertEqual(latest["lint"]["conclusion"], "success")
        self.assertEqual(latest["test"]["conclusion"], "success")


class SkipReasonTests(unittest.TestCase):
    def test_ready(self) -> None:
        self.assertIsNone(_ready())

    def test_missing_label(self) -> None:
        self.assertEqual(_ready(labels=[]), "missing labels: automerge")

    def test_empty_labels_configured(self) -> None:
        self.assertEqual(_ready(required=frozenset()), "no automerge labels configured")

    def test_no_required_checks(self) -> None:
        self.assertEqual(_ready(required_checks=()), "no required checks configured")

    def test_missing_check(self) -> None:
        self.assertEqual(
            _ready(check_runs=_green(names=("test",))),
            "required checks missing: lint",
        )

    def test_not_green(self) -> None:
        runs = _green()
        runs["lint"] = _check("lint", conclusion="failure")
        self.assertEqual(_ready(check_runs=runs), "required checks not green: lint")

    def test_wrong_sha(self) -> None:
        runs = _green(head_sha="other")
        self.assertEqual(_ready(check_runs=runs), "CI run does not match the pull request head")

    def test_empty_head_sha(self) -> None:
        self.assertEqual(_ready(head_sha=""), "CI run does not match the pull request head")

    def test_draft(self) -> None:
        self.assertEqual(_ready(draft=True), "pull request is a draft")

    def test_fork(self) -> None:
        self.assertEqual(_ready(cross_repository=True), "pull request is from a fork")

    def test_closed(self) -> None:
        self.assertEqual(_ready(state="MERGED"), "pull request is not open")

    def test_conflict(self) -> None:
        self.assertEqual(_ready(mergeable="CONFLICTING"), "merge conflict")


class MergeCommandTests(unittest.TestCase):
    def test_squash_matches_head_and_never_bypasses(self) -> None:
        cmd = gate.merge_command(12, "abc", "AllHailSeizure/sidereel")
        self.assertIn("--squash", cmd)
        self.assertIn("--match-head-commit", cmd)
        self.assertIn("abc", cmd)
        self.assertNotIn("--admin", cmd)
        self.assertNotIn("--rebase", cmd)
        self.assertNotIn("--merge", cmd)
        self.assertEqual(cmd[cmd.index("-R") + 1], "AllHailSeizure/sidereel")


class CandidateTests(unittest.TestCase):
    def test_env_pull_number(self) -> None:
        numbers, sha = gate.candidate_numbers_from_env({"AUTOMERGE_PULL_NUMBER": "12"})
        self.assertEqual(numbers, [12])
        self.assertEqual(sha, "")

    def test_env_zero_falls_through_to_sha(self) -> None:
        numbers, sha = gate.candidate_numbers_from_env(
            {"AUTOMERGE_PULL_NUMBER": "0", "AUTOMERGE_HEAD_SHA": "def"}
        )
        self.assertEqual(numbers, [])
        self.assertEqual(sha, "def")

    def test_fork_event_is_ignored(self) -> None:
        numbers, sha = gate.trigger_from_event(
            "pull_request_target",
            {
                "pull_request": {
                    "number": 3,
                    "head": {"repo": {"full_name": "someone/fork"}},
                    "base": {"repo": {"full_name": "AllHailSeizure/weedeat"}},
                }
            },
        )
        self.assertEqual(numbers, [])
        self.assertEqual(sha, "")

    def test_same_repo_label_event(self) -> None:
        numbers, sha = gate.trigger_from_event(
            "pull_request_target",
            {
                "pull_request": {
                    "number": 3,
                    "head": {"repo": {"full_name": "AllHailSeizure/weedeat"}},
                    "base": {"repo": {"full_name": "AllHailSeizure/weedeat"}},
                }
            },
        )
        self.assertEqual(numbers, [3])
        self.assertEqual(sha, "")

    def test_failed_workflow_run_ignored(self) -> None:
        numbers, sha = gate.trigger_from_event(
            "workflow_run",
            {
                "repository": {"full_name": "AllHailSeizure/weedeat"},
                "workflow_run": {
                    "event": "pull_request",
                    "conclusion": "failure",
                    "head_repository": {"full_name": "AllHailSeizure/weedeat"},
                    "head_sha": "abc",
                    "pull_requests": [{"number": 4}],
                },
            },
        )
        self.assertEqual(numbers, [])
        self.assertEqual(sha, "")

    def test_successful_workflow_run(self) -> None:
        numbers, sha = gate.trigger_from_event(
            "workflow_run",
            {
                "repository": {"full_name": "AllHailSeizure/weedeat"},
                "workflow_run": {
                    "event": "pull_request",
                    "conclusion": "success",
                    "head_repository": {"full_name": "AllHailSeizure/weedeat"},
                    "head_sha": "abc",
                    "pull_requests": [{"number": 4}],
                },
            },
        )
        self.assertEqual(numbers, [4])
        self.assertEqual(sha, "")



class LinkedIssueTests(unittest.TestCase):
    def test_same_repo_keeps_order_and_uniques(self) -> None:
        refs = [
            {
                "number": 7,
                "repository": {"name": "sidereel", "owner": {"login": "AllHailSeizure"}},
            },
            {
                "number": 7,
                "repository": {"name": "sidereel", "owner": {"login": "AllHailSeizure"}},
            },
            {
                "number": 9,
                "repository": {"name": "sidereel", "owner": {"login": "AllHailSeizure"}},
            },
        ]
        self.assertEqual(
            gate.same_repo_linked_issue_numbers(refs, "AllHailSeizure/sidereel"),
            (7, 9),
        )

    def test_skips_other_repositories(self) -> None:
        refs = [
            {
                "number": 7,
                "repository": {"name": "sidereel", "owner": {"login": "AllHailSeizure"}},
            },
            {
                "number": 1,
                "repository": {"name": "hotel-kline-game", "owner": {"login": "AllHailSeizure"}},
            },
        ]
        self.assertEqual(
            gate.same_repo_linked_issue_numbers(refs, "AllHailSeizure/sidereel"),
            (7,),
        )

    def test_missing_repository_counts_as_same_repo(self) -> None:
        self.assertEqual(
            gate.same_repo_linked_issue_numbers([{"number": 4}], "AllHailSeizure/sidereel"),
            (4,),
        )

    def test_empty_or_invalid(self) -> None:
        self.assertEqual(gate.same_repo_linked_issue_numbers(None, "AllHailSeizure/sidereel"), ())
        self.assertEqual(gate.same_repo_linked_issue_numbers([], "AllHailSeizure/sidereel"), ())
        self.assertEqual(gate.same_repo_linked_issue_numbers([{"number": 4}], ""), ())

    def test_close_command_targets_repo_and_comments(self) -> None:
        cmd = gate.close_issue_command(7, "AllHailSeizure/sidereel", "Closed automatically after automerge of #64.")
        self.assertEqual(cmd[cmd.index("-R") + 1], "AllHailSeizure/sidereel")
        self.assertIn("--comment", cmd)
        self.assertEqual(cmd[cmd.index("--comment") + 1], "Closed automatically after automerge of #64.")
        self.assertEqual(cmd[:4], ["gh", "issue", "close", "7"])

    def test_comment_names_the_pull_request(self) -> None:
        self.assertEqual(
            gate.linked_issue_comment(64),
            "Closed automatically after automerge of #64.",
        )

    def test_merge_command_does_not_override_subject_or_body(self) -> None:
        cmd = gate.merge_command(64, "abc", "AllHailSeizure/sidereel")
        self.assertNotIn("--subject", cmd)
        self.assertNotIn("--body", cmd)
        self.assertNotIn("--admin", cmd)

if __name__ == "__main__":
    unittest.main()
