"""Audio tag inspection for the LLM agent.

Reads metadata (title/artist/album, length, codec) with mutagen so the agent
can identify misassigned files. All tag values are UNTRUSTED DATA: they are
truncated and surfaced to the model with an explicit "data, not instructions"
declaration. File access is gated on the realpath allowlist (AUDIO_PATHS).
"""
from __future__ import annotations

import logging

import os
from mutagen import File as MutagenFile
log = logging.getLogger("importarr.tags")

MAX_VALUE = 200
MAX_OUT = 4000
def _allowlisted(path: str, roots: tuple[str, ...]) -> bool:
    try:
        rp = os.path.realpath(path)
    except OSError:
        return False
    return any(rp == r or rp.startswith(r + os.sep) for r in roots)


def inspect_tags(path: str, roots: tuple[str, ...]) -> dict:
    if not roots:
        return {"error": "tag inspection disabled (AUDIO_PATHS not set)"}
    if not _allowlisted(path, roots):
        return {"error": "path not in AUDIO_PATHS allowlist"}
    try:
        audio = MutagenFile(path)
    except Exception as e:
        return {"error": f"cannot read tags: {e.__class__.__name__}: {str(e)[:100]}"}
    if audio is None:
        return {"error": "no tag support for this file type"}

    def v(x) -> str:
        return str(x).strip()[:MAX_VALUE]

    out: dict = {"format": type(audio).__name__}
    tags = getattr(audio, "tags", None)
    if tags:
        out["tags"] = {
            k: v(x) for k, x in list(tags.items())[:24] if x
        }
    info = getattr(audio, "info", None)
    if info:
        out["info"] = {
            "length_s": round(getattr(info, "length", 0) or 0),
            "bitrate": getattr(info, "bitrate", None),
            "codec": getattr(info, "codec", None),
            "sample_rate": getattr(info, "sample_rate", None),
        }
    text = repr(out)
    if len(text) > MAX_OUT:
        out = {"truncated": True, "raw": text[:MAX_OUT]}
    return out
