"""Adapter for repos laid out like keiyoushi/extensions-source.

Extension metadata comes from Gradle itself: `dump-metadata.init.gradle` reads the evaluated
`keiyoushi {}` extension, so loops, mirrors and source ids are exactly what the build computes.
Metadata depends only on a folder's build file, so each distinct build-file blob is evaluated
once, in batches, against the current build infrastructure.
"""

import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from importlib.resources import files
from pathlib import Path

from keiex.origins.base import OriginAdapter

log = logging.getLogger(__name__)

INIT_SCRIPT = files("keiex").joinpath("dump-metadata.init.gradle")
INFRA_PATHS = [
    ".github", "common", "compiler", "core", "gradle", "lib", "lint-rules",
    "build.gradle.kts", "settings.gradle.kts", "gradle.properties", "gradlew",
]
BATCH_SIZE = 250
# Highest versionId tried when recognizing an auto-computed source id.
MAX_VERSION_ID = 64
GRADLE_FLAGS = ["-q", "help", "--no-configuration-cache", "-Dorg.gradle.configureondemand=false"]


def folder_of(path: str) -> str | None:
    """Maps a repo file path to the extension or theme folder that owns it."""
    parts = path.split("/")
    if parts[0] == "src" and len(parts) >= 4:
        return "/".join(parts[:3])
    if parts[0] == "lib-multisrc" and len(parts) >= 3:
        return "/".join(parts[:2])
    return None


def source_id(name: str, lang: str, version_id: int = 1) -> int:
    """computeSourceId in gradle/build-logic ExtensionPlugin.kt."""
    digest = hashlib.md5(f"{name.lower()}/{lang}/{version_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def stable_source_id(source: dict) -> str:
    """An auto-computed id with its versionId reset to 1, so a versionId bump keeps the same source; explicit ids as is."""
    sid = int(source["id"])
    for version_id in range(2, MAX_VERSION_ID + 1):
        if source_id(source["name"], source["lang"], version_id) == sid:
            return str(source_id(source["name"], source["lang"]))
    return str(sid)


def build_file_folder(path: str) -> str | None:
    """Folder whose own build file this is, or None for deeper/unrelated files."""
    parts = path.split("/")
    if parts[-1] != "build.gradle.kts":
        return None
    if (parts[0] == "src" and len(parts) == 4) or (parts[0] == "lib-multisrc" and len(parts) == 3):
        return "/".join(parts[:-1])
    return None


class KeiyoushiOrigin(OriginAdapter):
    METADATA_ROOTS = ("src", "lib-multisrc")
    folder_of = staticmethod(folder_of)
    metadata_file_folder = staticmethod(build_file_folder)

    @property
    def sdk_properties(self) -> Path | None:
        return self.options.get("sdk_properties")

    # ---- Gradle evaluation ----

    def evaluate(self, pending: list[sqlite3.Row]) -> None:
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
                self.store(row["blob"], None, out[-4000:])
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
            self.store(blob, by_path.get(path), "not configured by Gradle")
        self.conn.commit()

    # ---- derived tables ----

    def default_kind(self, path: str) -> str:
        return "theme" if path.startswith("lib-multisrc/") else "src"

    def stable_id(self, source: dict) -> str:
        return stable_source_id(source)

    def prepare_derive(self) -> None:
        self._head_files = set(self.repo.tree_entries(f"origin/{self.branch}", "src", "lib-multisrc", "core/src/main/res"))

    def folder_info(self, path: str, kind: str, meta: dict, removed_sha: str | None) -> dict:
        parts = path.split("/")
        if kind == "theme":
            return {"lang": None, "module": parts[1], "source_url": self._tree_url(path, removed_sha)}
        lang, module = parts[1], parts[2]
        theme = meta.get("theme")
        return {
            "lang": lang,
            "module": module,
            "package_name": "eu.kanade.tachiyomi.extension." + (meta.get("pkgName") or f"{lang}.{module}"),
            "theme_path": f"lib-multisrc/{theme}" if theme else None,
            "icon_url": self._icon_url(path, theme, removed_sha),
            "source_url": self._tree_url(path, removed_sha),
        }

    def _tree_url(self, path: str, removed_sha: str | None) -> str:
        rev = self.repo.rev_parse(f"{removed_sha}^") if removed_sha else self.branch
        return f"https://github.com/{self.origin}/tree/{rev}/{path}"

    def _icon_url(self, path: str, theme: str | None, removed_sha: str | None) -> str | None:
        """The extension's own launcher icon, else its theme's, else the default one (as the published index does).

        A removed extension's files are gone from the branch, so its icon is pinned to the commit before removal.
        """
        candidates = [path] + ([f"lib-multisrc/{theme}"] if theme else []) + ["core/src/main"]
        if removed_sha:
            rev = self.repo.rev_parse(f"{removed_sha}^")
            files = set(self.repo.tree_entries(rev, *candidates))
        else:
            rev, files = self.branch, self._head_files
        for folder in candidates:
            icon = f"{folder}/res/mipmap-xhdpi/ic_launcher.png"
            if icon in files:
                return f"https://cdn.jsdelivr.net/gh/{self.origin}@{rev}/{icon}"
        return None
