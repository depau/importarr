"""Import-failure flow: probe manualimport, classify, commit/retry/agent/notify."""
from __future__ import annotations

import asyncio
import logging

from .lidarr import LidarrClient, LidarrError
from .notify import Notifier
from .triage import Verdict, classify_candidates

log = logging.getLogger("importarr.flow")


async def _probe(lidarr: LidarrClient, event) -> list[dict]:
    """Candidate files Lidarr can currently see for this download.

    artistId is required by the endpoint. When the webhook has no artist
    (unknown-artist failures), query the queue item's artist instead; if that
    fails too, there's nothing to probe.
    """
    try:
        return await lidarr.manualimport(event.folder, event.artist_id or 0,
                                         event.downloadId)
    except LidarrError as e:
        log.info("manualimport probe failed: %s", e)
        return []


def _commit_files(candidates: list[dict]) -> list[dict]:
    """ManualImportFile payloads from candidates. Track re-assignment happens
    via update_manual_import() first when the agent has opinions."""
    files = []
    for c in candidates:
        album = c.get("album") or {}
        artist = c.get("artist") or {}
        files.append({
            "path": c["path"],
            "artistId": artist.get("id", 0),
            "albumId": album.get("id", 0),
            "albumReleaseId": c.get("albumReleaseId", 0),
            "trackIds": [t.get("id") for t in c.get("tracks") or []
                         if t.get("id") is not None],
            "downloadId": c.get("downloadId") or "",
            "quality": c.get("quality") or {},
        })
    return files



async def _queue_item(app, event) -> dict | None:
    lidarr: LidarrClient = app.state.lidarr
    try:
        page = await lidarr.queue()
    except LidarrError:
        return None
    for item in page.get("records") or []:
        if item.get("downloadId") and item.get("downloadId") == event.downloadId:
            return item
    return None


async def give_up(app, event, verdict: Verdict, agent_result: dict | None = None) -> None:
    """Notify the outcome. Removes the queue item + blacklists only when the
    agent explicitly chose `removed` (or no agent and verdict says give up)."""
    lidarr: LidarrClient = app.state.lidarr
    notifier: Notifier = app.state.notifier
    dry_run = app.state.cfg.dry_run
    removed = agent_result is not None and agent_result.get("outcome") == "removed" and not dry_run

    title = f"{event.artist_name} — {'agent ' + agent_result['outcome'] if agent_result else 'import failed'}"
    body = (f"reason: {verdict.reason}\n"
            f"files: {len(event.trackFiles)}\n"
            f"folder: {event.folder}\n"
            f"downloadId: {event.downloadId}")
    if agent_result and agent_result.get("summary"):
        body += f"\nagent: {agent_result['summary']}"
    if dry_run:
        body += "\n(dry run: nothing was changed)"

    if removed:
        item = await _queue_item(app, event)
        if item is not None:
            try:
                await lidarr.delete_queue(item["id"], blocklist=True,
                                          skip_redownload=True)
                body += f"\nqueue item {item['id']} removed + blacklisted"
            except LidarrError as e:
                body += f"\nqueue remove failed: {e}"

    await notifier.notify(f"[importarr] {title}", body)
    log.warning("gave up: %s (%s)", event.dedupe_key, verdict.reason)


async def handle_import_failure(app, event) -> None:
    lidarr: LidarrClient = app.state.lidarr
    cfg = app.state.cfg
    notifier: Notifier = app.state.notifier

    if not event.folder:
        await notifier.notify(
            f"[importarr] {event.artist_name} — no audio path in webhook",
            f"trackFiles had no recognizable audio file; cannot locate folder.\n"
            f"paths: {[tf.path for tf in event.trackFiles][:5]}")
        return

    verdict: Verdict | None = None
    for attempt, backoff in enumerate([0, *cfg.retry_backoffs]):
        if backoff:
            await asyncio.sleep(backoff)
        candidates = await _probe(lidarr, event)
        verdict = classify_candidates(candidates)
        log.info("%s: attempt %d -> %s (%s)",
                 event.dedupe_key, attempt + 1, verdict.cls, verdict.reason)

        if verdict.cls == "clean":
            files = _commit_files(candidates)
            if cfg.dry_run:
                await notifier.notify(
                    f"[importarr] {event.artist_name} — would import {len(files)} file(s)",
                    f"folder: {event.folder}\n"
                    f"files: {[f['path'].rsplit('/', 1)[-1] for f in files]}")
                log.info("dry run: would commit %d files", len(files))
            else:
                await lidarr.manual_import_command(files,
                                                   mode="auto")
                await notifier.notify(
                    f"[importarr] {event.artist_name} — committed {len(files)} file(s)",
                    f"folder: {event.folder}")
            return

        if verdict.cls == "env":
            # Environment problem: LLM won't help.
            await give_up(app, event, verdict)
            return

        if verdict.cls in ("mapping", "unknown"):
            break  # retries can't fix mapping; hand off

    # --- retries exhausted with mapping/unknown: agent, if configured ---
    from . import agent
    if agent.available(cfg):
        result = await agent.run_agent(app, event, verdict,
                                       transport=cfg.llm_transport)
        log.info("%s: agent outcome=%s (%s)",
                 event.dedupe_key, result["outcome"], result.get("summary"))
        if result["outcome"] != "imported":
            await give_up(app, event, verdict, agent_result=result)
        return

    await give_up(app, event, verdict or Verdict(cls="unknown", reason="no agent configured"))
