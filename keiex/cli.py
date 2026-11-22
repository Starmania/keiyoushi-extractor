import argparse
import logging
from pathlib import Path

from keiex import db
from keiex.git import Repo

KEIYOUSHI = "keiyoushi/extensions-source"
# Known repos: adapter, default branch, and the commit history starts at.
PRESETS = {
    KEIYOUSHI: ("keiyoushi-gradle", "main", "7363e4298c2a6cc4e32e6ef6af2d48611d5b619e"),
    # The connectors moved to web/src/engine/websites in this commit.
    "manga-download/haruneko": ("haruneko-ts", "master", "d16720e53f1f83f61f23200c81fb82d93599db9e"),
}


def adapters():
    from keiex.origins.haruneko import HarunekoOrigin
    from keiex.origins.keiyoushi import KeiyoushiOrigin

    return {"keiyoushi-gradle": KeiyoushiOrigin, "haruneko-ts": HarunekoOrigin}


def origin_repo(work: Path, origin: str) -> Repo:
    """The origin's clone, created on first use (CI starts without one)."""
    repo = Repo(work / origin.replace("/", "__"))
    if not repo.path.exists():
        work.mkdir(parents=True, exist_ok=True)
        Repo(work).run("clone", "--quiet", "--no-checkout", f"https://github.com/{origin}", str(repo.path))
    return repo


def cmd_add_origin(args, conn):
    kind, branch, anchor = PRESETS.get(args.origin, (None, None, None))
    args.kind, args.branch, args.anchor = args.kind or kind, args.branch or branch, args.anchor or anchor
    if not (args.kind and args.branch and args.anchor):
        raise SystemExit(f"{args.origin} has no preset: pass --kind, --branch and --anchor")
    if args.kind not in adapters():
        raise SystemExit(f"unknown adapter {args.kind}; known: {sorted(adapters())}")
    origin_repo(args.work, args.origin)
    conn.execute(
        "INSERT OR REPLACE INTO origin VALUES (?,?,?,?,?)",
        (args.origin, args.kind, f"https://github.com/{args.origin}", args.branch, args.anchor),
    )
    conn.commit()


def origins(conn, only: str | None):
    rows = conn.execute("SELECT * FROM origin").fetchall()
    return [r for r in rows if only in (None, r["id"])]


def adapter(args, conn, o):
    return adapters()[o["kind"]](conn, o["id"], origin_repo(args.work, o["id"]), args.work,
                                 sdk_properties=getattr(args, "sdk_properties", None))


def cmd_metadata(args, conn):
    for o in origins(conn, args.origin):
        adapter(args, conn, o).run()


def cmd_github(args, conn):
    from keiex.github import ingest

    for o in origins(conn, args.origin):
        ingest(conn, o["id"], adapter(args, conn, o).folder_of)


def cmd_link(args, conn):
    from keiex.linking import link_issues

    from keiex.sitelinks import link_sources

    for o in origins(conn, args.origin):
        link_issues(conn, o["id"])
    link_sources(conn)


def cmd_stats(args, conn):
    from keiex import stats

    stats.monitor(conn, KEIYOUSHI)
    stats.apk_urls(conn, KEIYOUSHI)
    stats.downloads(conn, KEIYOUSHI, args.downloads_db)


def cmd_all(args, conn):
    cmd_metadata(args, conn)
    cmd_github(args, conn)
    cmd_link(args, conn)
    cmd_stats(args, conn)


def main():
    p = argparse.ArgumentParser(prog="keiex")
    p.add_argument("--db", type=Path, default=Path("data/linker.db"))
    p.add_argument("--work", type=Path, default=Path("work"), help="clones and Gradle evaluation trees")
    p.add_argument("--origin", help="limit to one origin")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-origin", help="register a repo to track")
    a.add_argument("origin", nargs="?", default=KEIYOUSHI)
    a.add_argument("--kind", help="adapter; preset for known repos")
    a.add_argument("--branch", help="default branch; preset for known repos")
    a.add_argument("--anchor", help="first commit of the tracked history; preset for known repos")
    a.set_defaults(fn=cmd_add_origin)

    gradle_args = argparse.ArgumentParser(add_help=False)
    gradle_args.add_argument("--sdk-properties", type=Path, help="local.properties with sdk.dir for Gradle")
    stats_args = argparse.ArgumentParser(add_help=False)
    stats_args.add_argument("--downloads-db", type=Path, help="download collector SQLite; public Grafana panel otherwise")

    sub.add_parser("metadata", parents=[gradle_args], help="git history + Gradle-evaluated metadata").set_defaults(fn=cmd_metadata)
    sub.add_parser("github", help="PRs and issues").set_defaults(fn=cmd_github)
    sub.add_parser("link", help="recompute issue links and cross-origin source links").set_defaults(fn=cmd_link)
    sub.add_parser("stats", parents=[stats_args], help="monitor status and download counts").set_defaults(fn=cmd_stats)
    sub.add_parser("all", parents=[gradle_args, stats_args], help="everything, in order").set_defaults(fn=cmd_all)

    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.work = args.work.resolve()
    conn = db.connect(args.db)
    args.fn(args, conn)


if __name__ == "__main__":
    main()
