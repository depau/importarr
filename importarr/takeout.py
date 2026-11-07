"""CLI: import an existing, already-tagged audio folder into Lidarr.

`importarr import <path>...` points Lidarr's manual-import engine at each
path (a single file or a directory). No download is involved: Lidarr
discovers the files by their audio tags. Candidates are partitioned per
file:

  clean      - no rejections and artist+album+tracks all resolved:
               committed directly (deterministically, no LLM).
  env        - an environmental rejection (disk, permissions, root folder):
               reported, not retried, the LLM cannot fix it.
  agent      - a fixable rejection (track/album/artist mapping) and the
               file is otherwise resolved: handed to the LLM agent.
  unresolved - nothing to resolve (unknown artist, no album, >100-file
               cutoff): reported for the user. Only the LLM can push these
               further, and even then an artist must already be in the
               library.

The folder is relayed to Lidarr's API only, so it must be a path Lidarr can
see (a shared volume), not necessarily one the importarr container sees.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from .lidarr import LidarrClient, LidarrError
from .notify import Notifier
from .triage import Verdict

log = logging.getLogger("importarr.takeout")

# Per-file bucket, decided by the candidate's own rejections + resolved ids.
_CLEAN = "clean"
_ENV = "env"
_AGENT = "agent"
_UNRESOLVED = "unresolved"


class FolderEvent(SimpleNamespace):
    """Event shape the agent expects, for a user folder (no download)."""

    def __init__(self, folder: str):
        super().__init__(
            folder=folder,
            artist_id=None,
            artist_name=folder.rsplit("/", 1)[-1],
            downloadId="",
            downloadClient="",
            tracks=[],
            trackFiles=[],
        )


def _reject_reasons(c: dict) -> list[str]:
    out = []
    for r in c.get("rejections") or []:
        out.append(r.get("reason", str(r)) if isinstance(r, dict) else str(r))
    return out


def _env_reasons(c: dict) -> list[str]:
    from .triage import _ENV_MARKERS
    return [r for r in _reject_reasons(c)
            if any(m in r.lower() for m in _ENV_MARKERS)]


def _resolved(c: dict) -> bool:
    """Artist, album and at least one track id all present."""
    return bool(c.get("artist") and c.get("album")
                and any(t.get("id") is not None for t in c.get("tracks") or []))


def partition(candidates: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {
        _CLEAN: [], _ENV: [], _AGENT: [], _UNRESOLVED: []}
    for c in candidates:
        env = _env_reasons(c)
        reasons = _reject_reasons(c)
        if env:
            out[_ENV].append(c)
        elif reasons:
            # fixable rejection: agent if the file is otherwise resolvable,
            # else the user (unknown artist / no album / cutoff)
            out[_AGENT if _resolved(c) else _UNRESOLVED].append(c)
        elif _resolved(c):
            out[_CLEAN].append(c)
        else:
            # no rejections but not resolved (e.g. >100-file cutoff): not
            # safe to commit blind
            out[_UNRESOLVED].append(c)
    return out


def _commit_files(candidates: list[dict]) -> list[dict]:
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



async def import_paths(app, paths: list[str]) -> dict:
    """Drive the full folder import. Returns a per-folder report dict."""
    cfg = app.state.cfg
    lidarr: LidarrClient = app.state.lidarr
    notifier: Notifier = app.state.notifier
    dry_run = cfg.dry_run
    reports: list[dict] = []

    for path in paths:
        report = {"path": path, "clean": [], "env": [], "agent": [],
                  "unresolved": [], "committed": 0, "committed_names": [],
                  "would_commit": [], "skipped": []}
        try:
            candidates = await lidarr.manualimport(path, 0, "")
        except LidarrError as e:
            log.error("probe %s failed: %s", path, e)
            report["error"] = str(e)
            reports.append(report)
            continue

        if not candidates:
            log.warning("no audio candidates found for %s", path)
            report["skipped"].append("no candidates found (path not visible to Lidarr? no audio?)")
            reports.append(report)
            continue

        buckets = partition(candidates)
        report["clean"] = [c["name"] for c in buckets[_CLEAN]]
        report["env"] = [c["name"] for c in buckets[_ENV]]
        report["agent"] = [c["name"] for c in buckets[_AGENT]]
        report["unresolved"] = [c["name"] for c in buckets[_UNRESOLVED]]

        # 1) commit the clean files deterministically
        if buckets[_CLEAN]:
            files = _commit_files(buckets[_CLEAN])
            names = [f["path"].rsplit("/", 1)[-1] for f in files]
            if dry_run:
                report["would_commit"] = names
                log.info("dry run: would commit %d clean file(s) in %s",
                         len(names), path)
            else:
                await lidarr.manual_import_command(files)
                report["committed"] = len(files)
                report["committed_names"] = names
                log.info("committed %d clean file(s) in %s", len(files), path)

        # 2) hand the fixable-rejection files to the agent, if configured
        if buckets[_AGENT]:
            from . import agent
            verdict = Verdict(cls="mapping",
                              reason="manual import of existing folder",
                              candidates=buckets[_AGENT])
            if agent.available(cfg):
                result = await agent.run_agent(app, FolderEvent(path), verdict,
                                               transport=cfg.llm_transport,
                                               folder_mode=True)
                report["agent_outcome"] = result["outcome"]
                report["agent_summary"] = result.get("summary", "")
                log.info("agent for %s: %s (%s)", path, result["outcome"],
                         result.get("summary"))
            else:
                report["skipped"].append(
                    "agent files not handled (LLM not configured)")
        # 3) report the rest
        report["skipped"].extend(_names_of(buckets[_ENV], "env: "))
        report["skipped"].extend(_names_of(buckets[_UNRESOLVED],
                                           "unresolved: "))

        reports.append(report)
        await notifier.notify(
            f"[importarr import] {path}",
            _format_report(report, dry_run))

    return {"reports": reports}



def _names_of(cands: list[dict], prefix: str) -> list[str]:
    return [f"{prefix}{c.get('name') or c.get('path', '?').rsplit('/', 1)[-1]}"
            for c in cands]


def _format_report(report: dict, dry_run: bool) -> str:
    def _join(names: list[str]) -> str:
        return ", ".join(names) if names else "-"
    if dry_run:
        committed_line = (f"would commit: {len(report.get('would_commit', []))} "
                          f"({_join(report.get('would_commit', []))})\n")
    else:
        committed_line = (f"committed: {report['committed']} "
                          f"({_join(report['committed_names'])})\n")
    body = (
        committed_line
        + f"agent: {_join(report['agent'])}\n"
        + f"env: {_join(report['env'])}\n"
        + f"unresolved: {_join(report['unresolved'])}\n"
    )
    if report.get("agent_outcome"):
        body += f"agent outcome: {report['agent_outcome']}\n"
    if report.get("agent_summary"):
        body += f"agent: {report['agent_summary']}\n"
    if report.get("error"):
        body += f"error: {report['error']}\n"
    if report["skipped"]:
        body += "skipped: " + "; ".join(report["skipped"]) + "\n"
    if dry_run:
        body += "(dry run: nothing was changed)"
    return body.strip()
