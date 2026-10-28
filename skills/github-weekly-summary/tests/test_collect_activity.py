import argparse
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "activity", Path(__file__).parents[1] / "scripts" / "collect_activity.py")
activity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activity)


def commit(sha):
    return {"sha": sha, "html_url": f"https://github.com/o/r/commit/{sha}",
            "commit": {"message": "fix: restore connection tests\n\nHandle cold start.",
                       "author": {"date": "2026-10-15T00:00:00Z"},
                       "committer": {"date": "2026-10-15T00:00:00Z"}},
            "parents": [{"sha": "parent"}]}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.since = activity.timestamp("2026-10-09T10:00:00+08:00")
        self.until = activity.timestamp("2026-10-16T10:00:00+08:00")

    def test_pagination_and_get_with_branch_name(self):
        import json
        pages = [[{"sha": str(n)} for n in range(100)], [{"sha": "100"}]]
        response = subprocess.CompletedProcess([], 0, json.dumps(pages), "")
        with patch.object(activity.subprocess, "run", return_value=response) as run:
            rows = activity.api("repos/o/r/commits", {"sha": "feature/a b"}, paginate=True)
        self.assertEqual(len(rows), 101)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--method") + 1], "GET")
        self.assertIn("--paginate", command)
        self.assertIn("--slurp", command)
        self.assertIn("sha=feature/a b", command)

    def test_nondefault_commits_and_repository_sha_dedup(self):
        def api(endpoint, params=None, **kwargs):
            if endpoint == "user":
                return {"login": "current-user"}
            if endpoint == "user/repos":
                self.assertEqual(params["affiliation"], "owner,collaborator,organization_member")
                self.assertNotIn("since", params)
                return [{"full_name": "o/r"}, {"full_name": "o/fork"}]
            if endpoint.endswith("/branches"):
                return [{"name": "main"}, {"name": "feature/new"}]
            self.assertEqual(params["author"], "current-user")
            self.assertEqual(params["since"], "2026-10-09T02:00:00Z")
            self.assertEqual(params["until"], "2026-10-16T02:00:00Z")
            return [commit("shared")] + ([commit("unmerged")] if params["sha"] != "main" else [])
        with patch.object(activity, "api", side_effect=api):
            report, status = activity.collect([], None, self.since, self.until, discovery="all")
        self.assertEqual(status, 0)
        self.assertEqual(len(report["commits"]), 4)
        self.assertEqual(report["commits"][0]["branches"], ["main", "feature/new"])
        self.assertTrue(report["coverage"]["complete"])

    def test_alias_union_and_partial_permission_failure(self):
        def api(endpoint, params=None, **kwargs):
            if endpoint.endswith("bad/branches"):
                raise activity.ApiError("Not Found (HTTP 404)")
            if endpoint.endswith("/branches"):
                return [{"name": "main"}]
            return [commit("shared")]
        with patch.object(activity, "api", side_effect=api):
            report, status = activity.collect(["login", "alias@example.com"],
                ["o/good", "o/bad"], self.since, self.until)
        self.assertEqual(status, 2)
        self.assertEqual(len(report["commits"]), 1)
        self.assertEqual(report["coverage"]["repositories_completed"], 1)
        self.assertFalse(report["coverage"]["complete"])
        self.assertIn("o/bad", report["errors"][0]["scope"])

    def test_rate_limit_stops_and_preserves_results(self):
        responses = [[{"name": "main"}], [commit("saved")],
                     activity.ApiError("API rate limit exceeded (HTTP 403)")]
        with patch.object(activity, "api", side_effect=responses) as api:
            report, status = activity.collect(["login"], ["o/one", "o/two", "o/three"],
                                             self.since, self.until)
        self.assertEqual(api.call_count, 3)
        self.assertEqual(status, 2)
        self.assertEqual(report["commits"][0]["sha"], "saved")
        self.assertEqual(report["coverage"]["repositories_completed"], 1)

    def test_discovery_failure_is_not_empty_success(self):
        with patch.object(activity, "api", side_effect=activity.ApiError("Requires authentication")):
            report, status = activity.collect([], None, self.since, self.until)
        self.assertEqual(status, 1)
        self.assertFalse(report["coverage"]["complete"])
        self.assertTrue(report["errors"])

    def test_default_branch_option(self):
        with patch.object(activity, "api", side_effect=[{"default_branch": "trunk"}, []]) as api:
            report, status = activity.collect(["login"], ["o/r"], self.since, self.until, "default")
        self.assertEqual(status, 0)
        self.assertEqual(api.call_args.args[1]["sha"], "trunk")
        self.assertEqual(report["branch_scope"], "default")

    def test_timestamp_requires_timezone(self):
        self.assertEqual(activity.iso(self.until), "2026-10-16T02:00:00Z")
        with self.assertRaises(argparse.ArgumentTypeError):
            activity.timestamp("2026-10-16T10:00:00")

    def test_recent_discovery_unions_sources_without_listing_organization_repos(self):
        calls = []
        def api(endpoint, params=None, **kwargs):
            calls.append(endpoint)
            if endpoint == "graphql":
                return {"data": {"user": {"contributionsCollection": {
                    "restrictedContributionsCount": 0,
                    "commitContributionsByRepository": [{"repository": {"nameWithOwner": "o/committed"}}],
                    "pullRequestContributionsByRepository": [{"repository": {"nameWithOwner": "o/pr"}}],
                    "pullRequestReviewContributionsByRepository": [],
                }}}}
            if endpoint.endswith("/events"):
                return [
                    {"type": "PushEvent", "created_at": "2026-10-15T00:00:00Z", "repo": {"name": "o/branch-only"}},
                    {"type": "WatchEvent", "created_at": "2026-10-15T00:00:00Z", "repo": {"name": "o/watched"}},
                    {"type": "PushEvent", "created_at": "2026-10-01T00:00:00Z", "repo": {"name": "o/old"}},
                ]
            if endpoint.endswith("/branches"):
                return [{"name": "main"}]
            return []
        with patch.object(activity, "api", side_effect=api):
            report, status = activity.collect(["login"], None, self.since, self.until,
                                             include_repos=["o/known", "O/KNOWN"])
        self.assertEqual(status, 0)
        self.assertNotIn("user/repos", calls)
        self.assertEqual(report["coverage"]["repositories_discovered"], 4)
        self.assertFalse(report["coverage"]["repository_discovery_exhaustive"])
        self.assertEqual(report["repository_scope"], "recent_activity")
        self.assertIn("repos/o/branch-only/branches", calls)
        self.assertNotIn("repos/o/watched/branches", calls)
        self.assertNotIn("repos/o/old/branches", calls)

    def test_discovery_source_failure_keeps_other_source_and_reports_gap(self):
        def api(endpoint, params=None, **kwargs):
            if endpoint == "graphql":
                raise activity.ApiError("Insufficient permission")
            if endpoint.endswith("/events"):
                return [{"type": "PushEvent", "created_at": "2026-10-15T00:00:00Z", "repo": {"name": "o/r"}}]
            if endpoint.endswith("/branches"):
                return [{"name": "main"}]
            return [commit("saved")]
        with patch.object(activity, "api", side_effect=api):
            report, status = activity.collect(["login"], None, self.since, self.until)
        self.assertEqual(status, 2)
        self.assertEqual(len(report["commits"]), 1)
        self.assertIn("contribution discovery", report["errors"][0]["scope"])

    def test_graphql_uses_query_post_and_detects_response_errors(self):
        response = subprocess.CompletedProcess([], 0, '{"errors":[{"message":"Not authorized"}]}', "")
        with patch.object(activity.subprocess, "run", return_value=response) as run:
            with self.assertRaises(activity.ApiError):
                activity.api("graphql", {"query": activity.CONTRIBUTIONS_QUERY})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--method") + 1], "POST")


if __name__ == "__main__":
    unittest.main()
