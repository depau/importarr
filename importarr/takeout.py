"""Import files from a mixed, already-tagged music dump."""
from __future__ import annotations

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
                verdict = Verdict(cls="mapping", reason="identify from file tags", candidates=[candidate])
                result = await agent.run_agent(app, FolderEvent(file), verdict,
                                               transport=cfg.llm_transport, folder_mode=True)
                report["agent"].append(file)
                actions = [a for a in result.get("actions", []) if a.get("tool") == "commit_import"]
                submitted = any(f.get("path") == file for a in actions
                                for f in a.get("args", {}).get("files", []))
                if submitted:
                    report["would_commit" if cfg.dry_run else "committed_names"].append(file)
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
    return {"reports": reports}


def _format_report(report: dict, dry_run: bool) -> str:
    action = "would submit" if dry_run else "submitted"
    names = report["would_commit" if dry_run else "committed_names"]
    lines = [f"{action}: {len(names)}", f"unresolved: {len(report['unresolved'])}"]
    lines.extend(report["skipped"])
    if dry_run:
        lines.append("dry run: nothing was changed")
    return "\n".join(lines)
