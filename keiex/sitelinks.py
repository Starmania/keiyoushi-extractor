"""Links sources across origins: two sources that declared the same host at the same time are the same site.

Stable ids don't change for the same source, so once linked the pair stays linked whatever either side declares
later; that is what makes domain drift visible. Rebuilt from source_host on every run.
"""

import logging
import sqlite3
from collections import defaultdict
from typing import NamedTuple

log = logging.getLogger(__name__)

FOREVER = "9999"


class Range(NamedTuple):
    origin: str
    stable_id: str
    lang: str | None
    start: str  # ISO dates, compared as text
    end: str


def primary_lang(lang: str | None) -> str | None:
    return lang.split("-")[0].lower() if lang else None


def overlapping_pairs(ranges_by_host: dict[str, list[Range]]) -> dict[tuple[Range, Range], tuple[str, str]]:
    """(a, b) -> (host, since) for ranges of different origins on one host that overlap in time; a.origin < b.origin.

    Keeps the earliest shared host per pair. Ranges carry only identity, so equal (origin, id) ranges collapse.
    """
    out: dict[tuple[tuple, tuple], tuple[str, str]] = {}
    for host, ranges in ranges_by_host.items():
        for a in ranges:
            for b in ranges:
                if a.origin >= b.origin or not (a.start < b.end and b.start < a.end):
                    continue
                key = ((a.origin, a.stable_id, a.lang), (b.origin, b.stable_id, b.lang))
                since = max(a.start, b.start)
                if key not in out or since < out[key][1]:
                    out[key] = (host, since)
    return out


def prefer_same_lang(pairs: dict) -> dict:
    """A multi-language site (one host, a source per language) matches every language of the other side;
    when a source has candidates in its own language, keep only those."""
    for side in (0, 1):
        other = 1 - side
        by_source: dict[tuple, list[tuple]] = defaultdict(list)
        for key in pairs:
            by_source[(key[side][0], key[side][1], key[other][0])].append(key)
        for keys in by_source.values():
            same = [k for k in keys if primary_lang(k[0][2]) and primary_lang(k[0][2]) == primary_lang(k[1][2])]
            if same and len(same) < len(keys):
                for k in keys:
                    if k not in same:
                        pairs.pop(k, None)
    return pairs


def link_sources(conn: sqlite3.Connection) -> None:
    ranges_by_host: dict[str, list[Range]] = defaultdict(list)
    for r in conn.execute(
        """SELECT sh.origin, sh.stable_id, sh.lang, sh.host, fc.date AS start, tc.date AS "end"
           FROM source_host sh
           JOIN git_commit fc ON fc.origin = sh.origin AND fc.sha = sh.from_sha
           LEFT JOIN git_commit tc ON tc.origin = sh.origin AND tc.sha = sh.to_sha"""
    ):
        ranges_by_host[r["host"]].append(Range(r["origin"], r["stable_id"], r["lang"], r["start"], r["end"] or FOREVER))
    pairs = prefer_same_lang(overlapping_pairs(ranges_by_host))
    conn.execute("DELETE FROM source_link")
    conn.executemany(
        "INSERT INTO source_link VALUES (?,?,?,?,?,?)",
        [(a[0], a[1], b[0], b[1], host, since) for (a, b), (host, since) in pairs.items()],
    )
    conn.commit()
    log.info("source links: %d", len(pairs))
