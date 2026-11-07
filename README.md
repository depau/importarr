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
| `LOG_LEVEL` | no | `INFO` | |
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

For audio folders that were never downloaded through Lidarr (takeouts, copies),
import them via Lidarr's manual-import engine instead:

```sh
docker exec importarr importarr import /music/Takeout/...
```

No download is involved. For each path the app probes `manualimport` and
partitions every candidate file:

| Bucket | Detection | Action |
|---|---|---|
| `clean` | no rejections, artist+album+tracks resolved | committed directly (no LLM) |
| `agent` | fixable rejections (misassigned tracks, wrong album, …), artist resolved | LLM agent in folder mode: re-assign + `commit_import` |
| `env` | environmental rejections (disk, permissions, not in a root folder) | reported, never touched |
| `unresolved` | no artist (or no track) in the folder | reported for you — see constraint below |

Same `DRY_RUN` gate as the webhook flow: default logs + apprise what it would
commit; set `DRY_RUN=false` to commit.

**Constraint:** a file's artist (and at least one track) must already exist in
your Lidarr library. Lidarr's manual-import API resolves files to library
entities — it can't create unknown artists/albums — and the agent can't either.
Add the artist/album to Lidarr first, then re-run.


## Tests

```sh
python tests/test_triage.py   # classifier
python tests/test_e2e.py      # fake Lidarr + fake LLM, full flow (dry-run, commit, removal)
python tests/test_takeout.py  # fake Lidarr + fake LLM, CLI folder import

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
