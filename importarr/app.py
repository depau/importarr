"""FastAPI app: receives Lidarr 'ImportFailure' webhooks, dedupes, hands off to the flow.

Webhook shape (Lidarr 3.2.x, camelCase) for eventType=ImportFailure:
  {
    "eventType": "ImportFailure",
    "instanceName": "...", "applicationUrl": "...",
    "artist": {"id": 1, "name": "..."} | null,   # null when the artist is unknown
    "tracks":  [{"id":..,"trackNumber":..,"title":".."}],
    "trackFiles": [{"id":..,"path":"/downloads/Artist/Album/01 - x.flac",
                    "quality":"Lossless","qualityVersion":0,"releaseGroup":"..",
                    "sceneName":"..","size":..,"dateAdded":".."}],
    "isUpgrade": false,
    "downloadClient": "qbittorrent", "downloadClientType": "qbittorrent",
    "downloadId": "..."
  }
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from .config import Config
from .lidarr import LidarrClient
from .notify import Notifier

log = logging.getLogger("importarr")

# dedupe key -> last-handled time
_seen: dict[str, float] = {}
_SEEN_TTL = 3600.0


class WebhookTrackFile(BaseModel):
    id: int | None = None
    path: str = ""
    quality: str = ""
    qualityVersion: int = 0
    releaseGroup: str = ""
    sceneName: str = ""
    size: int | None = None


class WebhookArtist(BaseModel):
    id: int | None = None
    name: str = ""


class WebhookTrack(BaseModel):
    id: int | None = None
    trackNumber: int | None = None
    title: str = ""


class ImportFailureEvent(BaseModel):
    eventType: str = "ImportFailure"
    instanceName: str = ""
    applicationUrl: str = ""
    artist: WebhookArtist | None = None
    tracks: list[WebhookTrack] = []
    trackFiles: list[WebhookTrackFile] = []
    isUpgrade: bool = False
    downloadClient: str = ""
    downloadClientType: str = ""
    downloadId: str = ""

    @property
    def artist_id(self) -> int | None:
        return self.artist.id if self.artist else None

    @property
    def artist_name(self) -> str:
        return self.artist.name if self.artist else ""

    @property
    def dedupe_key(self) -> str:
        folder = self.folder
        return f"{self.downloadId}|{folder}" if self.downloadId else f"folder|{folder}"

    @property
    def folder(self) -> str:
        return LidarrClient.audio_folder_from_files(
            [tf.path for tf in self.trackFiles]) or ""


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def dedupe(key: str) -> bool:
    """True if this key was handled within the TTL and should be dropped."""
    now = time.time()
    for k in list(_seen):
        if now - _seen[k] > _SEEN_TTL:
            del _seen[k]
    if key in _seen:
        return True
    return False


async def _worker(app: FastAPI) -> None:
    from . import flow
    while True:
        event = await app.state.queue.get()
        try:
            await flow.handle_import_failure(app, event)
        except Exception:
            log.exception("import %s: unhandled error", event.dedupe_key)
        finally:
            app.state.queue.task_done()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.queue = asyncio.Queue()
    worker = asyncio.create_task(_worker(app))
    try:
        yield
    finally:
        worker.cancel()
        await app.state.lidarr.close()


def app(cfg: Config) -> FastAPI:
    application = FastAPI(title="importarr", lifespan=_lifespan)
    application.state.cfg = cfg
    application.state.lidarr = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key)
    application.state.notifier = Notifier(cfg.apprise_url)

    @application.post("/webhook")
    async def webhook(event: ImportFailureEvent):
        if event.eventType != "ImportFailure":
            log.debug("ignoring eventType=%s", event.eventType)
            return {"ok": True, "ignored": event.eventType}
        if not event.trackFiles:
            log.info("ImportFailure with no trackFiles; nothing to do")
            return {"ok": True, "ignored": "no files"}
        if dedupe(event.dedupe_key):
            log.info("deduped %s", event.dedupe_key)
            return {"ok": True, "deduped": True}
        _seen[event.dedupe_key] = time.time()
        log.info("ImportFailure artist=%s files=%d folder=%s",
                 event.artist_name, len(event.trackFiles), event.folder)
        await application.state.queue.put(event)
        return {"ok": True, "queued": True}

    @application.get("/health")
    async def health():
        return {"ok": True}

    return application
