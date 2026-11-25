"""Tests for account-wide label sync: discovery, modes, redirects, isolation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sync_labels as sl  # noqa: E402


def _repo(name: str, *, fork: bool = False, archived: bool = False, owner: str = "AllHailSeizure") -> dict:
    return {
        "name": name,
        "fork": fork,
        "archived": archived,
        "owner": {"login": owner},
    }


class DiscoverOwnedReposTests(unittest.TestCase):
    def test_excludes_forks_archived_and_dot_github(self) -> None:
        payload = [
            _repo("star-system"),
            _repo("Nestor", fork=True),
            _repo("willow-2.0", fork=True),
            _repo(".github"),
            _repo("old-lab", archived=True),
            _repo("hotel-kline-game"),
        ]
        self.assertEqual(
            sl.discover_owned_repos("AllHailSeizure", payload),
            ["star-system", "hotel-kline-game"],
        )

    def test_uses_current_name_from_payload(self) -> None:
        self.assertEqual(
            sl.discover_owned_repos("AllHailSeizure", [_repo("star-system")]),
            ["star-system"],
        )
        self.assertNotIn("science_bitch", sl.discover_owned_repos("AllHailSeizure", [_repo("star-system")]))

    def test_skips_other_owners(self) -> None:
        self.assertEqual(
            sl.discover_owned_repos("AllHailSeizure", [_repo("other", owner="someone-else")]),
            [],
        )

    def test_flattens_paginated_arrays(self) -> None:
        pages = [[_repo("cortex")], [_repo("weedeat")]]
        self.assertEqual(sl.discover_owned_repos("AllHailSeizure", pages), ["cortex", "weedeat"])


class SelectTargetsTests(unittest.TestCase):
    def test_filter_is_optional(self) -> None:
        targets, warnings, unknown = sl.select_targets(
            ["star-system", "synapse"],
            ["hotel-kline-game"],
            None,
        )
        self.assertEqual(targets, ["star-system", "synapse"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("hotel-kline-game", warnings[0])
        self.assertEqual(unknown, [])

    def test_extras_for_missing_repo_are_a_warning_not_a_failure(self) -> None:
        targets, warnings, unknown = sl.select_targets(
            ["synapse"],
            ["missing-repo"],
            None,
        )
        self.assertEqual(targets, ["synapse"])
        self.assertTrue(warnings)
        self.assertEqual(unknown, [])

    def test_repos_flag_filters_discovered_list(self) -> None:
        targets, warnings, unknown = sl.select_targets(
            ["star-system", "synapse", "cortex"],
            [],
            ["synapse", "cortex"],
        )
        self.assertEqual(targets, ["synapse", "cortex"])
        self.assertEqual(warnings, [])
        self.assertEqual(unknown, [])

    def test_unknown_filter_is_reported(self) -> None:
        targets, _warnings, unknown = sl.select_targets(
            ["synapse"],
            [],
            ["synapse", "science_bitch"],
        )
        self.assertEqual(targets, ["synapse"])
        self.assertEqual(unknown, ["science_bitch"])


class ResolveModesTests(unittest.TestCase):
    def test_schedule_applies_and_never_deletes(self) -> None:
        self.assertEqual(sl.resolve_modes("schedule", True, True), (False, False))
        self.assertEqual(sl.resolve_modes("schedule", True, False), (False, False))
        self.assertEqual(sl.resolve_modes("schedule", False, True), (False, False))

    def test_workflow_dispatch_keeps_flags(self) -> None:
        self.assertEqual(sl.resolve_modes("workflow_dispatch", True, False), (True, False))
        self.assertEqual(sl.resolve_modes("workflow_dispatch", False, True), (False, True))
        self.assertEqual(sl.resolve_modes("workflow_dispatch", False, False), (False, False))

    def test_local_or_missing_event_keeps_flags(self) -> None:
        self.assertEqual(sl.resolve_modes(None, True, False), (True, False))
        self.assertEqual(sl.resolve_modes("", False, True), (False, True))


class RedirectParsingTests(unittest.TestCase):
    def test_location_header_and_json_url(self) -> None:
        self.assertEqual(
            sl.redirect_target("", "Location: https://api.github.com/repositories/1431660617/labels/documentation\n"),
            "repositories/1431660617/labels/documentation",
        )
        self.assertEqual(
            sl.redirect_target(
                json.dumps({"url": "https://api.github.com/repos/AllHailSeizure/star-system/labels/documentation"}),
                "",
            ),
            "repos/AllHailSeizure/star-system/labels/documentation",
        )

    def test_parse_http_status(self) -> None:
        self.assertEqual(sl.parse_http_status("HTTP/2.0 307 Temporary Redirect"), 307)
        self.assertEqual(sl.parse_http_status("gh: HTTP 307"), 307)
        self.assertEqual(sl.parse_http_status("gh: Not Found (HTTP 404)"), 404)

    def test_repo_from_api_path(self) -> None:
        self.assertEqual(
            sl.repo_from_api_path("repos/AllHailSeizure/science_bitch/labels/documentation"),
            "science_bitch",
        )


class GhApiRedirectTests(unittest.TestCase):
    def test_follows_307_and_retries(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, body=None):
            calls.append(cmd)
            path = cmd[-1] if "--input" not in cmd else cmd[cmd.index("--input") - 1]
            if "science_bitch" in path:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr=(
                        "HTTP/2.0 307 Temporary Redirect\n"
                        "Location: https://api.github.com/repos/AllHailSeizure/star-system/labels/documentation\n"
                    ),
                )
            return SimpleNamespace(returncode=0, stdout='{"name":"documentation"}', stderr="")

        with patch.object(sl, "_run_gh", side_effect=fake_run):
            data = sl.gh_api(
                "PATCH",
                "repos/AllHailSeizure/science_bitch/labels/documentation",
                {"new_name": "docs"},
                repo="science_bitch",
            )
        self.assertEqual(data, {"name": "documentation"})
        self.assertEqual(len(calls), 2)
        self.assertIn("science_bitch", calls[0][-3] if "--input" in calls[0] else calls[0][-1])
        self.assertIn("star-system", "".join(calls[1]))

    def test_unfollowed_redirect_names_the_repo(self) -> None:
        def fake_run(cmd, body=None):
            return SimpleNamespace(returncode=1, stdout="", stderr="gh: HTTP 307 Temporary Redirect")

        with patch.object(sl, "_run_gh", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as raised:
                sl.gh_api(
                    "PATCH",
                    "repos/AllHailSeizure/science_bitch/labels/documentation",
                    {"new_name": "docs"},
                    repo="science_bitch",
                )
        message = str(raised.exception)
        self.assertIn("science_bitch", message)
        self.assertIn("307", message)
        self.assertIn("renamed", message)

    def test_ordinary_error_names_the_repo(self) -> None:
        def fake_run(cmd, body=None):
            return SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")

        with patch.object(sl, "_run_gh", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as raised:
                sl.gh_api("GET", "repos/AllHailSeizure/synapse/labels", repo="synapse")
        self.assertIn("synapse", str(raised.exception))
        self.assertIn("404", str(raised.exception))


class LoadSchemeTests(unittest.TestCase):
    def test_repos_section_is_optional(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
            handle.write(
                "owner: AllHailSeizure\n"
                "shared:\n"
                "  - name: bug\n"
                "    color: A2EEEF\n"
                "    description: Something isn't working.\n"
            )
            path = Path(handle.name)
        try:
            scheme = sl.load_scheme(path)
        finally:
            path.unlink()
        self.assertEqual(scheme["repos"], {})
        self.assertEqual(list(sl.desired_for(scheme, "star-system")), ["bug"])

    def test_real_scheme_has_no_science_bitch_and_only_extras(self) -> None:
        scheme = sl.load_scheme(ROOT / "labels.yml")
        self.assertNotIn("science_bitch", scheme["repos"])
        self.assertIn("hotel-kline-game", scheme["repos"])
        self.assertIn("register-study", scheme["repos"])
        # Inclusion is by discovery, not by empty stubs.
        self.assertNotIn("star-system", scheme["repos"])
        self.assertNotIn("synapse", scheme["repos"])
        desired = sl.desired_for(scheme, "hotel-kline-game")
        self.assertIn("governance", desired)
        self.assertIn("bug", desired)
        self.assertNotIn("governance", sl.desired_for(scheme, "star-system"))
        self.assertIn("area:formality", sl.desired_for(scheme, "register-study"))


class MergeRepoFiltersTests(unittest.TestCase):
    def test_repo_flag_and_repos_csv_and_env(self) -> None:
        self.assertEqual(
            sl._merge_repo_filters(["from-env"], ["synapse"], "cortex,synapse"),
            ["from-env", "synapse", "cortex"],
        )
        self.assertIsNone(sl._merge_repo_filters([], None, None))


class IsolationTests(unittest.TestCase):
    def test_one_repo_failure_does_not_abort_the_others(self) -> None:
        scheme = {
            "owner": "AllHailSeizure",
            "shared": [{"name": "bug", "color": "A2EEEF", "description": "x"}],
            "repos": {},
            "renames": {},
            "delete_when_enabled": [],
        }

        def fake_plan(scheme_arg, repo):
            if repo == "broken":
                raise RuntimeError("API request for broken failed: GET labels: HTTP 307")
            return sl.Plan(repo)

        with patch.object(sl, "plan_repo", side_effect=fake_plan):
            plans, failures = sl.sync_repos(
                scheme,
                ["ok-one", "broken", "ok-two"],
                dry_run=True,
                allow_deletes=False,
            )
        self.assertEqual([plan.repo for plan in plans], ["ok-one", "ok-two"])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "broken")
        self.assertIn("307", failures[0][1])


if __name__ == "__main__":
    unittest.main()
