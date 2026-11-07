"""Minimal async Lidarr v1 API client (target: 3.2.x).

Only the endpoints that exist in the real v1 API at v3.2.0.x:
manualimport (GET/POST), command (POST), queue (GET/DELETE),
artist/album/trackfile/history. There is no per-release importStatus
or release delete endpoint in v1.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("importarr.lidarr")

AUDIO_EXTS = (".flac", ".mp3", ".opus", ".ogg", ".m4a", ".wav", ".aac", ".wma")


class LidarrError(RuntimeError):
    pass


class LidarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 transport: object | None = None):
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> object:
        try:
            r = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise LidarrError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            raise LidarrError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        if r.status_code in (204,):
            return None
        try:
            return r.json()
        except ValueError:
            return None
    # --- health -----------------------------------------------------------
    async def status(self) -> dict:
        return (await self._request("GET", "/api/v1/system/status")) or {}

    # --- manual import ----------------------------------------------------
    async def manualimport(self, folder: str, artist_id: int,
                           download_id: str = "") -> list[dict]:
        """Candidate files in folder with auto track assignment + rejections."""
        params = {"folder": folder, "artistId": artist_id}
        if download_id:
            params["downloadId"] = download_id
        return (await self._request("GET", "/api/v1/manualimport", params=params)) or []

    async def update_manual_import(self, items: list[dict]) -> list[dict]:
        """Ask Lidarr to recalculate assignments for manual-import items."""
        return (await self._request("POST", "/api/v1/manualimport", json=items)) or []

    # --- commands ---------------------------------------------------------
    async def send_command(self, command: dict) -> object:
        return (await self._request("POST", "/api/v1/command", json=command)) or {}

    async def manual_import_command(self, files: list[dict], replace: bool = False,
                                    mode: str = "auto") -> object:
        """Submit selected ManualImportFile objects to Lidarr."""
        return await self.send_command({
            "name": "ManualImport",
            "files": files,
            "importMode": mode,
            "replaceExistingFiles": replace,
        })

    async def get_commands(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/command")) or []

    # --- queue ------------------------------------------------------------
    async def queue(self, page: int = 1, page_size: int = 100) -> dict:
        return (await self._request(
            "GET", "/api/v1/queue",
            params={"page": page, "pageSize": page_size})) or {}

    async def delete_queue(self, queue_id: int, blocklist: bool = False,
                           skip_redownload: bool = True,
                           remove_from_client: bool = False) -> None:
        """Remove queue item; optionally blacklist + skip re-download. This is the
        'give up on this release' destructive action."""
        await self._request(
            "DELETE", f"/api/v1/queue/{queue_id}",
            params={
                "blocklist": "true" if blocklist else "false",
                "skipRedownload": "true" if skip_redownload else "false",
                "removeFromClient": "true" if remove_from_client else "false",
            },
        )

    # --- lookup context for the agent --------------------------------------
    async def get_artist(self, artist_id: int) -> dict:
        return (await self._request("GET", f"/api/v1/artist/{artist_id}")) or {}

    async def find_library_artists(self, name: str) -> list[dict]:
        """Exact name matches among artists already in the Lidarr library."""
        if not isinstance(name, str):
            return []
        wanted = " ".join(name.split()).casefold()
        if not wanted:
            return []
        artists = (await self._request("GET", "/api/v1/artist")) or []
        return [
            artist for artist in artists
            if wanted in {
                " ".join(str(artist.get(field) or "").split()).casefold()
                for field in ("artistName", "sortName", "name")
            }
        ]

    async def artist_albums(self, artist_id: int) -> list[dict]:
        return (await self._request(
            "GET", "/api/v1/album", params={"artistId": artist_id})) or []

    async def get_album(self, album_id: int) -> dict:
        return (await self._request("GET", f"/api/v1/album/{album_id}")) or {}

    async def album_details(self, album_id: int,
                            album_release_id: int | None = None) -> dict:
        """Album releases plus tracks from the requested real Lidarr release."""
        if type(album_id) is not int or album_id <= 0:
            raise LidarrError("albumId must be a positive Lidarr ID")
        if (album_release_id is not None
                and (type(album_release_id) is not int or album_release_id <= 0)):
            raise LidarrError("albumReleaseId must be a positive Lidarr ID")
        album = await self.get_album(album_id)
        if album.get("id") != album_id:
            raise LidarrError(f"Lidarr did not return album {album_id}")
        params = ({"albumReleaseId": album_release_id}
                  if album_release_id is not None else {"albumId": album_id})
        if album_release_id is not None and album_release_id not in {
                release.get("id") for release in album.get("releases") or []}:
            raise LidarrError(
                f"album release {album_release_id} does not belong to album {album_id}")
        tracks = (await self._request("GET", "/api/v1/track", params=params)) or []
        return {"album": album, "tracks": tracks}

    async def trackfiles(self, album_id: int) -> list[dict]:
        return (await self._request(
            "GET", "/api/v1/trackfile", params={"albumId": album_id})) or []

    async def history(self, download_id: str | None = None,
                      artist_ids: list[int] | None = None,
                      page_size: int = 50) -> list[dict]:
        params: dict = {"pageSize": page_size}
        if download_id:
            params["downloadId"] = download_id
        if artist_ids:
            params["artistIds"] = artist_ids
        page = (await self._request("GET", "/api/v1/history", params=params)) or {}
        return page.get("records") or []

    # --- root folders for folder discovery ----------------------------------
    async def root_folders(self) -> list[dict]:
        return (await self._request("GET", "/api/v1/rootfolder")) or []

    @staticmethod
    def audio_folder_from_files(files: list[str]) -> str | None:
        """Parent dir of the first audio file (download client drops album files
        in one dir)."""
        for f in files:
            base = f.rsplit("/", 1)[-1]
            if base.lower().endswith(AUDIO_EXTS):
                return f.rsplit("/", 1)[0] if "/" in f else f
        return None
