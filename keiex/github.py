import json
import logging
import os
import sqlite3
import subprocess

import httpx

from keiex.db import get_kv, set_kv
from keiex.origins.keiyoushi import folder_of

log = logging.getLogger(__name__)

PR_FIELDS = """
number title state createdAt mergedAt closedAt updatedAt body
author { login }
files(first: 100) { totalCount nodes { path } }
closingIssuesReferences(first: 20) { nodes { number repository { nameWithOwner } } }
"""

ISSUE_FIELDS = """
number title state createdAt closedAt updatedAt body
author { login }
labels(first: 20) { nodes { name } }
closedByPullRequestsReferences(first: 10, includeClosedPrs: true) { nodes { number repository { nameWithOwner } } }
"""


def token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or subprocess.run(
        ["gh", "auth", "token"], check=True, capture_output=True, text=True
    ).stdout.strip()


class GitHub:
    def __init__(self):
        self.http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"bearer {token()}"},
            timeout=60,
        )

    def graphql(self, query: str, **variables) -> dict:
        for attempt in range(3):
            r = self.http.post("/graphql", json={"query": query, "variables": variables})
            if r.status_code < 500:
                break
            log.warning("GitHub %d, retrying", r.status_code)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(json.dumps(body["errors"])[:2000])
        return body["data"]

    def updated_since(self, owner: str, name: str, connection: str, fields: str, since: str):
        """Yields nodes of `connection` (pullRequests/issues), newest update first, until `since`."""
        query = f"""
        query($owner: String!, $name: String!, $after: String) {{
          repository(owner: $owner, name: $name) {{
            {connection}(first: 50, after: $after, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ {fields} }}
            }}
          }}
        }}"""
        after = None
        while True:
            page = self.graphql(query, owner=owner, name=name, after=after)["repository"][connection]
            for node in page["nodes"]:
                if node["updatedAt"] < since:
                    return
                yield node
            if not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    def all_pr_files(self, owner: str, name: str, number: int) -> list[str]:
        query = """
        query($owner: String!, $name: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              files(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { path } }
            }
          }
        }"""
        paths, after = [], None
        while True:
            files = self.graphql(query, owner=owner, name=name, number=number, after=after)["repository"]["pullRequest"]["files"]
            paths += [n["path"] for n in files["nodes"]]
            if not files["pageInfo"]["hasNextPage"]:
                return paths
            after = files["pageInfo"]["endCursor"]


def same_repo_numbers(refs: dict, origin: str) -> list[int]:
    return [n["number"] for n in refs["nodes"] if n["repository"]["nameWithOwner"].lower() == origin.lower()]


def ingest(conn: sqlite3.Connection, origin: str) -> None:
    gh = GitHub()
    owner, name = origin.split("/")
    anchor_date = conn.execute(
        "SELECT date FROM git_commit WHERE origin = ? AND seq = 0", (origin,)
    ).fetchone()[0]

    key = f"github:{origin}:prs_since"
    since = get_kv(conn, key) or anchor_date
    newest = since
    count = 0
    for pr in gh.updated_since(owner, name, "pullRequests", PR_FIELDS, since):
        newest = max(newest, pr["updatedAt"])
        paths = [n["path"] for n in pr["files"]["nodes"]]
        if pr["files"]["totalCount"] > len(paths):
            paths = gh.all_pr_files(owner, name, pr["number"])
        folders = sorted({f for p in paths if (f := folder_of(p))})
        conn.execute(
            "INSERT OR REPLACE INTO pull_request VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                origin, pr["number"], pr["title"], pr["state"], (pr["author"] or {}).get("login"),
                pr["createdAt"], pr["mergedAt"], pr["closedAt"], pr["body"],
                folders[0] if len(folders) == 1 else None,
            ),
        )
        conn.execute("DELETE FROM pr_folder WHERE origin = ? AND number = ?", (origin, pr["number"]))
        conn.executemany("INSERT INTO pr_folder VALUES (?,?,?)", [(origin, pr["number"], f) for f in folders])
        for issue in same_repo_numbers(pr["closingIssuesReferences"], origin):
            conn.execute("INSERT OR IGNORE INTO issue_pr VALUES (?,?,?)", (origin, issue, pr["number"]))
        count += 1
        if count % 200 == 0:
            log.info("%d PRs", count)
            conn.commit()
    set_kv(conn, key, newest)
    conn.commit()
    log.info("PRs: %d updated", count)

    key = f"github:{origin}:issues_since"
    since = get_kv(conn, key) or anchor_date
    newest = since
    count = 0
    for issue in gh.updated_since(owner, name, "issues", ISSUE_FIELDS, since):
        newest = max(newest, issue["updatedAt"])
        conn.execute(
            "INSERT OR REPLACE INTO issue VALUES (?,?,?,?,?,?,?,?,?)",
            (
                origin, issue["number"], issue["title"], issue["state"], (issue["author"] or {}).get("login"),
                issue["createdAt"], issue["closedAt"], issue["body"],
                json.dumps([label["name"] for label in issue["labels"]["nodes"]]),
            ),
        )
        for pr in same_repo_numbers(issue["closedByPullRequestsReferences"], origin):
            conn.execute("INSERT OR IGNORE INTO issue_pr VALUES (?,?,?)", (origin, issue["number"], pr))
        count += 1
        if count % 200 == 0:
            log.info("%d issues", count)
            conn.commit()
    set_kv(conn, key, newest)
    conn.commit()
    log.info("issues: %d updated", count)
