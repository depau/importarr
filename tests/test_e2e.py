"""E2e check: fake Lidarr (real v3.2 API shapes) + fake LLM, drives the app.

Run: python tests/test_e2e.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

FOLDER = "/downloads/Test Artist/Album (2020)"
TRACKS = [{"id": 1, "trackNumber": 1, "title": "one"}, {"id": 2, "trackNumber": 2, "title": "two"}]
CANDIDATES = [{
    "id": 100, "path": f"{FOLDER}/01 - one.flac", "name": "01 - one.flac", "size": 1000,
    "artist": {"id": 123, "name": "Test Artist"},
    "album": {"id": 456, "title": "Album (2020)"},
    "albumReleaseId": 789, "downloadId": "dl-1",
    "quality": {"qualityId": 4, "revision": {"version": 0, "real": 0}},
    "tracks": [{"id": 1, "trackNumber": 1, "title": "one"},
               {"id": 2, "trackNumber": 2, "title": "two"}],
    "rejections": [{"reason": "Too few tracks: album has 10, found 2", "type": "permanent"}],
    "additionalFile": False,
}]

state = {"commands": [], "deleted": []}
rounds = {"n": 0}


class FakeLidarr:
    """Responds with Lidarr v3.2 response shapes for the endpoints the app uses."""

    def __call__(self, request: httpx.Request):
        path = request.url.path
        if path == "/api/v1/manualimport":
            return httpx.Response(200, json=CANDIDATES)
        if path == "/api/v1/command" and request.method == "POST":
            state["commands"].append(json.loads(request.content))
            return httpx.Response(202, json=[{"name": "ManualImport", "status": "queued"}])
        if path == "/api/v1/queue":
            return httpx.Response(200, json={"records": [
                {"id": 55, "downloadId": "dl-1", "status": "importPending"}]})
        if path == "/api/v1/queue/55" and request.method == "DELETE":
            state["deleted"].append(dict(request.url.params))
            return httpx.Response(200)
        if path == "/api/v1/artist/123":
            return httpx.Response(200, json={"id": 123, "name": "Test Artist",
                                             "path": "/music/Test Artist"})
        if path == "/api/v1/album/456":
            return httpx.Response(200, json={"id": 456, "title": "Album (2020)"})
        if path == "/api/v1/history":
            return httpx.Response(200, json={"records": []})
        raise AssertionError(f"unexpected {request.method} {path}")


def llm_app(request: httpx.Request):
    assert request.url.path == "/v1/chat/completions", request.url.path
    json.loads(request.content)
    rounds["n"] += 1
    if rounds["n"] == 1:
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "commit_import",
                "arguments": json.dumps({"files": [{
                    "path": f"{FOLDER}/01 - one.flac", "artistId": 123, "albumId": 456,
                    "albumReleaseId": 789, "trackIds": [1, 2], "downloadId": "dl-1"}]})}},
            {"id": "c2", "type": "function", "function": {
                "name": "finish",
                "arguments": json.dumps({"outcome": "imported",
                                         "summary": "tracks were misassigned; committed"})}}]}
    else:
        raise AssertionError("agent did not finish in round 1")
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def llm_remove(request: httpx.Request):
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "r1", "type": "function", "function": {
            "name": "remove_release",
            "arguments": json.dumps({"reason": "release is a single song, not the album"})}},
        {"id": "r2", "type": "function", "function": {
            "name": "finish",
            "arguments": json.dumps({"outcome": "removed",
                                     "summary": "wrong release; removed"})}}]}
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def llm_tags(request: httpx.Request):
    data = json.loads(request.content)
    rounds["n"] += 1
    if rounds["n"] == 1:
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {
                "name": "inspect_tags",
                "arguments": json.dumps({"path": f"{AUDIO_ROOT}/01 - one.flac"})}},
            {"id": "t2", "type": "function", "function": {
                "name": "inspect_tags",
                "arguments": json.dumps({"path": "/etc/passwd"})}}]}
    else:
        tools = [m for m in data["messages"] if m.get("role") == "tool"]
        assert len(tools) == 2, tools
        assert "one" in tools[0]["content"] and "FLAC" in tools[0]["content"], tools[0]
        assert "not in AUDIO_PATHS" in tools[1]["content"], tools[1]
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t3", "type": "function", "function": {
                "name": "finish",
                "arguments": json.dumps({"outcome": "gave_up",
                                         "summary": "tags confirmed; giving up"})}}]}
    return httpx.Response(200, json={"choices": [{"message": msg}]})


import importarr.app as appmod
from importarr.app import app as build_app
from importarr.config import Config


def make_cfg(dry_run=True, transport=None, audio_paths=None):
    return Config(
        lidarr_url="http://lidarr.test", lidarr_api_key="k",
        llm_base_url="http://llm.test/v1", llm_api_key="k", llm_model="m",
        llm_max_rounds=5, llm_transport=transport,
        dry_run=dry_run, retry_backoffs=(), log_level="WARNING",
        audio_paths=audio_paths if audio_paths is not None else (AUDIO_ROOT,),
    )


def build(dry_run, transport, audio_paths=None):
    app = build_app(make_cfg(dry_run=dry_run, transport=transport,
                             audio_paths=audio_paths))
    app.state.lidarr.client = httpx.AsyncClient(
        base_url="http://lidarr.test", headers={"X-Api-Key": "k"},
        transport=httpx.MockTransport(FakeLidarr()))
    return app


PAYLOAD = {
    "eventType": "ImportFailure",
    "artist": {"id": 123, "name": "Test Artist"},
    "tracks": TRACKS,
    "trackFiles": [{"path": f"{FOLDER}/01 - one.flac", "quality": "Lossless",
                    "qualityVersion": 0, "size": 1000}],
    "downloadClient": "qbittorrent", "downloadClientType": "qbittorrent",
    "downloadId": "dl-1",
}


import os
import shutil
import tempfile

HERE = Path(__file__).resolve().parent
_tmp = tempfile.mkdtemp(prefix="importarr-tags-")
AUDIO_ROOT = os.path.realpath(_tmp) + "/downloads"
os.makedirs(AUDIO_ROOT, exist_ok=True)
# committed tagged FLAC (title=one, artist=Test Artist, album=Album (2020))
shutil.copyfile(HERE / "fixtures" / "one.flac", f"{AUDIO_ROOT}/01 - one.flac")


async def post_event(app, payload):
    cli = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x")
    try:
        r = await cli.post("/webhook", json=payload)
        assert r.json()["ok"] is True, r.text
        await app.state.queue.join()
    finally:
        await cli.aclose()


async def main():
    # --- scenario 1: mapping failure, agent commits, dry run => no mutation
    appmod._seen.clear()
    app = build(dry_run=True, transport=httpx.MockTransport(llm_app))
    async with app.router.lifespan_context(app):
        await post_event(app, PAYLOAD)
        assert rounds["n"] >= 1, rounds
        assert state["commands"] == [], "dry run must not commit"
        assert state["deleted"] == []
        print("ok dry-run agent path")
    await app.state.lidarr.close()

    # --- scenario 2: same but DRY_RUN=false => commit lands
    appmod._seen.clear()
    rounds["n"] = 0
    state["commands"].clear()
    app2 = build(dry_run=False, transport=httpx.MockTransport(llm_app))
    async with app2.router.lifespan_context(app2):
        await post_event(app2, PAYLOAD)
        assert len(state["commands"]) == 1, state["commands"]
        cmd = state["commands"][0]
        assert cmd["name"] == "ManualImport" and cmd["importMode"] == "manual"
        assert cmd["files"][0]["path"] == f"{FOLDER}/01 - one.flac"
        assert cmd["files"][0]["trackIds"] == [1, 2]
        assert state["deleted"] == []
        print("ok real commit path")
    await app2.state.lidarr.close()

    # --- scenario 3: agent decides removal, DRY_RUN=false => queue deleted + blacklisted
    appmod._seen.clear()
    state["commands"].clear()
    state["deleted"].clear()
    app3 = build(dry_run=False, transport=httpx.MockTransport(llm_remove))
    async with app3.router.lifespan_context(app3):
        await post_event(app3, PAYLOAD)
        assert state["deleted"] == [{"blocklist": "true", "skipRedownload": "true",
                                     "removeFromClient": "false"}], state["deleted"]
        assert state["commands"] == []
        print("ok removal path")
    await app3.state.lidarr.close()

    # --- scenario 4: agent inspects file tags; out-of-prefix path is refused
    appmod._seen.clear()
    rounds["n"] = 0
    state["commands"].clear()
    state["deleted"].clear()
    app4 = build(dry_run=True, transport=httpx.MockTransport(llm_tags))
    async with app4.router.lifespan_context(app4):
        await post_event(app4, PAYLOAD)
        assert rounds["n"] == 2, rounds
        assert state["commands"] == [] and state["deleted"] == []
        print("ok tag inspection + allowlist rejection")
    await app4.state.lidarr.close()


if __name__ == "__main__":
    asyncio.run(main())
    print("4 passed")
