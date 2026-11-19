"""What every origin adapter shares: git history, the metadata-file timeline and the derived tables.

An adapter says which files define a folder's metadata and how to evaluate them; evaluated metadata is
a dict in one shape for all adapters:

    {"kind": "src" | "theme", "name", "theme", "contentWarning", "libVersion", "versionCode", "pkgName",
     "deeplinks": [{"hosts": [...]}],
     "sources": [{"id", "name", "lang", "baseUrl": {"type", "defaultUrl", "mirrors": [{"url"}]}}]}
"""

import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from keiex.git import Repo

log = logging.getLogger(__name__)


def pr_number_of(subject: str) -> int | None:
    """Squash merges end with "(#123)"."""
    s = subject.rstrip()
    start = s.rfind("(#")
    if not s.endswith(")") or start < 0:
        return None
    digits = s[start + 2:-1]
    return int(digits) if digits.isdigit() else None


def host_of(url: str) -> str | None:
    return urlsplit(url).hostname or None


class OriginAdapter:
    # Tree paths that hold metadata files, for ls-tree/diff-tree.
    METADATA_ROOTS: tuple[str, ...] = ()

    def __init__(self, conn: sqlite3.Connection, origin: str, repo: Repo, work_dir: Path, **options):
        self.conn = conn
        self.origin = origin
        self.repo = repo
        self.work_dir = work_dir
        self.options = options
        row = conn.execute("SELECT * FROM origin WHERE id = ?", (origin,)).fetchone()
        self.anchor = row["anchor_sha"]
        self.branch = row["default_branch"]

    # ---- layout, implemented per adapter ----

    @staticmethod
    def folder_of(path: str) -> str | None:
        """The extension or theme folder that owns a repo file, if any."""
        raise NotImplementedError

    @staticmethod
    def metadata_file_folder(path: str) -> str | None:
        """The folder whose metadata this file defines, if it is such a file."""
        raise NotImplementedError

    def evaluate(self, pending: list[sqlite3.Row]) -> None:
        """Fills blob_metadata for `pending` rows (path, blob, seq, sha)."""
        raise NotImplementedError

    def folder_info(self, path: str, kind: str, meta: dict, removed_sha: str | None) -> dict:
        """Adapter-specific columns of `extension`: lang, module, package_name, theme_path, icon_url, source_url."""
        raise NotImplementedError

    def prepare_derive(self) -> None:
        """Hook run once before the derived tables are rebuilt."""

    # ---- git history ----

    def ingest_commits(self) -> None:
        head = self.repo.rev_parse(f"origin/{self.branch}")
        known = {r[0] for r in self.conn.execute("SELECT sha FROM git_commit WHERE origin = ?", (self.origin,))}
        shas = self.repo.first_parent_commits(self.anchor, head)
        new = [(seq, sha) for seq, sha in enumerate(shas) if sha not in known]
        if not new:
            return
        log.info("%s: ingesting %d commits", self.origin, len(new))
        # One `git show` per chunk: header fields split by \x1f, commits by \x1e, then changed paths.
        fmt = "%x1e%H%x1f%cI%x1f%an%x1f%s%x1f%b%x1f"
        seq_of = {sha: seq for seq, sha in new}
        for i in range(0, len(new), 200):
            chunk = [sha for _, sha in new[i:i + 200]]
            out = self.repo.run("show", "--no-renames", "--first-parent", "--name-only", f"--format={fmt}", *chunk)
            for record in out.split("\x1e")[1:]:
                sha, date, author, subject, body, paths = record.split("\x1f")
                self.conn.execute(
                    "INSERT INTO git_commit VALUES (?,?,?,?,?,?,?,?)",
                    (self.origin, sha, seq_of[sha], date, author, subject, body.strip() or None, pr_number_of(subject)),
                )
                folders = {f for p in paths.split("\n") if p and (f := self.folder_of(p))}
                self.conn.executemany(
                    "INSERT OR IGNORE INTO commit_folder VALUES (?,?,?)", [(self.origin, sha, f) for f in folders]
                )
        self.conn.commit()

    def ingest_metadata_timeline(self) -> None:
        last_seq = self.conn.execute(
            "SELECT max(seq) FROM folder_version WHERE origin = ?", (self.origin,)
        ).fetchone()[0]
        commits = self.conn.execute(
            "SELECT sha, seq FROM git_commit WHERE origin = ? AND seq > ? ORDER BY seq",
            (self.origin, -1 if last_seq is None else last_seq),
        ).fetchall()
        rows = []
        for c in commits:
            if c["seq"] == 0:
                for path, blob in self.repo.tree_entries(c["sha"], *self.METADATA_ROOTS).items():
                    if folder := self.metadata_file_folder(path):
                        rows.append((self.origin, folder, c["sha"], 0, blob))
                continue
            out = self.repo.run(
                "diff-tree", "-r", "--no-renames", "--no-commit-id", "-z", f"{c['sha']}^", c["sha"], "--", *self.METADATA_ROOTS
            )
            fields = out.split("\0")
            # -z raw format: ":<mode> <mode> <old> <new> <status>\0<path>\0"
            for meta, path in zip(fields[0::2], fields[1::2]):
                if not meta or not (folder := self.metadata_file_folder(path)):
                    continue
                new_blob, status = meta.split()[3], meta.split()[4]
                rows.append((self.origin, folder, c["sha"], c["seq"], None if status == "D" else new_blob))
        self.conn.executemany("INSERT OR REPLACE INTO folder_version VALUES (?,?,?,?,?)", rows)
        self.conn.commit()
        log.info("%s: %d metadata-file versions added", self.origin, len(rows))

    def evaluate_pending_blobs(self) -> None:
        pending = self.conn.execute(
            """
            SELECT v.path, v.blob, min(v.seq) AS seq, min(v.sha) AS sha FROM folder_version v
            LEFT JOIN blob_metadata b ON b.blob = v.blob
            WHERE v.origin = ? AND v.blob IS NOT NULL AND b.blob IS NULL
            GROUP BY v.blob ORDER BY seq
            """,
            (self.origin,),
        ).fetchall()
        if pending:
            log.info("%s: %d metadata files to evaluate", self.origin, len(pending))
            self.evaluate(pending)

    def store(self, blob: str, meta: dict | None, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO blob_metadata VALUES (?,?,?)",
            (blob, json.dumps(meta) if meta else None, None if meta else (error or "no metadata")),
        )

    # ---- derived tables ----

    def rebuild_derived(self) -> None:
        o = self.origin
        for table in ("extension", "source", "domain_usage"):
            self.conn.execute(f"DELETE FROM {table} WHERE origin = ?", (o,))
        versions = self.conn.execute(
            """
            SELECT v.path, v.sha, v.seq, v.blob, b.json FROM folder_version v
            LEFT JOIN blob_metadata b ON b.blob = v.blob
            WHERE v.origin = ? ORDER BY v.path, v.seq
            """,
            (o,),
        ).fetchall()
        by_path: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for v in versions:
            by_path[v["path"]].append(v)
        self.prepare_derive()
        for path, history in by_path.items():
            self._derive_folder(path, history)
        self.conn.commit()

    def _derive_folder(self, path: str, history: list[sqlite3.Row]) -> None:
        o = self.origin
        last = history[-1]
        removed_sha = last["sha"] if last["blob"] is None else None
        meta = next((json.loads(v["json"]) for v in reversed(history) if v["json"]), None) or {}
        kind = meta.get("kind") or self.default_kind(path)
        info = self.folder_info(path, kind, meta, removed_sha)
        self.conn.execute(
            """INSERT INTO extension (origin, path, kind, lang, module, name, package_name, theme, theme_path,
                 content_warning, lib_version, version_code, icon_url, source_url, first_sha, last_sha, removed_sha)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                o, path, kind, info["lang"], info["module"], meta.get("name") or info["module"], info.get("package_name"),
                meta.get("theme"), info.get("theme_path"), meta.get("contentWarning"), meta.get("libVersion"),
                meta.get("versionCode") if kind == "src" else meta.get("baseVersionCode"),
                info.get("icon_url"), info.get("source_url"), history[0]["sha"], last["sha"], removed_sha,
            ),
        )
        for s in meta.get("sources", []):
            url = s["baseUrl"]
            self.conn.execute(
                "INSERT OR REPLACE INTO source VALUES (?,?,?,?,?,?,?,?)",
                (o, path, str(s["id"]), s["name"], s["lang"], url["defaultUrl"], url["type"],
                 json.dumps([m["url"] for m in url.get("mirrors", [])])),
            )

        # Open/close host ranges along the folder's history.
        open_since: dict[tuple[str, str], str] = {}
        for v in history:
            hosts = self._hosts(json.loads(v["json"])) if v["json"] else (set() if v["blob"] is None else set(open_since))
            for key in list(open_since):
                if key not in hosts:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO domain_usage VALUES (?,?,?,?,?,?)",
                        (o, path, key[0], key[1], open_since.pop(key), v["sha"]),
                    )
            for key in hosts:
                open_since.setdefault(key, v["sha"])
        for (host, role), since in open_since.items():
            self.conn.execute("INSERT OR REPLACE INTO domain_usage VALUES (?,?,?,?,?,NULL)", (o, path, host, role, since))

    def default_kind(self, path: str) -> str:
        return "src"

    @staticmethod
    def _hosts(meta: dict) -> set[tuple[str, str]]:
        hosts = set()
        for s in meta.get("sources", []):
            url = s["baseUrl"]
            if h := host_of(url["defaultUrl"]):
                hosts.add((h, "base"))
            for m in url.get("mirrors", []):
                if (h := host_of(m["url"])) and (h, "base") not in hosts:
                    hosts.add((h, "mirror"))
        for d in meta.get("deeplinks", []):
            for h in d["hosts"]:
                if (h.lower(), "base") not in hosts and (h.lower(), "mirror") not in hosts:
                    hosts.add((h.lower(), "deeplink"))
        return hosts

    def run(self) -> None:
        self.repo.run("fetch", "--quiet", "origin", self.branch)
        self.ingest_commits()
        self.ingest_metadata_timeline()
        self.evaluate_pending_blobs()
        self.rebuild_derived()
