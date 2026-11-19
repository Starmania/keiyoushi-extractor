"""Adapter for manga-download/haruneko.

Each website connector is one TypeScript file, `web/src/engine/websites/<Name>.ts`, next to its icon
(`<Name>.webp`), its end-to-end test (`<Name>_e2e.ts`) and helpers (`<Name>.DRM.js`, `<Name>.proto`...).
Shared code lives in `templates/` (base classes and helpers) and `decorators/`; those are the themes.

Metadata comes from the connector's own source, read with the TypeScript compiler API by
`haruneko_meta.cjs`: the `super(id, title, url, ...tags)` call of its default-exported class.
"""

import json
import logging
import os
import posixpath
import sqlite3
import subprocess
from importlib.resources import files

from keiex.origins.base import OriginAdapter

log = logging.getLogger(__name__)

ROOT = "web/src/engine/websites"
THEME_DIRS = ("templates", "decorators")
META_SCRIPT = files("keiex.origins").joinpath("haruneko_meta.cjs")
TYPESCRIPT = "typescript@5.9.3"


def _split(path: str) -> tuple[str | None, str] | None:
    """`ROOT/[templates|decorators/]<file>` -> (theme dir or None, file name)."""
    if not path.startswith(ROOT + "/"):
        return None
    parts = path[len(ROOT) + 1:].split("/")
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2 and parts[0] in THEME_DIRS:
        return parts[0], parts[1]
    return None  # legacy/, _mocks_/


def _folder(subdir: str | None, stem: str) -> str:
    return "/".join(p for p in (ROOT, subdir, stem) if p)


def folder_of(path: str) -> str | None:
    """Every file of a connector maps to it: Foo.ts, Foo.webp, Foo_e2e.ts, Foo_proto.ts, Foo.DRM.js -> ROOT/Foo."""
    if not (split := _split(path)):
        return None
    subdir, name = split
    stem = name.split(".", 1)[0].removesuffix("_e2e").removesuffix("_proto")
    return None if not stem or stem.startswith("_") else _folder(subdir, stem)


def connector_file_folder(path: str) -> str | None:
    """Only the connector's own source defines its metadata: Foo.ts, not Foo_e2e.ts or Foo.DRM.d.ts."""
    if not (split := _split(path)):
        return None
    subdir, name = split
    stem, dot, ext = name.partition(".")
    if ext != "ts" or not stem or stem.startswith("_") or stem.endswith(("_e2e", "_proto")):
        return None
    return _folder(subdir, stem)


class HarunekoOrigin(OriginAdapter):
    METADATA_ROOTS = (ROOT,)
    folder_of = staticmethod(folder_of)
    metadata_file_folder = staticmethod(connector_file_folder)

    def evaluate(self, pending: list[sqlite3.Row]) -> None:
        typescript = self._typescript()
        lines = "".join(
            json.dumps({"blob": r["blob"], "path": r["path"] + ".ts", "text": self.repo.cat_blob(r["blob"]).decode("utf-8", "replace")}) + "\n"
            for r in pending
        )
        proc = subprocess.run(
            ["node", str(META_SCRIPT)], input=lines, capture_output=True, text=True, check=True,
            env=os.environ | {"KEIEX_TYPESCRIPT": str(typescript)},
        )
        failed = 0
        for line in proc.stdout.splitlines():
            out = json.loads(line)
            failed += "meta" not in out
            self.store(out["blob"], out.get("meta"), out.get("error"))
        self.conn.commit()
        log.info("%s: %d connector sources read, %d without metadata", self.origin, len(pending), failed)

    def _typescript(self):
        """The TypeScript compiler, installed once into the work directory."""
        prefix = self.work_dir / "node"
        module = prefix / "node_modules" / "typescript"
        if not module.exists():
            log.info("installing %s", TYPESCRIPT)
            subprocess.run(["npm", "install", "--prefix", str(prefix), "--no-save", "--silent", TYPESCRIPT], check=True)
        return module

    def default_kind(self, path: str) -> str:
        return "theme" if path.split("/")[len(ROOT.split("/"))] in THEME_DIRS else "src"

    def prepare_derive(self) -> None:
        self._head_files = set(self.repo.tree_entries(f"origin/{self.branch}", ROOT))

    def folder_info(self, path: str, kind: str, meta: dict, removed_sha: str | None) -> dict:
        rev = self.repo.rev_parse(f"{removed_sha}^") if removed_sha else self.branch
        module = path.rsplit("/", 1)[1]
        info = {"lang": None, "module": module, "source_url": f"https://github.com/{self.origin}/blob/{rev}/{path}.ts"}
        if kind == "theme":
            return info
        files = set(self.repo.tree_entries(rev, ROOT)) if removed_sha else self._head_files
        # The icon import is relative to the connector's directory, e.g. "./OtherSite.webp".
        icon = posixpath.normpath(posixpath.join(posixpath.dirname(path), meta.get("icon") or f"./{module}.webp"))
        sources = meta.get("sources") or [{}]
        return info | {
            "lang": sources[0].get("lang"),
            "theme_path": f"{ROOT}/{meta['theme']}" if meta.get("theme") else None,
            "icon_url": f"https://cdn.jsdelivr.net/gh/{self.origin}@{rev}/{icon}" if icon in files else None,
        }
