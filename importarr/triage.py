"""Classify an importFailure episode from manualimport candidates.

The `GET /api/v1/manualimport?folder=&artistId=` response is the source of
truth: it lists candidate files with auto track assignment and, per file, a
`rejections: [{reason, type}]` list explaining why Lidarr won't import it.

Classes:
  timing  - no candidate files yet: media still landing / download client
            hasn't unpacked. A plain re-probe may fix it.
  env     - environmental rejections (disk full, not writable) or a broken
            environment: LLM won't help, notify and stop.
  mapping - candidates exist but are rejected or ambiguous (track count,
            album/artist mismatch, unmapped tracks): the agent's domain.
  clean   - candidates exist with no rejections: just commit them.
  unknown - unexpected state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Rejection substrings (case-insensitive) that mean "the environment, not the
# files, is the problem".
_ENV_MARKERS = (
    "disk", "space", "writable", "access", "permission", "locked", "full",
    "root folder",
)


@dataclass
class Verdict:
    cls: str  # timing | env | mapping | clean | unknown
    reason: str
    candidates: list[dict] = field(default_factory=list)

    @property
    def rejected(self) -> list[dict]:
        return [c for c in self.candidates if c.get("rejections")]

    @property
    def reasons(self) -> list[str]:
        out: list[str] = []
        for c in self.rejected:
            for r in c.get("rejections", []):
                reason = r.get("reason", str(r)) if isinstance(r, dict) else str(r)
                out.append(reason)
        return out


def classify_candidates(candidates: list[dict]) -> Verdict:
    if not candidates:
        return Verdict(cls="timing", reason="no candidate files found in folder yet")

    reasons = []
    env = False
    for c in candidates:
        for r in c.get("rejections") or []:
            reason = (r.get("reason", str(r)) if isinstance(r, dict) else str(r))
            reasons.append(reason)
            if any(m in reason.lower() for m in _ENV_MARKERS):
                env = True

    if env:
        return Verdict(cls="env", reason="; ".join(dict.fromkeys(reasons))[:300],
                       candidates=candidates)

    if reasons:
        return Verdict(cls="mapping", reason="; ".join(dict.fromkeys(reasons))[:300],
                       candidates=candidates)

    return Verdict(cls="clean",
                   reason=f"{len(candidates)} candidate(s), no rejections",
                   candidates=candidates)
