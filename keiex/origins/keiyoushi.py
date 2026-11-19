"""Adapter for repos laid out like keiyoushi/extensions-source.

Extension metadata comes from Gradle itself: `dump-metadata.init.gradle` reads the evaluated
`keiyoushi {}` extension, so loops, mirrors and source ids are exactly what the build computes.
Metadata depends only on a folder's build file, so each distinct build-file blob is evaluated
once, in batches, against the current build infrastructure.
"""

import json
import logging
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from keiex.git import Repo

log = logging.getLogger(__name__)

INIT_SCRIPT = files("keiex").joinpath("dump-metadata.init.gradle")
INFRA_PATHS = [
    ".github", "common", "compiler", "core", "gradle", "lib", "lint-rules",
    "build.gradle.kts", "settings.gradle.kts", "gradle.properties", "gradlew",
]
BATCH_SIZE = 250
GRADLE_FLAGS = ["-q", "help", "--no-configuration-cache", "-Dorg.gradle.configureondemand=false"]


def folder_of(path: str) -> str | None:
    """Maps a repo file path to the extension or theme folder that owns it."""
    parts = path.split("/")
    if parts[0] == "src" and len(parts) >= 4:
        return "/".join(parts[:3])
    if parts[0] == "lib-multisrc" and len(parts) >= 3:
        return "/".join(parts[:2])
    return None


def build_file_folder(path: str) -> str | None:
    """Folder whose own build file this is, or None for deeper/unrelated files."""
    parts = path.split("/")
    if parts[-1] != "build.gradle.kts":
        return None
    if (parts[0] == "src" and len(parts) == 4) or (parts[0] == "lib-multisrc" and len(parts) == 3):
        return "/".join(parts[:-1])
    return None


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


class KeiyoushiOrigin:
    def __init__(self, conn: sqlite3.Connection, origin: str, repo: Repo, work_dir: Path, sdk_properties: Path | None):
        self.conn = conn
        self.origin = origin
        self.repo = repo
        self.work_dir = work_dir
        self.sdk_properties = sdk_properties
        row = conn.execute("SELECT * FROM origin WHERE id = ?", (origin,)).fetchone()
        self.anchor = row["anchor_sha"]
        self.branch = row["default_branch"]

    # ---- git history ----

    def ingest_commits(self) -> str:
        head = self.repo.rev_parse(f"origin/{self.branch}")
        known = {r[0]: r[1] for r in self.conn.execute("SELECT sha, seq FROM git_commit WHERE origin = ?", (self.origin,))}
        shas = self.repo.first_parent_commits(self.anchor, head)
        new = [(seq, sha) for seq, sha in enumerate(shas) if sha not in known]
        if not new:
            return head
        log.info("ingesting %d commits", len(new))
        # One `git show` per chunk: header fields split by \x1f, commits by \x1e, then changed paths.
        fmt = "%x1e%H%x1f%cI%x1f%an%x1f%s%x1f%b%x1f"
        seq_of = dict((sha, seq) for seq, sha in new)
        for i in range(0, len(new), 200):
            chunk = [sha for _, sha in new[i:i + 200]]
            out = self.repo.run("show", "--no-renames", "--first-parent", "--name-only", f"--format={fmt}", *chunk)
            for record in out.split("\x1e")[1:]:
                sha, date, author, subject, body, paths = record.split("\x1f")
                self.conn.execute(
                    "INSERT INTO git_commit VALUES (?,?,?,?,?,?,?,?)",
                    (self.origin, sha, seq_of[sha], date, author, subject, body.strip() or None, pr_number_of(subject)),
                )
                folders = {f for p in paths.split("\n") if p and (f := folder_of(p))}
                self.conn.executemany(
                    "INSERT OR IGNORE INTO commit_folder VALUES (?,?,?)", [(self.origin, sha, f) for f in folders]
                )
        self.conn.commit()
        return head

    def ingest_build_file_timeline(self) -> None:
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
                for path, blob in self.repo.tree_entries(c["sha"], "src", "lib-multisrc").items():
                    if folder := build_file_folder(path):
                        rows.append((self.origin, folder, c["sha"], 0, blob))
                continue
            out = self.repo.run("diff-tree", "-r", "--no-renames", "--no-commit-id", "-z", f"{c['sha']}^", c["sha"], "--", "src", "lib-multisrc")
            fields = out.split("\0")
            # -z raw format: ":<mode> <mode> <old> <new> <status>\0<path>\0"
            for meta, path in zip(fields[0::2], fields[1::2]):
                if not meta or not (folder := build_file_folder(path)):
                    continue
                new_blob, status = meta.split()[3], meta.split()[4]
                rows.append((self.origin, folder, c["sha"], c["seq"], None if status == "D" else new_blob))
        self.conn.executemany("INSERT OR REPLACE INTO folder_version VALUES (?,?,?,?,?)", rows)
        self.conn.commit()
        log.info("timeline: %d build-file versions added", len(rows))

    # ---- Gradle evaluation ----

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
        if not pending:
            return
        log.info("%d build files to evaluate", len(pending))

        # Layer k holds each folder's k-th pending version, so a batch never has a folder twice.
        layers: list[list[sqlite3.Row]] = []
        depth: dict[str, int] = defaultdict(int)
        for row in pending:
            k = depth[row["path"]]
            depth[row["path"]] += 1
            if k == len(layers):
                layers.append([])
            layers[k].append(row)

        head = self.repo.rev_parse(f"origin/{self.branch}")
        tree = self._prepare_tree(head)
        theme_blobs = self._latest_theme_blobs()
        failed: list[sqlite3.Row] = []
        for layer in layers:
            for i in range(0, len(layer), BATCH_SIZE):
                failed += self._evaluate_batch(tree, layer[i:i + BATCH_SIZE], theme_blobs)

        for row in failed:
            self._evaluate_at_own_commit(row)

    def _prepare_tree(self, rev: str) -> Path:
        tree = self.work_dir / "eval" / self.origin.replace("/", "__")
        if tree.exists():
            shutil.rmtree(tree)
        tree.mkdir(parents=True)
        self._export(rev, tree, INFRA_PATHS)
        return tree

    def _export(self, rev: str, dest: Path, paths: list[str]) -> None:
        present = [p for p in paths if self.repo.run("ls-tree", rev, "--", p).strip()]
        archive = subprocess.run(
            ["git", "-C", str(self.repo.path), "archive", "--format=tar", rev, "--", *present],
            check=True, capture_output=True,
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
        if self.sdk_properties:
            shutil.copy(self.sdk_properties, dest / "local.properties")

    def _latest_theme_blobs(self) -> dict[str, str]:
        """Every theme that ever existed, at its last non-deleted version: old build files may name removed themes."""
        rows = self.conn.execute(
            """
            SELECT path, blob FROM folder_version v WHERE origin = ? AND path LIKE 'lib-multisrc/%' AND blob IS NOT NULL
            AND seq = (SELECT max(seq) FROM folder_version w WHERE w.origin = v.origin AND w.path = v.path AND w.blob IS NOT NULL)
            """,
            (self.origin,),
        )
        return {r["path"]: r["blob"] for r in rows}

    def _write_folders(self, tree: Path, blobs: dict[str, str]) -> None:
        for d in ("src", "lib-multisrc"):
            shutil.rmtree(tree / d, ignore_errors=True)
        for path, blob in blobs.items():
            (tree / path).mkdir(parents=True, exist_ok=True)
            (tree / path / "build.gradle.kts").write_bytes(self.repo.cat_blob(blob))

    def _evaluate_batch(self, tree: Path, batch: list[sqlite3.Row], theme_blobs: dict[str, str]) -> list[sqlite3.Row]:
        """Evaluates `batch`; returns the rows that must be retried at their own commit."""
        deferred: list[sqlite3.Row] = []
        while batch:
            folders = dict(theme_blobs)
            folders.update({r["path"]: r["blob"] for r in batch})
            self._write_folders(tree, folders)
            log.info("gradle: evaluating %d build files", len(batch))
            ok, out, dump = self._run_gradle(tree)
            if ok:
                self._store(dump, {r["path"]: r["blob"] for r in batch})
                return deferred
            culprit = self._failing_folder(tree, out)
            if any(r["path"] == culprit for r in batch):
                log.info("deferring %s to its own commit", culprit)
                deferred += [r for r in batch if r["path"] == culprit]
                batch = [r for r in batch if r["path"] != culprit]
            elif culprit in theme_blobs:
                del theme_blobs[culprit]
            elif len(batch) == 1:
                return deferred + batch
            else:
                # Failure not attributable from the output: split and retry each half.
                mid = len(batch) // 2
                return (
                    deferred
                    + self._evaluate_batch(tree, batch[:mid], theme_blobs)
                    + self._evaluate_batch(tree, batch[mid:], theme_blobs)
                )
        return deferred

    def _evaluate_at_own_commit(self, row: sqlite3.Row) -> None:
        with tempfile.TemporaryDirectory(dir=self.work_dir) as tmp:
            tree = Path(tmp)
            self._export(row["sha"], tree, INFRA_PATHS)
            themes = {p: b for p, b in self.repo.tree_entries(row["sha"], "lib-multisrc").items() if build_file_folder(p)}
            self._write_folders(tree, {build_file_folder(p): b for p, b in themes.items()} | {row["path"]: row["blob"]})
            ok, out, dump = self._run_gradle(tree)
            if ok:
                self._store(dump, {row["path"]: row["blob"]})
            else:
                log.warning("cannot evaluate %s at %s", row["path"], row["sha"][:10])
                self.conn.execute(
                    "INSERT OR REPLACE INTO blob_metadata VALUES (?, NULL, ?)", (row["blob"], out[-4000:])
                )
                self.conn.commit()

    def _run_gradle(self, tree: Path) -> tuple[bool, str, list[dict]]:
        dump_file = tree / "keidump.jsonl"
        dump_file.unlink(missing_ok=True)
        cmd = ["./gradlew", *GRADLE_FLAGS, f"--init-script={INIT_SCRIPT}", f"-PkeidumpOut={dump_file}"]
        proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        if proc.returncode != 0:
            return False, out, []
        return True, out, [json.loads(line) for line in dump_file.read_text().splitlines()]

    @staticmethod
    def _failing_folder(tree: Path, out: str) -> str | None:
        marker = f"Build file '{tree}/"
        start = out.find(marker)
        if start < 0:
            # Project-path form, e.g. "project ':src:en:foo'".
            start = out.find("project ':")
            if start < 0:
                return None
            end = out.find("'", start + 10)
            folder = out[start + 10:end].replace(":", "/")
            return folder if folder.startswith(("src/", "lib-multisrc/")) else None
        end = out.find("/build.gradle.kts'", start)
        return out[start + len(marker):end] if end > 0 else None

    def _store(self, dump: list[dict], wanted: dict[str, str]) -> None:
        by_path = {d["path"]: d for d in dump}
        for path, blob in wanted.items():
            d = by_path.get(path)
            self.conn.execute(
                "INSERT OR REPLACE INTO blob_metadata VALUES (?,?,?)",
                (blob, json.dumps(d) if d else None, None if d else "not configured by Gradle"),
            )
        self.conn.commit()

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

        for path, history in by_path.items():
            self._derive_folder(path, history)
        self.conn.commit()

    def _derive_folder(self, path: str, history: list[sqlite3.Row]) -> None:
        o = self.origin
        kind = "theme" if path.startswith("lib-multisrc/") else "src"
        parts = path.split("/")
        last = history[-1]
        latest = next((json.loads(v["json"]) for v in reversed(history) if v["json"]), None)
        meta = latest or {}
        lang, module = (parts[1], parts[2]) if kind == "src" else (None, parts[1])
        pkg = None
        if kind == "src":
            pkg = "eu.kanade.tachiyomi.extension." + (meta.get("pkgName") or f"{lang}.{module}")
        self.conn.execute(
            "INSERT INTO extension VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                o, path, kind, lang, module, meta.get("name") or module, pkg, meta.get("theme"),
                meta.get("contentWarning"), meta.get("libVersion"),
                meta.get("versionCode") if kind == "src" else meta.get("baseVersionCode"),
                f"https://cdn.jsdelivr.net/gh/{o}@{self.branch}/{path}/res/mipmap-xhdpi/ic_launcher.png" if kind == "src" else None,
                history[0]["sha"], last["sha"], last["sha"] if last["blob"] is None else None,
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
        self.ingest_build_file_timeline()
        self.evaluate_pending_blobs()
        self.rebuild_derived()
