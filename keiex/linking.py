"""Links issues to extension/theme folders. Rebuilt from scratch each run; manual overrides live in the site's own database."""

import logging
import sqlite3
from collections import defaultdict
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

log = logging.getLogger(__name__)

md = MarkdownIt("commonmark", {"linkify": True}).enable("linkify")

# PRs touching more folders than this are sweeping migrations, not fixes for the issues they close.
MAX_PR_FOLDERS = 3

LANGUAGE_NAMES = {
    "english": "en", "french": "fr", "français": "fr", "spanish": "es", "español": "es",
    "portuguese": "pt", "portuguese (brazil)": "pt-BR", "brazilian portuguese": "pt-BR",
    "indonesian": "id", "arabic": "ar", "turkish": "tr", "russian": "ru", "japanese": "ja",
    "korean": "ko", "chinese": "zh", "vietnamese": "vi", "thai": "th", "german": "de",
    "italian": "it", "polish": "pl", "ukrainian": "uk", "multi": "all", "all": "all",
}


def normalize(name: str) -> str:
    return "".join(c for c in name.casefold() if c.isalnum())


def is_version(word: str) -> bool:
    w = word.casefold().removeprefix("v")
    return bool(w) and all(c.isdigit() or c == "." for c in w) and any(c.isdigit() for c in w)


def name_candidates(text: str) -> list[str]:
    """"Not Real Scans 1.6.1 (EN)" -> ["notrealscans", ...]: longest prefix first, versions dropped."""
    words = [w for w in text.replace("(", " ").replace(")", " ").split() if not is_version(w)]
    return [n for i in range(len(words), 0, -1) if (n := normalize(" ".join(words[:i])))]


def form_sections(body: str) -> dict[str, str]:
    """Issue-form answers: "### <label>" headings followed by their text."""
    sections: dict[str, list[str]] = {}
    current = None
    tokens = md.parse(body or "")
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            current = tokens[i + 1].content.strip().casefold()
            sections[current] = []
        elif tok.type == "inline" and current and tokens[i - 1].type != "heading_open":
            sections[current].append(tok.content)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def body_hosts(body: str) -> set[str]:
    hosts = set()
    stack = md.parse(body or "")
    while stack:
        tok = stack.pop()
        stack.extend(tok.children or [])
        if tok.type == "link_open" and (h := urlsplit(tok.attrs.get("href", "")).hostname):
            hosts.add(h.lower())
    return hosts


def title_names(title: str) -> tuple[list[str], list[str]]:
    """Names reporters put up front, as (leading groups, name before a separator).

    "[Asura Scans] ..." -> (["Asura Scans"], []); "(Mangabox) Mangakakalot: ..." -> (["Mangabox"], ["Mangakakalot"]).
    Leading groups often name the theme rather than the extension.
    """
    t = title.strip()
    groups = []
    while t[:1] in ("[", "(") and (end := t.find("]" if t[0] == "[" else ")")) > 0:
        groups.append(t[1:end])
        t = t[end + 1:].lstrip()
    names = [t.split(sep, 1)[0] for sep in (":", " - ", " | ") if sep in t]
    return groups, names


class Index:
    def __init__(self, conn: sqlite3.Connection, origin: str):
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.lang: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute("SELECT path, name, module, lang FROM extension WHERE origin = ? AND kind = 'src'", (origin,)):
            for n in (r["name"], r["module"]):
                self.by_name[normalize(n)].add(r["path"])
            self.lang[r["path"]].add(r["lang"])
        self.themes = {
            normalize(r["module"]): r["path"]
            for r in conn.execute("SELECT path, module FROM extension WHERE origin = ? AND kind = 'theme'", (origin,))
        }
        for r in conn.execute("SELECT path, name, lang FROM source WHERE origin = ?", (origin,)):
            self.by_name[normalize(r["name"])].add(r["path"])
            self.lang[r["path"]].add(r["lang"])
        self.by_host: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute("SELECT DISTINCT path, host FROM domain_usage WHERE origin = ?", (origin,)):
            self.by_host[r["host"]].add(r["path"])

    def match_name(self, text: str, language: str | None, exact: bool = False) -> set[str]:
        """Longest matching prefix of `text`, or only the whole of it (versions dropped) when `exact`."""
        candidates = name_candidates(text)
        for cand in candidates[:1] if exact else candidates:
            if paths := self.by_name.get(cand):
                code = LANGUAGE_NAMES.get((language or "").strip().casefold())
                narrowed = {p for p in paths if code and (code in self.lang[p] or "all" in self.lang[p])}
                return narrowed or paths
        return set()

    def match_host(self, host: str) -> set[str]:
        return self.by_host.get(host) or self.by_host.get(host.removeprefix("www.")) or self.by_host.get("www." + host, set())


def link_issues(conn: sqlite3.Connection, origin: str) -> None:
    index = Index(conn, origin)
    pr_folders: dict[int, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT number, path FROM pr_folder WHERE origin = ?", (origin,)):
        pr_folders[r["number"]].append(r["path"])
    closing: dict[int, list[int]] = defaultdict(list)
    for r in conn.execute("SELECT issue, pr FROM issue_pr WHERE origin = ?", (origin,)):
        closing[r["issue"]].append(r["pr"])

    links: dict[tuple[int, str, str], float] = {}

    def add(issue: int, paths: set[str] | list[str], method: str, confidence: float):
        for p in paths:
            key = (issue, p, method)
            links[key] = max(links.get(key, 0), confidence)

    for issue in conn.execute("SELECT number, title, body FROM issue WHERE origin = ?", (origin,)):
        n = issue["number"]
        for pr in closing[n]:
            folders = pr_folders.get(pr, [])
            if 0 < len(folders) <= MAX_PR_FOLDERS:
                add(n, folders, "closing-pr", 1.0 if len(folders) == 1 else 0.8)

        for host in body_hosts(issue["body"]):
            paths = index.match_host(host)
            add(n, paths, "domain", 0.9 if len(paths) == 1 else 0.6)

        sections = form_sections(issue["body"])
        if info := sections.get("source information") or sections.get("source name"):
            paths = index.match_name(info, sections.get("source language"))
            add(n, paths, "form-name", 0.85 if len(paths) == 1 else 0.5)

        # Titles are free text, so only whole-name matches count.
        groups, names = title_names(issue["title"])
        for group in groups:
            theme = index.themes.get(normalize(group))
            paths = {theme} if theme else index.match_name(group, None, exact=True)
            if 0 < len(paths) <= MAX_PR_FOLDERS:
                add(n, paths, "title-name", 0.6)
        for name in names:
            paths = index.match_name(name, None, exact=True)
            if 0 < len(paths) <= MAX_PR_FOLDERS:
                add(n, paths, "title-name", 0.6)

    conn.execute("DELETE FROM issue_link WHERE origin = ?", (origin,))
    conn.executemany(
        "INSERT INTO issue_link VALUES (?,?,?,?,?)",
        [(origin, issue, path, method, conf) for (issue, path, method), conf in links.items()],
    )
    conn.commit()
    linked = len({k[0] for k in links})
    total = conn.execute("SELECT count(*) FROM issue WHERE origin = ?", (origin,)).fetchone()[0]
    log.info("issue links: %d links, %d/%d issues linked", len(links), linked, total)
