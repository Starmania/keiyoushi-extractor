# keiyoushi-extractor

Builds the linker database (`data/linker.db`) that [keiyoushi-linker-site](../keiyoushi-linker-site) serves, for
several extension repositories. Each repository is an *origin* (`owner/repo`) read by an adapter for its layout:

| origin | adapter | extension folder | theme folder | metadata read from |
| --- | --- | --- | --- | --- |
| `keiyoushi/extensions-source` | `keiyoushi-gradle` | `src/<lang>/<module>` | `lib-multisrc/<theme>` | Gradle itself: `keiex/dump-metadata.init.gradle` reads the evaluated `keiyoushi {}` extension, sources and ids included |
| `manga-download/haruneko` | `haruneko-ts` | `web/src/engine/websites/<Name>` | `…/templates/<Name>`, `…/decorators/<Name>`, or a parent connector | the TypeScript compiler API (`keiex/origins/haruneko_meta.cjs`): the connector class's `super(id, title, url, ...tags)` call, decorators and imports |

Neither adapter uses regex on source code. For every origin, the extractor collects:

- **History since an anchor commit.** Every first-parent commit with the folders it touched and its squash-merge PR number. Each
  distinct version of a metadata file (build file, connector source) is evaluated once, so the timeline also yields
  domain history (when a host was added or dropped).
- **GitHub PRs and issues**, fetched incrementally through GraphQL: changed folders, closing references, and issue-form
  answers.
- **Issue links**, from closing PRs, hosts in the issue body (only while a folder declared them), the "Source
  information" answer, and names at the start of the title. Manual corrections live in the site's own database.
- **Stats** (keiyoushi only): daily [keiyoushi-monitor](https://hazptr.github.io/keiyoushi-monitor/) status, with its
  past runs, and download counts from the download collector.

## Run

```bash
uv sync
uv run keiex add-origin keiyoushi/extensions-source   # presets: adapter, branch, anchor 7363e4298c
uv run keiex add-origin manga-download/haruneko       # anchor d16720e53, when connectors moved to web/src/engine/websites
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

`origin.kind` selects the adapter (`keiex/origins/`). An adapter subclasses `OriginAdapter` (`base.py`) and provides:

- `folder_of(path)`: the extension or theme folder that owns a repo file (used for commits and PRs)
- `metadata_file_folder(path)` and `METADATA_ROOTS`: which files define a folder's metadata
- `evaluate(pending)`: turns those files into the shared metadata shape documented in `base.py`
- `folder_info(...)`: layout-specific columns such as the language, theme folder, icon and source URL

The base class handles commits, the metadata timeline, sources, domain history and the derived tables. A repo with a
known layout gets a preset in `keiex/cli.py`; otherwise pass `--kind`, `--branch` and `--anchor` to `add-origin`.
