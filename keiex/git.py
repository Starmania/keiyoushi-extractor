import subprocess
from pathlib import Path


class Repo:
    def __init__(self, path: Path):
        self.path = path

    def run(self, *args: str, input: bytes | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            input=input,
        ).stdout.decode("utf-8", "replace")

    def rev_parse(self, rev: str) -> str:
        return self.run("rev-parse", rev).strip()

    def first_parent_commits(self, anchor: str, head: str) -> list[str]:
        """Anchor first, then every first-parent commit up to head."""
        return [anchor] + self.run("rev-list", "--reverse", "--first-parent", f"{anchor}..{head}").split()

    def tree_entries(self, rev: str, *paths: str) -> dict[str, str]:
        """path -> blob sha for every file under `paths` at `rev`."""
        out = self.run("ls-tree", "-r", "-z", rev, "--", *paths)
        entries = {}
        for record in out.split("\0"):
            if not record:
                continue
            meta, path = record.split("\t", 1)
            entries[path] = meta.split()[2]
        return entries

    def cat_blob(self, blob: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.path), "cat-file", "blob", blob], check=True, capture_output=True
        ).stdout
