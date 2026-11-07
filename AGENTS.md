# AGENTS.md

## Project overview

importarr is a Python sidecar that handles Lidarr's failed auto-imports. It receives
Lidarr's `ImportFailure` webhook, probes the manual-import candidates via the REST API,
retries transient failures, commits clean ones, and hands mapping failures to an
LLM agent (OpenAI-compatible tool-calling) that can do a manual import or remove +
blacklist the release. Mutations are dry-run by default.

Single package, no framework beyond FastAPI: `importarr/` holds app (webhook + queue),
flow (triage ladder), lidarr (REST client), triage (classifier), agent (LLM loop),
tags (mutagen file inspection), notify (apprise), config (env), `__main__` (uvicorn entry).

## Setup and test commands

- Install: `pip install .` (or `uv pip install .`)
- Run locally: `LIDARR_URL=... LIDARR_API_KEY=... python -m importarr` (port 8323)
- Run all checks (no test framework; both are plain scripts with asserts):
  - `python tests/test_triage.py` — classifier unit checks
  - `python tests/test_e2e.py` — full flow against a fake Lidarr + fake LLM
    (httpx MockTransport; no network, no real Lidarr needed). Uses the committed
    tagged FLAC at `tests/fixtures/one.flac` for the tag-inspection scenario.
- A commit is ready when both pass and `python -m py_compile importarr/*.py` is clean.

## Wire contracts — do not break

- The webhook trigger is `eventType: "ImportFailure"` (PascalCase, from Lidarr's
  `WebhookEventType` enum). The app filters on exactly that string; other events
  are ignored. Payload fields are camelCase (`trackFiles`, `downloadId`, …).
- Lidarr API is v1 (target 3.2.x): `GET/POST /api/v1/manualimport`,
  `POST /api/v1/command` (ManualImport with `files`, `importMode`,
  `replaceExistingFiles`), `GET/DELETE /api/v1/queue`,
  `GET /api/v1/artist|album|trackfile|history`. There is no per-release
  importStatus or release-delete endpoint in v1.
- `ManualImport` file payloads need `path`, `artistId`, `albumId`,
  `albumReleaseId`, `trackIds`, `downloadId` — copied from the latest
  `manualimport` candidates, never invented.
- `DELETE /api/v1/queue/{id}` params are serialized as `"true"`/`"false"` strings.
- The download folder is derived from the parent dir of the first audio file in
  `trackFiles[].path` (see `LidarrClient.audio_folder_from_files`).

## Code style

- Python 3.11+, async throughout (httpx `AsyncClient`, asyncio queue).
- Config is a frozen dataclass built in `Config.from_env()`; add new env vars there.
- No LLM SDK: the agent calls `{LLM_BASE_URL}/chat/completions` with raw httpx
  (`agent._llm_call`). Keep it that way.
- Tests are plain assert scripts, no pytest fixtures; keep them dependency-free.
- Keep the dry-run gate: every mutating client call (commit, queue delete) must
  check `cfg.dry_run` in the flow/agent layer, never inside `lidarr.py`.

## Security boundary (tag inspection)

- File reads for the agent are confined to `AUDIO_PATHS` roots: `tags._allowlisted`
  does a `realpath` prefix check (symlink-safe). Never loosen this to make a path work.
- Tag values are truncated (`MAX_VALUE`/`MAX_OUT` in `tags.py`) and the system prompt
  declares tool results data, not instructions (prompt-injection mitigation).
- Mount the audio root read-only in compose; the container never writes files.

## Commits

- Conventional Commits style, one conceptual change per commit
  (`feat:` / `fix:` / small scope where the repo has established one).
- No branches unless requested; commit to `main` directly.

## Notes for changes

- `llm_transport` on `Config` and `run_agent(..., transport=)` are test hooks
  (httpx transports); tests inject them. Don't remove without updating
  `tests/test_e2e.py`.
- Dedupe is a module-level dict in `app.py` (`_seen`, 1h TTL); e2e tests clear it
  between scenarios.
- Lidarr's webhook must include `trackFiles` (it does on `ImportFailure`); the app
  refuses events without them.
