#!/usr/bin/env python3
"""Collect author commits through read-only gh REST calls; emit JSON, not prose."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import sys


class ApiError(RuntimeError):
    @property
    def rate_limited(self):
        return bool(re.search(r"rate limit|HTTP 429", str(self), re.I))


def api(endpoint, params=None, *, paginate=False):
    method = "POST" if endpoint == "graphql" else "GET"
    cmd = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    for key, value in (params or {}).items():
        cmd += ["--raw-field", f"{key}={value}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(f"GitHub CLI unavailable or timed out: {type(exc).__name__}") from exc
    if result.returncode:
        message = result.stderr.strip().splitlines()
        message = message[-1] if message else "GitHub API request failed"
        message = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", message)
        raise ApiError(message)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise ApiError("GitHub CLI returned invalid JSON") from exc
    if isinstance(data, dict) and data.get("errors"):
        raise ApiError("; ".join(e["message"] for e in data["errors"]))
    if paginate:
        if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
            raise ApiError("Expected paginated JSON arrays")
        return [item for page in data for item in page]
    return data


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO 8601 timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp must include timezone, e.g. +08:00 or Z")
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


CONTRIBUTIONS_QUERY = """
query($login: String!, $since: DateTime!, $until: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $since, to: $until) {
      restrictedContributionsCount
      commitContributionsByRepository(maxRepositories: 100) { repository { nameWithOwner } }
      pullRequestContributionsByRepository(maxRepositories: 100) { repository { nameWithOwner } }
      pullRequestReviewContributionsByRepository(maxRepositories: 100) { repository { nameWithOwner } }
    }
  }
}
"""


def discover_recent(logins, since, until, report):
    """Use participation hints to select repos, never as the final commit history."""
    names = set()
    report["warnings"].append(
        "Repository discovery uses contribution/activity hints and may omit repositories; "
        "use --include-repo/--repo for known projects or --discovery all for a full scan.")
    for login in logins:
        try:
            data = api("graphql", {"query": CONTRIBUTIONS_QUERY, "login": login,
                                   "since": iso(since), "until": iso(until)})
            user = (data.get("data") or {}).get("user")
            if not user:
                raise ApiError(f"No contribution data for {login}")
            collection = user["contributionsCollection"]
            for field in ("commitContributionsByRepository", "pullRequestContributionsByRepository",
                          "pullRequestReviewContributionsByRepository"):
                rows = collection[field]
                names.update(r["repository"]["nameWithOwner"] for r in rows if r.get("repository"))
                if len(rows) >= 100:
                    report["warnings"].append(f"{login}:{field} reached the 100-repository discovery limit.")
            if collection.get("restrictedContributionsCount"):
                report["warnings"].append(f"{login}: some contribution repository names are restricted.")
        except ApiError as exc:
            report["errors"].append({"scope": f"{login}:contribution discovery", "message": str(exc)})
            if exc.rate_limited:
                raise
        try:
            events = api(f"users/{login}/events", {"per_page": 100}, paginate=True)
            work_events = {"PushEvent", "PullRequestEvent", "PullRequestReviewEvent",
                           "PullRequestReviewCommentEvent", "CreateEvent"}
            for event in events:
                if (event["type"] in work_events and
                        since <= timestamp(event["created_at"]) <= until):
                    names.add(event["repo"]["name"])
            if len(events) >= 300 and min(timestamp(e["created_at"]) for e in events) > since:
                report["warnings"].append(f"{login}: 300-event limit does not cover the entire time window.")
        except ApiError as exc:
            report["errors"].append({"scope": f"{login}:event discovery", "message": str(exc)})
            if exc.rate_limited:
                raise
    return [{"full_name": name} for name in sorted(names)]


def collect(authors, repos, since, until, branch_scope="all", discovery="recent", include_repos=None):
    report = {
        "authors": authors, "since": iso(since), "until": iso(until),
        "branch_scope": branch_scope,
        "repository_scope": "explicit" if repos else (
            "recent_activity" if discovery == "recent" else "authenticated_affiliations"),
        "coverage": {"repositories_discovered": 0, "repositories_completed": 0,
                     "branch_author_queries_completed": 0, "complete": False},
        "commits": [], "errors": [], "warnings": [],
    }
    commits = {}

    def failure(scope, exc):
        report["errors"].append({"scope": scope, "message": str(exc)})

    try:
        if not authors:
            authors = [api("user")["login"]]
            report["authors"] = authors
        if repos:
            repositories = [{"full_name": name} for name in dict.fromkeys(repos)]
        elif discovery == "recent":
            logins = [a for a in authors if "@" not in a]
            if not logins:
                logins = [api("user")["login"]]
                report["warnings"].append("Email-only authors: discovery uses the authenticated user's activity.")
            repositories = discover_recent(logins, since, until, report)
        else:
            repositories = api("user/repos", {
                "affiliation": "owner,collaborator,organization_member",
                "visibility": "all", "per_page": 100,
            }, paginate=True)
    except ApiError as exc:
        failure("identity/repository discovery", exc)
        return report, 1

    known = {repo["full_name"].lower() for repo in repositories}
    for name in include_repos or []:
        if name.lower() not in known:
            repositories.append({"full_name": name})
            known.add(name.lower())
    report["coverage"]["repository_discovery_exhaustive"] = bool(repos) or discovery == "all"
    report["coverage"]["repositories_discovered"] = len(repositories)
    stopped = False
    for repo in repositories:
        name = repo["full_name"]
        error_count = len(report["errors"])
        try:
            if branch_scope == "default":
                info = repo if "default_branch" in repo else api(f"repos/{name}")
                branches = [{"name": info["default_branch"]}] if info.get("default_branch") else []
            else:
                branches = api(f"repos/{name}/branches", {"per_page": 100}, paginate=True)
        except ApiError as exc:
            failure(f"{name}:branches", exc)
            if exc.rate_limited:
                stopped = True
                break
            continue

        for branch in branches:
            for author in authors:
                try:
                    rows = api(f"repos/{name}/commits", {
                        "sha": branch["name"], "author": author,
                        "since": iso(since), "until": iso(until), "per_page": 100,
                    }, paginate=True)
                    report["coverage"]["branch_author_queries_completed"] += 1
                except ApiError as exc:
                    failure(f"{name}:{branch['name']}:{author}", exc)
                    if exc.rate_limited:
                        stopped = True
                        break
                    continue
                for row in rows:
                    key = (name, row["sha"])
                    if key not in commits:
                        metadata = row["commit"]
                        commits[key] = {
                            "repository": name, "sha": row["sha"],
                            "url": row["html_url"], "message": metadata["message"],
                            "author_date": (metadata.get("author") or {}).get("date"),
                            "committer_date": (metadata.get("committer") or {}).get("date"),
                            "is_merge": len(row.get("parents", [])) > 1,
                            "branches": [],
                        }
                    if branch["name"] not in commits[key]["branches"]:
                        commits[key]["branches"].append(branch["name"])
            if stopped:
                break
        if stopped:
            break
        if len(report["errors"]) == error_count:
            report["coverage"]["repositories_completed"] += 1

    report["commits"] = sorted(commits.values(), key=lambda c: (
        c["repository"], c["committer_date"] or "", c["sha"]))
    report["coverage"]["complete"] = not report["errors"]
    return report, 2 if report["errors"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--author", action="append", help="GitHub login or email; repeat for aliases")
    parser.add_argument("--repo", action="append", help="OWNER/REPO; repeat to select repositories")
    parser.add_argument("--include-repo", action="append", help="Add a known repository to discovery results")
    parser.add_argument("--discovery", choices=["recent", "all"], default="recent",
                        help="Select recently participated repositories (default) or scan all affiliations")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--since", type=timestamp)
    parser.add_argument("--until", type=timestamp)
    parser.add_argument("--branch-scope", choices=["all", "default"], default="all")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r)
           for r in (args.repo or []) + (args.include_repo or [])):
        parser.error("Repository must be OWNER/REPO")
    until = args.until or datetime.now(timezone.utc).replace(microsecond=0)
    since = args.since or until - timedelta(days=args.days)
    if since >= until:
        parser.error("--since must be before --until")
    report, status = collect(list(dict.fromkeys(args.author or [])), args.repo,
                             since, until, args.branch_scope, args.discovery, args.include_repo)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
