"""Import files from a mixed, already-tagged music dump."""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

from mutagen import File as MutagenFile

from . import agent
from .tags import _allowlisted, inspect_tags
from .triage import Verdict

log = logging.getLogger("importarr.takeout")
_AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv", ".mka", ".aac"}


class FolderEvent(SimpleNamespace):
    def __init__(self, path: str):
        super().__init__(folder=path, artist_id=None, artist_name=os.path.basename(path),
                         downloadId="", downloadClient="", tracks=[], trackFiles=[])


def _files(path: str):
    if os.path.isfile(path):
        yield path
    elif os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() in _AUDIO_EXTENSIONS:
                    yield os.path.join(root, name)


def _tag_context(path: str, roots: tuple[str, ...]) -> dict:
    tags = inspect_tags(path, roots)
    if "error" in tags:
        return tags
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            keys = ("artist", "albumartist", "album", "title", "tracknumber",
                    "discnumber", "date", "musicbrainz_trackid",
                    "musicbrainz_albumid", "musicbrainz_artistid")
            tags["identity"] = {key: [str(v)[:200] for v in audio.tags.get(key, [])[:5]]
                                for key in keys if audio.tags.get(key)}
    except Exception:
        log.warning("cannot read normalized tags for %s", path, exc_info=True)
    return tags


async def import_paths(app, paths: list[str]) -> dict:
    """Inspect and process each file independently; one failure never stops the dump."""
    cfg, lidarr, notifier = app.state.cfg, app.state.lidarr, app.state.notifier
    reports = []
    app.state.takeout_submitted_tracks = set()
    pending = {}
    for path in paths:
        report = {"path": path, "agent": [], "env": [], "unresolved": [],
                  "committed_names": [], "would_commit": [], "skipped": []}
        reports.append(report)
        if not cfg.audio_paths or not _allowlisted(path, cfg.audio_paths):
            report["error"] = "input must be mounted and inside an AUDIO_PATHS root"
            continue
        if not os.path.exists(path):
            report["error"] = "input path does not exist in the importarr container"
            continue
        if not agent.available(cfg):
            report["error"] = "tag-driven import requires LLM_BASE_URL and LLM_MODEL"
            continue
        found = False
        for file in _files(path):
            found = True
            if not _allowlisted(file, cfg.audio_paths):
                report["skipped"].append(f"{file}: outside AUDIO_PATHS")
                continue
            tags = _tag_context(file, cfg.audio_paths)
            if "error" in tags:
                report["unresolved"].append(file)
                report["skipped"].append(f"{file}: {tags['error']}")
                continue
            try:
                # A single-file probe obtains Lidarr's quality/rejection metadata;
                # identity is determined from tags, not a dump folder name.
                candidates = await lidarr.manualimport(file, 0, "")
                candidate = next((c for c in candidates if c.get("path") == file), None)
                if candidate is None:
                    report["unresolved"].append(file)
                    report["skipped"].append(f"{file}: Lidarr returned no candidate")
                    continue
                candidate["audioTags"] = tags
                skip = await _skip_reason(lidarr, file, candidate)
                if skip:
                    report["skipped"].append(f"{file}: {skip}")
                    continue
                verdict = Verdict(cls="mapping", reason="identify from file tags", candidates=[candidate])
                result = await agent.run_agent(app, FolderEvent(file), verdict,
                                               transport=cfg.llm_transport, folder_mode=True)
                report["agent"].append(file)
                actions = [a for a in result.get("actions", []) if a.get("tool") == "commit_import"]
                submitted = any(f.get("path") == file for a in actions
                                for f in a.get("args", {}).get("files", []))
                if submitted:
                    report["would_commit" if cfg.dry_run else "committed_names"].append(file)
                    if not cfg.dry_run:
                        for action in actions:
                            command = action.get("command")
                            command_id = command.get("id") if isinstance(command, dict) else None
                            if type(command_id) is not int or command_id <= 0:
                                log.warning("Cannot track import command for %s; skipping its album search", file)
                                continue
                            pending.setdefault(command_id, []).extend(
                                {**f, "_previousTrackFileId": action.get("previousTrackFileId")}
                                for f in action["args"]["files"] if f.get("path") == file)
                else:
                    report["unresolved"].append(file)
                    report["skipped"].append(f"{file}: {result.get('summary') or 'agent did not submit an import'}")
            except Exception as e:
                log.exception("cannot process %s: %s", file, e)
                report["unresolved"].append(file)
                report["skipped"].append(f"{file}: {e}")
        if not found:
            report["skipped"].append("no audio files found in the importarr container")
        await notifier.notify(f"[importarr import] {path}", _format_report(report, cfg.dry_run))
    if pending:
        await _search_incomplete_albums(lidarr, pending)
    return {"reports": reports}


async def _skip_reason(lidarr, file: str, candidate: dict) -> str | None:
    """If the mapped track already lives at this exact path, it is imported.

    Mirrors Lidarr's own copy-in-place case: the file is a new import but its
    path is already a library track file of the same track id.
    """
    track_ids = [t.get("id") for t in candidate.get("tracks") or []]
    track_ids = [tid for tid in track_ids if type(tid) is int and tid > 0]
    if len(track_ids) != 1:
        return None
    try:
        tracks = await lidarr.tracks_by_id(track_ids)
    except Exception:
        return None
    if len(tracks) != 1 or not isinstance(tracks[0], dict):
        return None
    existing = tracks[0]
    if existing.get("id") != track_ids[0] or not existing.get("hasFile"):
        return None
    trackfile_id = existing.get("trackFileId")
    if type(trackfile_id) is not int or trackfile_id <= 0:
        return None
    try:
        trackfile = await lidarr.trackfile(trackfile_id)
    except Exception:
        return None
    path = trackfile.get("path") if isinstance(trackfile, dict) else None
    if isinstance(path, str) and os.path.normpath(path) == os.path.normpath(file):
        return f"already the library file (track id {track_ids[0]})"
    return None


async def _search_incomplete_albums(lidarr, pending: dict) -> None:
    """Wait for submitted imports, then search albums still missing tracks."""
    albums: dict[int, dict[int, int | None]] = {}
    # ponytail: one five-minute deadline; persist pending jobs if runs need recovery.
    deadline = asyncio.get_running_loop().time() + 300
    while pending and asyncio.get_running_loop().time() < deadline:
        for command_id, files in list(pending.items()):
            try:
                command = await lidarr.get_command(command_id)
                status = command.get("status", "").lower()
                if status in ("queued", "started"):
                    continue
                del pending[command_id]
                if status != "completed" or command.get("result", "").lower() == "unsuccessful":
                    log.warning("Import command %s did not complete successfully: %s",
                                command_id, command)
                    continue
                for file in files:
                    album = albums.setdefault(file["albumId"], {})
                    for track_id in file["trackIds"]:
                        album[track_id] = file.get("_previousTrackFileId")
            except Exception:
                pending.pop(command_id, None)
                log.exception("Could not check import command %s; skipping its album search", command_id)
        if pending:
            await asyncio.sleep(1)
    if pending:
        log.warning("Timed out waiting for import commands %s; skipping their album searches",
                    sorted(pending))

    incomplete = []
    for album_id, imported_tracks in albums.items():
        try:
            tracks = await lidarr.album_tracks(album_id)
            if (any(t.get("id") in imported_tracks and t.get("hasFile") is True
                    and (imported_tracks[t["id"]] is None
                         or (type(t.get("trackFileId")) is int and t["trackFileId"] > 0
                             and t["trackFileId"] != imported_tracks[t["id"]]))
                    for t in tracks) and any(t.get("hasFile") is False for t in tracks)):
                incomplete.append(album_id)
        except Exception:
            log.exception("Could not check tracks for album %s", album_id)
    if incomplete:
        try:
            await lidarr.send_command({"name": "AlbumSearch", "albumIds": sorted(incomplete)})
            log.info("Submitted search for incomplete albums: %s", sorted(incomplete))
        except Exception:
            log.exception("Could not submit search for incomplete albums: %s", sorted(incomplete))


def _format_report(report: dict, dry_run: bool) -> str:
    action = "would submit" if dry_run else "submitted"
    names = report["would_commit" if dry_run else "committed_names"]
    lines = [f"{action}: {len(names)}", f"unresolved: {len(report['unresolved'])}"]
    lines.extend(report["skipped"])
    if dry_run:
        lines.append("dry run: nothing was changed")
    return "\n".join(lines)
