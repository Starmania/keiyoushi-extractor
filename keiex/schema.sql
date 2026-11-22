-- Every row is scoped by `origin` ("owner/repo"), so repos with another layout can be added later.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS origin (
    id             TEXT PRIMARY KEY,        -- "keiyoushi/extensions-source"
    kind           TEXT NOT NULL,           -- adapter, e.g. "keiyoushi-gradle"
    repo_url       TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    anchor_sha     TEXT NOT NULL            -- history starts here
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS git_commit (
    origin     TEXT NOT NULL,
    sha        TEXT NOT NULL,
    seq        INTEGER NOT NULL,            -- 0 = anchor, increasing along first-parent history
    date       TEXT NOT NULL,               -- committer date, ISO 8601
    author     TEXT,
    subject    TEXT NOT NULL,
    body       TEXT,
    pr_number  INTEGER,
    PRIMARY KEY (origin, sha)
);
CREATE INDEX IF NOT EXISTS git_commit_seq ON git_commit(origin, seq);
CREATE INDEX IF NOT EXISTS git_commit_pr ON git_commit(origin, pr_number);

-- Folder = "src/<lang>/<module>" or "lib-multisrc/<theme>".
CREATE TABLE IF NOT EXISTS commit_folder (
    origin TEXT NOT NULL,
    sha    TEXT NOT NULL,
    path   TEXT NOT NULL,
    PRIMARY KEY (origin, sha, path)
);
CREATE INDEX IF NOT EXISTS commit_folder_path ON commit_folder(origin, path);

-- Evaluated build file, cached by blob: identical files are evaluated once.
CREATE TABLE IF NOT EXISTS blob_metadata (
    blob     TEXT PRIMARY KEY,
    json     TEXT,                          -- init-script output line, NULL when evaluation failed
    error    TEXT
);

-- Timeline of each folder's build file: one row per commit that changed it.
CREATE TABLE IF NOT EXISTS folder_version (
    origin TEXT NOT NULL,
    path   TEXT NOT NULL,
    sha    TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    blob   TEXT,                            -- NULL = folder removed at this commit
    PRIMARY KEY (origin, path, sha)
);
CREATE INDEX IF NOT EXISTS folder_version_seq ON folder_version(origin, path, seq);

-- Latest known state of every folder, derived from folder_version + blob_metadata.
CREATE TABLE IF NOT EXISTS extension (
    origin          TEXT NOT NULL,
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- "src" | "theme"
    lang            TEXT,
    module          TEXT NOT NULL,
    name            TEXT,
    package_name    TEXT,
    theme           TEXT,                   -- theme display name
    theme_path      TEXT,                   -- theme folder, e.g. "lib-multisrc/<theme>"
    content_warning TEXT,
    lib_version     TEXT,
    version_code    INTEGER,
    icon_url        TEXT,
    source_url      TEXT,                   -- where to browse the folder's code
    first_sha       TEXT,
    last_sha        TEXT,                   -- last commit that changed the build file
    removed_sha     TEXT,
    PRIMARY KEY (origin, path)
);
CREATE INDEX IF NOT EXISTS extension_theme ON extension(origin, theme);
CREATE INDEX IF NOT EXISTS extension_pkg ON extension(package_name);

CREATE TABLE IF NOT EXISTS source (
    origin      TEXT NOT NULL,
    path        TEXT NOT NULL,
    source_id   TEXT NOT NULL,              -- 64-bit id kept as text: JS cannot hold it as a number
    stable_id   TEXT,                       -- see source_host
    name        TEXT NOT NULL,
    lang        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    url_type    TEXT NOT NULL,              -- "static" | "mirrors" | "custom"
    mirror_urls TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (origin, path, source_id)
);
CREATE INDEX IF NOT EXISTS source_id ON source(source_id);

-- Every host an extension used, with the commit range it was declared in.
CREATE TABLE IF NOT EXISTS domain_usage (
    origin   TEXT NOT NULL,
    path     TEXT NOT NULL,
    host     TEXT NOT NULL,
    role     TEXT NOT NULL,                 -- "base" | "mirror" | "deeplink"
    from_sha TEXT NOT NULL,
    to_sha   TEXT,                          -- NULL = still declared
    PRIMARY KEY (origin, path, host, role, from_sha)
);
CREATE INDEX IF NOT EXISTS domain_usage_host ON domain_usage(host);

-- Base and mirror hosts per source, keyed by a stable id that survives what the origin treats as the same source
-- (keiyoushi: a versionId bump). Hosts are normalized for matching across origins: lowercase, no "www."/"m.".
CREATE TABLE IF NOT EXISTS source_host (
    origin    TEXT NOT NULL,
    stable_id TEXT NOT NULL,
    path      TEXT NOT NULL,
    name      TEXT NOT NULL,
    lang      TEXT,
    host      TEXT NOT NULL,
    from_sha  TEXT NOT NULL,
    to_sha    TEXT,                         -- NULL = still declared
    PRIMARY KEY (origin, stable_id, host, from_sha)
);
CREATE INDEX IF NOT EXISTS source_host_host ON source_host(host);

-- Sources of two origins that declared the same host at the same time: the same site. Rebuilt each run from
-- source_host, so a link stays once its evidence is in the history, whatever either side declares later.
CREATE TABLE IF NOT EXISTS source_link (
    origin_a  TEXT NOT NULL,                -- origin_a < origin_b
    stable_a  TEXT NOT NULL,
    origin_b  TEXT NOT NULL,
    stable_b  TEXT NOT NULL,
    host      TEXT NOT NULL,                -- first shared host
    since     TEXT NOT NULL,                -- when both started declaring it
    PRIMARY KEY (origin_a, stable_a, origin_b, stable_b)
);
CREATE INDEX IF NOT EXISTS source_link_b ON source_link(origin_b, stable_b);

CREATE TABLE IF NOT EXISTS pull_request (
    origin     TEXT NOT NULL,
    number     INTEGER NOT NULL,
    title      TEXT NOT NULL,
    state      TEXT NOT NULL,               -- OPEN | CLOSED | MERGED
    author     TEXT,
    created_at TEXT NOT NULL,
    merged_at  TEXT,
    closed_at  TEXT,
    body       TEXT,
    single_target TEXT,                     -- the one src/ or lib-multisrc/ folder it touches, else NULL
    PRIMARY KEY (origin, number)
);

CREATE TABLE IF NOT EXISTS pr_folder (
    origin TEXT NOT NULL,
    number INTEGER NOT NULL,
    path   TEXT NOT NULL,
    PRIMARY KEY (origin, number, path)
);
CREATE INDEX IF NOT EXISTS pr_folder_path ON pr_folder(origin, path);

CREATE TABLE IF NOT EXISTS issue (
    origin     TEXT NOT NULL,
    number     INTEGER NOT NULL,
    title      TEXT NOT NULL,
    state      TEXT NOT NULL,
    author     TEXT,
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    body       TEXT,
    labels     TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (origin, number)
);

CREATE TABLE IF NOT EXISTS issue_pr (
    origin TEXT NOT NULL,
    issue  INTEGER NOT NULL,
    pr     INTEGER NOT NULL,
    PRIMARY KEY (origin, issue, pr)
);
CREATE INDEX IF NOT EXISTS issue_pr_pr ON issue_pr(origin, pr);

-- Automatic links, recomputed on every run.
CREATE TABLE IF NOT EXISTS issue_link (
    origin     TEXT NOT NULL,
    issue      INTEGER NOT NULL,
    path       TEXT NOT NULL,
    method     TEXT NOT NULL,               -- "closing-pr" | "domain" | "form-name" | "title-name"
    confidence REAL NOT NULL,
    PRIMARY KEY (origin, issue, path, method)
);
CREATE INDEX IF NOT EXISTS issue_link_path ON issue_link(origin, path);

CREATE TABLE IF NOT EXISTS status_snapshot (
    origin TEXT NOT NULL,
    path   TEXT NOT NULL,
    date   TEXT NOT NULL,                   -- monitor sweep date
    status TEXT NOT NULL,
    since  TEXT,
    ops    TEXT NOT NULL DEFAULT '[]',
    error    TEXT,                          -- the monitor's scrubbed error or fixed recheck sentence
    notes    TEXT,                          -- JSON list of plain-language notes; NULL on history-only rows
    rechecks TEXT,                          -- JSON list of proxy tries {from, status, ops, step?, tags?, ticked?}
    PRIMARY KEY (origin, path, date)
);

CREATE TABLE IF NOT EXISTS download_snapshot (
    origin   TEXT NOT NULL,
    path     TEXT NOT NULL,
    date     TEXT NOT NULL,                 -- UTC day of the reading
    h1       INTEGER,
    h24      INTEGER,
    d7       INTEGER,
    d30      INTEGER,
    all_time INTEGER,
    PRIMARY KEY (origin, path, date)
);
