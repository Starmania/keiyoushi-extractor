"""Daily snapshots of the keiyoushi-monitor status and of the download collector."""

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MONITOR_URL = "https://hazptr.github.io/keiyoushi-monitor/status.json"
GRAFANA = "https://grafana.doesdev.fr"
GRAFANA_DASHBOARD = "24677f7ec4ed4172b682f9d26c04037f"
GRAFANA_EXTENSIONS_PANEL = 9
# keiyoushi-monitor app.js FROM_CODE
HISTORY_CODES = {"p": "pass", "t": "partial", "x": "fail", "m": "maybe", "u": "untestable", "n": "unsupported"}


def monitor(conn: sqlite3.Connection, origin: str) -> None:
    data = httpx.get(MONITOR_URL, timeout=60).raise_for_status().json()
    rows = []
    # History first, so today's detailed row (with ops) replaces its one-letter version.
    runs = data["history"]["runs"]
    for ext_id, codes in data["history"]["ext"].items():
        for date, code in zip(runs, codes):
            if status := HISTORY_CODES.get(code):
                rows.append((origin, f"src/{ext_id}", date, status, None, "[]"))
    for e in data["extensions"]:
        if e["status"] != "pending":
            rows.append((origin, f"src/{e['id']}", data["sweepDate"], e["status"], e.get("since"), json.dumps(e.get("ops", []))))
    conn.executemany(
        """INSERT INTO status_snapshot VALUES (?,?,?,?,?,?)
           ON CONFLICT DO UPDATE SET status = excluded.status,
             since = coalesce(excluded.since, since),
             ops = CASE WHEN excluded.ops = '[]' THEN ops ELSE excluded.ops END""",
        rows,
    )
    fill_since(conn, origin)
    conn.commit()
    log.info("monitor: %d status rows (sweep %s)", len(rows), data["sweepDate"])


def fill_since(conn: sqlite3.Connection, origin: str) -> None:
    """Gives each extension's latest row a `since` when the monitor gave none: the first day of the current run of
    that status in the history. Stored so the site doesn't walk the whole history on every request."""
    conn.execute(
        """UPDATE status_snapshot AS l SET since = (
               SELECT min(s.date) FROM status_snapshot s
               WHERE s.origin = l.origin AND s.path = l.path AND s.date > coalesce((
                   SELECT max(o.date) FROM status_snapshot o
                   WHERE o.origin = l.origin AND o.path = l.path AND o.date < l.date AND o.status != l.status
               ), ''))
           WHERE l.origin = ? AND l.since IS NULL
             AND l.date = (SELECT max(m.date) FROM status_snapshot m WHERE m.origin = l.origin AND m.path = l.path)""",
        (origin,),
    )


def downloads(conn: sqlite3.Connection, origin: str, collector_db: Path | None) -> None:
    today = datetime.now(UTC).date().isoformat()
    if collector_db:
        rows, via = from_collector(collector_db), "collector db"
    elif token := os.environ.get("GRAFANA_TOKEN"):
        rows, via = from_grafana_api(token), "Grafana API"
    else:
        # rows, via = _from_grafana(conn, origin), "public Grafana panel"
        rows, via = {}, "no collector db or Grafana token"
    conn.executemany(
        "INSERT OR REPLACE INTO download_snapshot VALUES (?,?,?,?,?,?,?,?)",
        [(origin, path, today, *counts) for path, counts in rows.items()],
    )
    conn.commit()
    log.info("downloads: %d extensions (%s)", len(rows), via)


# Per-folder counts; the collector's assets carry their src/<lang>/<module> folder.
COLLECTOR_SQL = """
WITH s AS (
  SELECT asset_id,
    sum(CASE WHEN ts > strftime('%s','now') - 3600 THEN delta ELSE 0 END) AS h1,
    sum(CASE WHEN ts > strftime('%s','now') - 86400 THEN delta ELSE 0 END) AS h24,
    sum(CASE WHEN ts > strftime('%s','now') - 604800 THEN delta ELSE 0 END) AS d7,
    sum(delta) AS d30
  FROM sample WHERE ts > strftime('%s','now') - 2592000 GROUP BY asset_id
)
SELECT a.dir_lang, a.module,
  sum(coalesce(s.h1, 0)), sum(coalesce(s.h24, 0)), sum(coalesce(s.d7, 0)), sum(coalesce(s.d30, 0)),
  sum(a.last_count)
FROM asset a LEFT JOIN s ON s.asset_id = a.id
WHERE a.dir_lang IS NOT NULL AND a.module IS NOT NULL
GROUP BY a.dir_lang, a.module
"""


def from_collector(path: Path) -> dict[str, tuple]:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return {f"src/{lang}/{module}": tuple(counts) for lang, module, *counts in src.execute(COLLECTOR_SQL)}


def from_grafana_api(token: str) -> dict[str, tuple]:
    """Runs COLLECTOR_SQL on the collector through Grafana's datasource API (service-account token)."""
    query = {"refId": "A", "datasource": {"type": "frser-sqlite-datasource", "uid": "keiyoushi"},
             "queryType": "table", "queryText": COLLECTOR_SQL, "rawQueryText": COLLECTOR_SQL}
    r = httpx.post(
        f"{GRAFANA}/api/ds/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"from": "now-1h", "to": "now", "queries": [query]},
        timeout=120,
    ).raise_for_status()
    columns = r.json()["results"]["A"]["frames"][0]["data"]["values"]
    return {f"src/{lang}/{module}": tuple(counts) for lang, module, *counts in zip(*columns)}


def _from_grafana(conn: sqlite3.Connection, origin: str) -> dict[str, tuple]:
    """Public panel fallback. It only exposes display name + language, so map those back to folders."""
    r = httpx.post(
        f"{GRAFANA}/api/public/dashboards/{GRAFANA_DASHBOARD}/panels/{GRAFANA_EXTENSIONS_PANEL}/query",
        json={"intervalMs": 3600000, "maxDataPoints": 200, "timeRange": {"from": "now-30d", "to": "now", "timezone": "utc"}},
        timeout=60,
    ).raise_for_status()
    frame = r.json()["results"]["A"]["frames"][0]
    columns = dict(zip((f["name"] for f in frame["schema"]["fields"]), frame["data"]["values"]))

    folders: dict[tuple[str, str], list[str]] = {}
    for row in conn.execute(
        "SELECT path, name, (SELECT count(DISTINCT lang) FROM source s WHERE s.origin = e.origin AND s.path = e.path) AS langs, "
        "(SELECT min(lang) FROM source s WHERE s.origin = e.origin AND s.path = e.path) AS lang "
        "FROM extension e WHERE origin = ? AND kind = 'src' AND removed_sha IS NULL",
        (origin,),
    ):
        lang = "multi" if row["langs"] > 1 else row["lang"]
        folders.setdefault((row["name"], lang), []).append(row["path"])

    out, unmatched = {}, 0
    for i, name in enumerate(columns["Extension"]):
        paths = folders.get((name, columns["Language"][i]), [])
        if len(paths) != 1:
            unmatched += 1
            continue
        out[paths[0]] = tuple(columns[c][i] for c in ("1 h", "24 h", "7 d", "30 d", "All time"))
    if unmatched:
        log.warning("downloads: %d panel rows did not map to exactly one folder", unmatched)
    return out
