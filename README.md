# keiyoushi-extractor

Builds the linker database (`data/linker.db`) that [keiyoushi-linker-site](../keiyoushi-linker-site) serves:

- **Extension metadata from Gradle itself.** `keiex/dump-metadata.init.gradle` is a Gradle init script that reads the
  evaluated `keiyoushi {}` extension of every module, including the sources the plugin resolved for KSP (ids, base URLs,
  mirrors). Loops, mirrors and pkgName overrides come out exactly as the build sees them. No regex is involved.
- **History since an anchor commit.** Every first-parent commit since the anchor, with the folders it touched
  (`src/<lang>/<module>`, `lib-multisrc/<theme>`) and its squash-merge PR number. Every build-file version is evaluated
  once per distinct blob, so the timeline also yields domain history (when a host was added or dropped).
- **GitHub PRs and issues**, fetched incrementally through GraphQL: changed folders, closing references, and issue-form
  answers.
- **Issue links**, from closing PRs, hosts in the issue body, the "Source information" answer, and title prefixes.
  Manual corrections made through the site API are kept in a separate table and never overwritten.
- **Stats**: daily [keiyoushi-monitor](https://hazptr.github.io/keiyoushi-monitor/) status (with its past runs), and
  download counts from the download collector.

## Run

```bash
uv sync
uv run keiex add-origin                    # keiyoushi/extensions-source, anchored at 7363e4298c
uv run keiex all --sdk-properties ~/dev/extensions-source/local.properties \
                 --downloads-db ~/dev/keiyoushi-download-dashboard/data/dashboard.db
```

Steps can be run on their own: `metadata`, `github`, `link`, `stats`. Every step is incremental.

## CI

`.github/workflows/refresh.yml` runs `keiex all` hourly. The database is the only state between runs: the workflow
downloads it from the `db` release, refreshes it, and uploads it back as `linker.db.gz`. On the first run there's no
release yet, so it does the full backfill. Seed the release from a local run instead, which is faster:

```bash
gzip -9 -k data/linker.db && gh release create db data/linker.db.gz --title "Linker database" --notes "Seed"
```

CI can't read the download collector's SQLite, so it runs the same SQL through Grafana's datasource API
(`POST /api/ds/query`) with a Viewer service-account token in the `GRAFANA_TOKEN` repository secret.

| option / env | meaning |
| --- | --- |
| `--db` | database path (default `data/linker.db`) |
| `--work` | clones and Gradle evaluation trees (default `work/`) |
| `--sdk-properties` | a `local.properties` with `sdk.dir`; Gradle needs it to configure Android modules |
| `--downloads-db` | the download collector's SQLite, opened read-only |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub token, public read access only. Falls back to `gh auth token` |
| `GRAFANA_TOKEN` | Grafana service-account token, used for download counts when `--downloads-db` is not given |

Download counts come from the first source available: `--downloads-db`, then `GRAFANA_TOKEN`, then the public panel
(which only maps rows by display name and language, so some rows are skipped).

## How metadata evaluation stays cheap

Metadata depends only on a folder's `build.gradle.kts`. Each pending blob is written into a scratch tree that holds the
current build infrastructure, about 250 folders per Gradle configuration run (`gradlew help`, nothing compiled). A
folder's successive versions go into different batches. A blob that fails against the current infrastructure (for
example one that references a `lib/` module removed since) is re-evaluated against the infrastructure of its own commit.

## Adding another repository

Each row carries an `origin` (`owner/repo`), and `origin.kind` selects the adapter. Only `keiyoushi-gradle` exists
today (`keiex/origins/keiyoushi.py`). A repo with a different layout needs its own adapter that fills the same tables:
`git_commit`, `commit_folder`, `folder_version`/`blob_metadata` → `extension`, `source` and `domain_usage`.
