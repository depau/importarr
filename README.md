# importarr

Sidecar for [Lidarr](https://lidarr.audio) that handles failed auto-imports: retries the
transient ones, commits the clean ones, and hands the messy ones to an LLM agent that can
do a manual import (track assignment), or remove + blacklist the release.

## How it works

Lidarr's **Import Failed** webhook (`eventType: ImportFailure`, fired after a
download completes but the import can't be applied) is pointed at `POST /webhook`.
For each event:

1. **Probe** `GET /api/v1/manualimport` for the download's folder and classify the
   candidates' rejection reasons:
   - `clean` — everything maps: commit via `ManualImport` command (auto-imported).
   - `timing` — no candidates yet (files still landing): retry with backoff
     (`RETRY_BACKOFFS`, default `30,120,600`).
   - `mapping` — candidates exist but tracks/album don't match (the "manual import"
     cases: misassigned track numbers, unknown artist, partial albums).
   - `env` — disk/permission errors: notify and stop, the LLM won't fix your volume.
2. **Agent** (mapping cases, or after retries exhaust): an OpenAI-compatible LLM
   runs a tool-calling loop against the Lidarr REST API with real data as context:
   - `manualimport_get` / `update_manual_import` — probe and re-assign files/albums/tracks
   - `commit_import` — send the `ManualImport` command
   - `get_artist` / `artist_albums` / `get_album` / `history` — context for decisions
   - `inspect_tags` — read a candidate file's audio metadata (title/artist/album
     tags, length) to identify misassigned files
   - `remove_release` — delete the queue item with `blocklist` + `skipRedownload`
   - `finish` — report `imported` / `removed` / `gave_up` with a summary
3. **Notify** every final outcome via [Apprise](https://github.com/caronc/apprise)
   (ntfy, Telegram, Discord, …).

**Dry-run is the default.** With `DRY_RUN=true` (default) every mutation is logged and
notified, not executed. Read the logs for a while, then set `DRY_RUN=false`.

## Config

| Env | Required | Default | Description |
|---|---|---|---|
| `LIDARR_URL` | yes | — | e.g. `http://host.docker.internal:8686` |
| `LIDARR_API_KEY` | yes | — | Lidarr API key |
| `LLM_BASE_URL` | no | — | OpenAI-compatible base, e.g. `http://host.docker.internal:11434/v1` (ollama, vLLM, OpenRouter, …). Unset = agent disabled, ladder only. |
| `LLM_API_KEY` | no | `""` | |
| `LLM_MODEL` | no | — | model name |
| `LLM_MAX_ROUNDS` | no | `15` | agent tool-loop cap |
| `DRY_RUN` | no | `true` | `false` = execute mutations |
| `AUDIO_PATHS` | no | — | comma-separated roots the agent may read audio files from (e.g. your download folder). Unset = `inspect_tags` disabled. |
| `APPRISE_URL` | no | — | Apprise URL (or comma-separated list) |
| `LOG_LEVEL` | no | `INFO` | Application log level; agent tool arguments and summaries are logged in full. httpx logs warnings and errors only. |
| `PORT` | no | `8323` | webhook port |

## Lidarr webhook setup

In Lidarr: **Settings → Connect → Webhook**, create a new one with URL
`http://<importarr-host>:8323/webhook` and enable the **On Import Failure** trigger
(optionally *On Download Failure* for releases that die in the download client).
The payload includes `trackFiles` with the audio file paths, which the app uses to
locate the download folder.

## Run

```sh
# local
pip install .
LIDARR_URL=... LIDARR_API_KEY=... importarr

# docker (image from GHCR; `docker build -t importarr .` works too)
docker run -d --name importarr \
  -e LIDARR_URL=http://host.docker.internal:8686 \
  -e LIDARR_API_KEY=$LIDARR_API_KEY \
  -e LLM_BASE_URL=http://host.docker.internal:11434/v1 \
  -e LLM_MODEL=qwen3 \
  -p 8323:8323 ghcr.io/depau/importarr:latest
```

Pulling from GHCR needs a token: `docker login ghcr.io` with a GitHub PAT
scoped `read:packages`.

`docker-compose.example.yml` has a full example.

## Health

`GET /health` → `{"ok": true}` (also used as the Docker HEALTHCHECK).
 
## Takeout import (CLI)

Import a mixed dump with an OpenAI-compatible model configured. Mount the dump
at the same path in both containers, and include it in `AUDIO_PATHS`:

```sh
docker compose exec importarr importarr import /data/library/music/import
```

The CLI reads each file's tags and uses the agent to match the artist, album
and track against Lidarr's library. It probes each file separately for import
metadata, not the dump folder's name. Files it cannot identify are reported
without blocking the rest. The source files are copied or hardlinked, not moved.
The artist and album must already exist in Lidarr. Individual songs can be
imported even if Lidarr reports `Has missing tracks`; other rejections still block.
An existing track is never overwritten, except when the incoming file is FLAC
and the existing one is a lossy format. These checks read Lidarr's live state
at commit time, so a check at import time doesn't guarantee the state stays.
A file that is already the library's track file for its mapped track is skipped
before the LLM runs.
After submitted imports finish, the CLI searches each affected album that still
has missing tracks once. Dry runs do not submit searches. Import commands that
fail, cannot be checked, or are still pending after five minutes are skipped.

`DRY_RUN=true` is the default. To submit imports without changing the service:

```sh
docker compose exec -e DRY_RUN=false importarr importarr import /data/library/music/import
```

A submitted command is not confirmation that Lidarr completed it.


## Tests

```sh
python tests/test_triage.py   # classifier
python tests/test_e2e.py      # fake Lidarr + fake LLM, full flow (dry-run, commit, removal)
python tests/test_takeout.py  # fake Lidarr + fake LLM, CLI folder import
```

## Notes

- Deduped on `downloadId|folder` for one hour; at-least-once safe: the flow re-probes
  the live manualimport state before committing, so a release Lidarr imported in the
  meantime can't be double-imported (Lidarr rejects already-imported paths).
- The `remove_release` action blocks re-download; without it Lidarr can re-grab the
  same release on the next search.
- **Tag safety:** file tags are untrusted input. They are truncated before reaching
  the model and the system prompt declares tool results data, not instructions. The
  container's file access is limited to `AUDIO_PATHS` (realpath-checked), so the
  agent cannot read outside your mounted music/download root.
- Built and verified against Lidarr v3.2.x's API v1 surface.
