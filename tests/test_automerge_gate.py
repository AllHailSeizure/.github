"""Unit tests for tools/automerge_gate.py. No GitHub API calls."""

from __future__ import annotations

import sys
import unittest
from unittest import mock
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

class StrictDetectionTests(unittest.TestCase):
    def test_rules_strict_true(self) -> None:
        rules = [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {"strict_required_status_checks_policy": True},
            },
        ]
        self.assertTrue(gate.strict_from_rules(rules))

    def test_rules_strict_false(self) -> None:
        rules = [
            {
                "type": "required_status_checks",
                "parameters": {"strict_required_status_checks_policy": False},
            }
        ]
        self.assertFalse(gate.strict_from_rules(rules))

    def test_rules_missing_or_garbage(self) -> None:
        self.assertFalse(gate.strict_from_rules(None))
        self.assertFalse(gate.strict_from_rules([{"type": "deletion"}, "x"]))

    def test_protection_strict(self) -> None:
        self.assertTrue(gate.strict_from_protection({"strict": True, "contexts": []}))
        self.assertFalse(gate.strict_from_protection({"strict": False}))
        self.assertFalse(gate.strict_from_protection(None))


class NeedsUpdateTests(unittest.TestCase):
    def test_not_behind_never_updates(self) -> None:
        self.assertFalse(gate.needs_update(behind_by=0, merge_state="BEHIND", strict=True))

    def test_behind_state_updates(self) -> None:
        self.assertTrue(gate.needs_update(behind_by=2, merge_state="BEHIND", strict=False))

    def test_behind_with_strict_updates_when_state_lags(self) -> None:
        self.assertTrue(gate.needs_update(behind_by=1, merge_state="UNKNOWN", strict=True))

    def test_behind_without_strict_merges_as_is(self) -> None:
        self.assertFalse(gate.needs_update(behind_by=3, merge_state="CLEAN", strict=False))


class ErrorClassifierTests(unittest.TestCase):
    def test_not_up_to_date(self) -> None:
        text = "Pull request #90 is not mergeable: the head branch is not up to date with the base branch."
        self.assertTrue(gate.is_not_up_to_date_error(text))
        self.assertFalse(gate.is_not_up_to_date_error("Required status check is expected"))

    def test_conflict(self) -> None:
        self.assertTrue(gate.is_conflict_error("merge conflict between base and head (HTTP 422)"))
        self.assertFalse(gate.is_conflict_error("Resource not accessible by integration"))

    def test_stale_head(self) -> None:
        self.assertTrue(gate.is_stale_head_error("expected head sha didn't match current head ref."))
        self.assertFalse(gate.is_stale_head_error("merge conflict"))


class WorkflowDispatchDetectionTests(unittest.TestCase):
    def test_mapping_form(self) -> None:
        self.assertTrue(gate.workflow_has_dispatch("on:\n  pull_request:\n  workflow_dispatch:\n"))

    def test_list_and_inline_forms(self) -> None:
        self.assertTrue(gate.workflow_has_dispatch("on: [push, workflow_dispatch]\n"))
        self.assertTrue(gate.workflow_has_dispatch("on:\n  - workflow_dispatch\n"))

    def test_comment_and_absent(self) -> None:
        self.assertFalse(gate.workflow_has_dispatch("on:\n  push:\n# workflow_dispatch:\n"))
        self.assertFalse(gate.workflow_has_dispatch("on:\n  pull_request:\n"))


class CommandTests(unittest.TestCase):
    def test_update_branch_is_merge_method_pinned_to_head(self) -> None:
        cmd = gate.update_branch_command("AllHailSeizure/sidereel", 90, "abc")
        self.assertEqual(cmd[:4], ["gh", "api", "-X", "PUT"])
        self.assertIn("repos/AllHailSeizure/sidereel/pulls/90/update-branch", cmd)
        self.assertIn("expected_head_sha=abc", cmd)
        self.assertFalse([part for part in cmd if "rebase" in part])

    def test_dispatch_targets_workflow_and_branch(self) -> None:
        cmd = gate.dispatch_command("AllHailSeizure/sidereel", "ci.yml", "feat/x")
        self.assertIn("repos/AllHailSeizure/sidereel/actions/workflows/ci.yml/dispatches", cmd)
        self.assertIn("ref=feat/x", cmd)

    def test_notice_marker_is_per_kind_and_sha(self) -> None:
        self.assertNotEqual(gate.notice_marker("conflict", "a"), gate.notice_marker("conflict", "b"))
        self.assertIn(gate.notice_marker("conflict", "a"), gate.notice_body("conflict", "a", "text"))


class DispatchRunTriggerTests(unittest.TestCase):
    def _event(self, event: str, pulls: list[dict]) -> dict:
        return {
            "repository": {"full_name": "AllHailSeizure/sidereel"},
            "workflow_run": {
                "event": event,
                "conclusion": "success",
                "head_repository": {"full_name": "AllHailSeizure/sidereel"},
                "head_sha": "newsha",
                "pull_requests": pulls,
            },
        }

    def test_dispatch_run_resolves_by_sha(self) -> None:
        numbers, sha = gate.trigger_from_event("workflow_run", self._event("workflow_dispatch", []))
        self.assertEqual(numbers, [])
        self.assertEqual(sha, "newsha")

    def test_push_run_still_ignored(self) -> None:
        self.assertEqual(
            gate.trigger_from_event("workflow_run", self._event("push", [])), ([], "")
        )


class UpdateAndDispatchTests(unittest.TestCase):
    """Drive update_and_dispatch with a fake gh. No network."""

    REPO = "AllHailSeizure/sidereel"

    def _run_flow(self, *, dispatch_ok=True, workflow_text="on:\n  workflow_dispatch:\n",
                  update_rc=0, update_err="", heads=("new",)):
        calls: list[list[str]] = []
        head_iter = iter(heads)

        def fake_run(cmd, env):
            calls.append(cmd)
            joined = " ".join(cmd)
            result = mock.Mock(returncode=0, stdout="", stderr="")
            if "/contents/" in joined:
                result.stdout = workflow_text
            elif "update-branch" in joined:
                result.returncode, result.stderr = update_rc, update_err
            elif cmd[:3] == ["gh", "pr", "view"]:
                result.stdout = next(head_iter, heads[-1]) + "\n"
            elif "dispatches" in joined:
                result.returncode = 0 if dispatch_ok else 1
            elif "comments" in joined:
                result.stdout = ""
            return result

        with mock.patch.object(gate, "_run", fake_run), mock.patch.object(
            gate, "UPDATE_WAIT_SECONDS", 0.05
        ), mock.patch.object(gate, "UPDATE_POLL_SECONDS", 0.01):
            outcome = gate.update_and_dispatch(90, "old", "feat/x", "master", self.REPO, {})
        self.outcome = outcome
        return outcome.ok, calls

    @staticmethod
    def _has(calls, needle):
        return any(needle in " ".join(c) for c in calls)

    def test_updates_then_dispatches(self) -> None:
        ok, calls = self._run_flow()
        self.assertTrue(ok)
        self.assertEqual(self.outcome.updated_sha, "new")
        self.assertTrue(self._has(calls, "update-branch"))
        self.assertTrue(self._has(calls, "workflows/ci.yml/dispatches"))
        names = [" ".join(c) for c in calls]
        self.assertLess(
            next(i for i, n in enumerate(names) if "update-branch" in n),
            next(i for i, n in enumerate(names) if "dispatches" in n),
        )

    def test_no_dispatch_trigger_means_no_update(self) -> None:
        ok, calls = self._run_flow(workflow_text="on:\n  pull_request:\n")
        self.assertTrue(ok)
        self.assertFalse(self._has(calls, "update-branch"))
        self.assertTrue(self._has(calls, "issues/90/comments") or self._has(calls, "pr comment"))

    def test_conflict_is_not_a_failure_and_not_dispatched(self) -> None:
        ok, calls = self._run_flow(update_rc=1, update_err="merge conflict between base and head (HTTP 422)")
        self.assertTrue(ok)
        self.assertEqual(self.outcome.updated_sha, "")
        self.assertFalse(self._has(calls, "dispatches"))

    def test_stale_head_is_benign(self) -> None:
        ok, calls = self._run_flow(update_rc=1, update_err="expected head sha didn't match current head ref. (HTTP 422)")
        self.assertTrue(ok)
        self.assertFalse(self._has(calls, "dispatches"))

    def test_head_that_never_moves_fails(self) -> None:
        ok, calls = self._run_flow(heads=("old",))
        self.assertFalse(ok)
        self.assertFalse(self._has(calls, "dispatches"))

    def test_dispatch_failure_fails_the_run(self) -> None:
        ok, _ = self._run_flow(dispatch_ok=False)
        self.assertFalse(ok)


class ChecksFinishedTests(unittest.TestCase):
    NAMES = ("test", "lint")

    def test_all_completed_on_sha(self) -> None:
        self.assertTrue(gate.checks_finished(_green("abc"), self.NAMES, "abc"))

    def test_failed_still_counts_as_finished(self) -> None:
        runs = _green("abc")
        runs["lint"] = _check("lint", head_sha="abc", conclusion="failure")
        self.assertTrue(gate.checks_finished(runs, self.NAMES, "abc"))

    def test_in_progress_is_not_finished(self) -> None:
        runs = _green("abc")
        runs["test"] = _check("test", head_sha="abc", status="in_progress", conclusion="")
        self.assertFalse(gate.checks_finished(runs, self.NAMES, "abc"))

    def test_missing_or_other_sha_is_not_finished(self) -> None:
        self.assertFalse(gate.checks_finished({"test": _check("test", head_sha="abc")}, self.NAMES, "abc"))
        self.assertFalse(gate.checks_finished(_green("old"), self.NAMES, "new"))

    def test_no_required_checks_never_finishes(self) -> None:
        self.assertFalse(gate.checks_finished({}, (), "abc"))


class ProcessQueueTests(unittest.TestCase):
    """process() with consider/wait_for_ci faked: update, wait, merge, next PR."""

    def _drive(self, script: dict[int, list], following=None, wait_ok=True):
        calls: list[tuple] = []
        scripts = {n: list(items) for n, items in script.items()}

        def fake_consider(number, env, *, allow_merge=True):
            calls.append(("consider", number, allow_merge))
            return scripts[number].pop(0)

        def fake_wait(number, sha, deadline, env):
            calls.append(("wait", number, sha))
            return wait_ok

        pending = [following]

        def fake_next(merged, branch, env):
            calls.append(("next", merged))
            return pending.pop(0) if pending else None

        with mock.patch.object(gate, "consider", fake_consider), mock.patch.object(
            gate, "wait_for_ci", fake_wait
        ), mock.patch.object(gate, "next_behind", fake_next), mock.patch.object(
            gate, "_pull_request", lambda n, e: {"baseRefName": "master"}
        ):
            ok = gate.process(90, {})
        return ok, calls

    def test_merge_ready_pr_then_nothing_behind(self) -> None:
        ok, calls = self._drive({90: [gate.Outcome(True, merged=True)]})
        self.assertTrue(ok)
        self.assertEqual(calls, [("consider", 90, True), ("next", 90)])

    def test_behind_pr_is_updated_waited_for_then_merged(self) -> None:
        ok, calls = self._drive(
            {90: [gate.Outcome(True, updated_sha="new"), gate.Outcome(True, merged=True)]}
        )
        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [("consider", 90, True), ("wait", 90, "new"), ("consider", 90, True), ("next", 90)],
        )

    def test_failed_ci_leaves_the_pr_alone(self) -> None:
        ok, calls = self._drive(
            {90: [gate.Outcome(True, updated_sha="new"), gate.Outcome(True)]}
        )
        self.assertTrue(ok)
        self.assertNotIn(("next", 90), calls)

    def test_wait_timeout_stops_quietly(self) -> None:
        ok, calls = self._drive({90: [gate.Outcome(True, updated_sha="new")]}, wait_ok=False)
        self.assertTrue(ok)
        self.assertEqual(calls, [("consider", 90, True), ("wait", 90, "new")])

    def test_next_pr_is_carried_through_after_a_merge(self) -> None:
        ok, calls = self._drive(
            {
                90: [gate.Outcome(True, merged=True)],
                91: [gate.Outcome(True, merged=True)],
            },
            following=(91, gate.Outcome(True, updated_sha="n91")),
        )
        self.assertTrue(ok)
        self.assertIn(("wait", 91, "n91"), calls)
        self.assertIn(("consider", 91, True), calls)

    def test_failure_fails_the_run(self) -> None:
        ok, _ = self._drive({90: [gate.Outcome(False)]})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
