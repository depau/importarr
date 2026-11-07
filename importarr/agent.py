"""LLM agent: fixes import failures the retry ladder couldn't.

OpenAI-compatible /v1/chat/completions with tool calling, raw httpx (no SDK).
Every tool returns real Lidarr v3.2 data. Mutating tools (commit, queue
delete) are gated by DRY_RUN.
"""
from __future__ import annotations

import json
import logging

import httpx

from .lidarr import LidarrClient, LidarrError
from .notify import Notifier

log = logging.getLogger("importarr.agent")

SYSTEM = """\
You are importarr, an automated helper for Lidarr (music import). A download \
finished but Lidarr failed to auto-import it. Your job: fix it if you can, \
or decide it cannot be fixed.

Workflow:
1. Look at the candidate files and their rejection reasons (provided, or \
re-probe with manualimport_get).
2. If tracks are assigned to the wrong files, use update_manual_import to \
re-assign artistId/albumId/trackIds (use get_artist/artist_albums/get_album \
to find the right IDs; a file's tracks[] carry the track ids and numbers).
3. Commit with commit_import when the files should go in.
4. If the album is unrecoverable (wrong release, unrecoverable mismatches), \
use remove_release to drop the queue item and stop Lidarr re-grabbing.

Rules:
- Commit ONLY files you are confident about; never commit a file whose \
rejection reason you cannot explain or fix.
- Prefer commit_import over remove_release.
- Never invent ids: only use ids you saw in tool results.
- When done, call finish(outcome, summary). outcome must be exactly one of: \
imported, removed, gave_up.
- Be decisive: if the rejections are environmental (disk, permissions), \
finish(gave_up) immediately.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "manualimport_get",
        "description": "Re-probe Lidarr: current candidate files for the \
download, with track assignments and rejection reasons.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "update_manual_import",
        "description": "Re-assign candidate files. items: list of \
{path, artistId?, albumId?, trackIds?, additionalFile?} copied from a \
manualimport_get result. Returns updated candidates with fresh rejections.",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "artistId": {"type": "integer"},
                    "albumId": {"type": "integer"},
                    "albumReleaseId": {"type": "integer"},
                    "trackIds": {"type": "array", "items": {"type": "integer"}},
                    "additionalFile": {"type": "boolean"},
                }, "required": ["path"]}}},
            "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "commit_import",
        "description": "Commit selected files for import. files: list of \
{path, artistId, albumId, albumReleaseId, trackIds, downloadId, quality} \
copied from the latest candidate list.",
        "parameters": {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "artistId": {"type": "integer"},
                    "albumId": {"type": "integer"},
                    "albumReleaseId": {"type": "integer"},
                    "trackIds": {"type": "array", "items": {"type": "integer"}},
                    "downloadId": {"type": "string"},
                    "quality": {"type": "object"},
                }, "required": ["path", "artistId", "albumId"]}},
            "replace": {"type": "boolean"}},
            "required": ["files"]}}},
    {"type": "function", "function": {
        "name": "get_artist",
        "description": "Artist details including path.",
        "parameters": {"type": "object", "properties": {
            "artistId": {"type": "integer"}}, "required": ["artistId"]}}},
    {"type": "function", "function": {
        "name": "artist_albums",
        "description": "Albums the artist has in the library (id, title, \
tracks count).",
        "parameters": {"type": "object", "properties": {
            "artistId": {"type": "integer"}}, "required": ["artistId"]}}},
    {"type": "function", "function": {
        "name": "get_album",
        "description": "Album details including its tracklist (track ids, \
numbers, titles).",
        "parameters": {"type": "object", "properties": {
            "albumId": {"type": "integer"}}, "required": ["albumId"]}}},
    {"type": "function", "function": {
        "name": "history",
        "description": "Recent import/grab/download history for this \
download id.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "remove_release",
        "description": "Give up: remove the queue item, blacklist it so \
Lidarr does not re-grab, and stop re-downloading.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "End the task. outcome: imported | removed | gave_up. \
summary: 1-3 sentences on what you found and did.",
        "parameters": {"type": "object", "properties": {
            "outcome": {"type": "string", "enum": ["imported", "removed", "gave_up"]},
            "summary": {"type": "string"}},
            "required": ["outcome", "summary"]}}},
]


def available(cfg) -> bool:
    return bool(cfg.llm_base_url and cfg.llm_model)


def _slim_candidate(c: dict) -> dict:
    keep = {
        "path": c.get("path"),
        "name": c.get("name"),
        "size": c.get("size"),
        "albumReleaseId": c.get("albumReleaseId"),
        "downloadId": c.get("downloadId"),
        "quality": c.get("quality"),
        "releaseGroup": c.get("releaseGroup"),
        "rejections": c.get("rejections") or [],
        "additionalFile": c.get("additionalFile"),
    }
    art, alb = c.get("artist"), c.get("album")
    keep["artist"] = {"id": art.get("id"), "name": art.get("name")} if art else None
    keep["album"] = {"id": alb.get("id"), "title": alb.get("title")} if alb else None
    keep["tracks"] = [
        {"id": t.get("id"), "number": t.get("trackNumber"),
         "title": t.get("title")}
        for t in c.get("tracks") or []
    ]
    return keep


def _dry(msg: str, actions: list[dict]) -> dict:
    return {"dry_run": True, "message": msg, "actions": actions[-3:]}

async def _llm_call(cfg, client: httpx.AsyncClient, messages) -> dict:
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}
    payload = {
        "model": cfg.llm_model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    r = await client.post(f"{cfg.llm_base_url.rstrip('/')}/chat/completions",
                          json=payload, headers=headers, timeout=120.0)
    if r.status_code >= 400:
        raise LidarrError(f"LLM {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]

async def run_agent(app, event, verdict, transport=None) -> dict:
    cfg = app.state.cfg
    lidarr: LidarrClient = app.state.lidarr
    notifier: Notifier = app.state.notifier
    dry_run = cfg.dry_run
    actions: list[dict] = []
    outcome = "gave_up"
    summary = ""

    candidates = [_slim_candidate(c) for c in (verdict.candidates if verdict else [])]
    state = {"candidates": candidates}

    user = {
        "eventType": "importFailure",
        "artist": {"id": event.artist_id, "name": event.artist_name},
        "expectedTracks": [{"id": t.id, "number": t.trackNumber, "title": t.title}
                           for t in event.tracks],
        "webhookFiles": [{"path": tf.path, "quality": tf.quality} for tf in event.trackFiles],
        "folder": event.folder,
        "downloadId": event.downloadId,
        "downloadClient": event.downloadClient,
        "classification": verdict.cls if verdict else "unknown",
        "reason": verdict.reason if verdict else "",
        "candidates": candidates,
    }
    messages = [
        {"role": "system", "content": SYSTEM + (
            "\n\nDRY RUN is ON: mutating tools (commit_import, remove_release) \
are recorded but NOT executed. Reason as if they were." if dry_run else "")},
        {"role": "user", "content": json.dumps(user, default=str)[:20000]},
    ]

    async with (httpx.AsyncClient(transport=transport) if transport
                else httpx.AsyncClient()) as llm:
        for _round in range(cfg.llm_max_rounds):
            msg = await _llm_call(cfg, llm, messages)
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                summary = msg.get("content") or summary
                log.info("agent final text: %s", summary[:300])
                break
            messages.append(msg)
            finished = False
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                log.info("agent tool: %s %s", name,
                         json.dumps(args, default=str)[:300])
                result = await _execute(app, event, name, args, state, actions,
                                        dry_run)
                messages.append({"role": "tool",
                                 "tool_call_id": tc["id"],
                                 "content": json.dumps(result, default=str)[:8000]})
                if name == "finish":
                    outcome = args.get("outcome", "gave_up")
                    summary = args.get("summary", "")
                    finished = True
                    if dry_run:
                        await notifier.notify(
                            f"[importarr dry-run] {event.artist_name} — agent: {outcome}",
                            f"summary: {summary}\n"
                            f"proposed actions: {json.dumps(actions, default=str)[:500]}")
            if finished:
                break

    return {"outcome": outcome, "summary": summary, "actions": actions,
            "removed": outcome == "removed" and not dry_run}


async def _execute(app, event, name, args, state, actions, dry_run):
    lidarr: LidarrClient = app.state.lidarr
    try:
        if name == "manualimport_get":
            items = await lidarr.manualimport(event.folder, event.artist_id or 0,
                                              event.downloadId)
            state["candidates"] = [_slim_candidate(c) for c in items]
            return state["candidates"]
        if name == "update_manual_import":
            payload = [{k: v for k, v in item.items() if v is not None}
                       for item in args.get("items", [])]
            if dry_run:
                actions.append({"tool": "update_manual_import", "args": args})
                return _dry("recorded, not executed", actions)
            out = await lidarr.update_manual_import(payload)
            state["candidates"] = [_slim_candidate(c) for c in out]
            return state["candidates"]
        if name == "commit_import":
            files = args.get("files", [])
            actions.append({"tool": "commit_import", "args": {"files": files}})
            if dry_run:
                return _dry(f"{len(files)} file(s) recorded, not committed", actions)
            await lidarr.manual_import_command(files, replace=args.get("replace", False))
            return {"committed": len(files)}
        if name == "get_artist":
            return await lidarr.get_artist(args["artistId"])
        if name == "artist_albums":
            return await lidarr.artist_albums(args["artistId"])
        if name == "get_album":
            return await lidarr.get_album(args["albumId"])
        if name == "history":
            return await lidarr.history(download_id=event.downloadId or None)
        if name == "remove_release":
            # Decision marker only — the flow performs the actual queue removal
            # (single execution site).
            actions.append({"tool": "remove_release",
                            "args": {"reason": args.get("reason")}})
            return {"ok": True, "note": "decision recorded; flow will remove the queue item"}
        if name == "finish":
            return {"ok": True}
        return {"error": f"unknown tool {name}"}
    except LidarrError as e:
        return {"error": str(e)}
