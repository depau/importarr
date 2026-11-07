"""CLI import check: partition + clean commit + folder-mode agent + dry-run.

Fake Lidarr (real v3.2 API shapes) + fake LLM via httpx MockTransports; no
network, no real Lidarr. Covers: per-file partition, clean commit, agent
handoff, env/unresolved reporting, and the dry-run gate.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from importarr.notify import Notifier

FOLDER = "/music/Takeout"
CANDIDATES = [
    # clean: no rejections, fully resolved -> committed directly
    {"id": 1, "path": f"{FOLDER}/clean.flac", "name": "clean.flac",
     "artist": {"id": 10, "name": "A"}, "album": {"id": 100, "title": "Al"},
     "albumReleaseId": 1, "downloadId": "",
     "quality": {"qualityId": 4, "revision": {"version": 0, "real": 0}},
     "tracks": [{"id": 1, "trackNumber": 1, "title": "t"}], "rejections": []},
    # agent: fixable rejection, resolved -> LLM
    {"id": 2, "path": f"{FOLDER}/misassigned.flac", "name": "misassigned.flac",
     "artist": {"id": 10, "name": "A"}, "album": {"id": 100, "title": "Al"},
     "albumReleaseId": 1, "downloadId": "",
     "quality": {"qualityId": 4, "revision": {"version": 0, "real": 0}},
     "tracks": [{"id": 1, "trackNumber": 1, "title": "t"}],
     "rejections": [{"reason": "Track number already assigned", "type": "permanent"}]},
    # env: environmental rejection -> reported, not touched
    {"id": 3, "path": f"{FOLDER}/locked.flac", "name": "locked.flac",
     "artist": {"id": 10, "name": "A"}, "album": {"id": 100, "title": "Al"},
     "albumReleaseId": 1, "downloadId": "",
     "quality": {"qualityId": 4, "revision": {"version": 0, "real": 0}},
     "tracks": [{"id": 1, "trackNumber": 1, "title": "t"}],
     "rejections": [{"reason": "File is not writable", "type": "permanent"}]},
    # unresolved: unknown artist, nothing to resolve -> reported for user
    {"id": 4, "path": f"{FOLDER}/unknown.flac", "name": "unknown.flac",
     "artist": None, "album": None, "albumReleaseId": 0, "downloadId": "",
     "quality": None, "tracks": [],
     "rejections": [{"reason": "Artist not found in the library", "type": "permanent"}]},
]

state = {"commands": []}


class FakeLidarr:
    def __call__(self, request: httpx.Request):
        path = request.url.path
        if path == "/api/v1/manualimport" and request.method == "GET":
            return httpx.Response(200, json=CANDIDATES)
        if path == "/api/v1/command" and request.method == "POST":
            state["commands"].append(json.loads(request.content))
            return httpx.Response(202, json=[{"name": "ManualImport", "status": "queued"}])
        raise AssertionError(f"unexpected {request.method} {path}")


def llm_folder(request: httpx.Request):
    assert request.url.path == "/v1/chat/completions"
    data = json.loads(request.content)
    # folder mode must not offer remove_release or history
    tools = {t["function"]["name"] for t in data["tools"]}
    assert "remove_release" not in tools, tools
    assert "history" not in tools, tools
    assert "commit_import" in tools and "finish" in tools, tools
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "commit_import",
            "arguments": json.dumps({"files": [{
                "path": f"{FOLDER}/misassigned.flac", "artistId": 10, "albumId": 100,
                "albumReleaseId": 1, "trackIds": [1]}]})}},
        {"id": "f1", "type": "function", "function": {
            "name": "finish",
            "arguments": json.dumps({"outcome": "imported",
                                     "summary": "reassigned and committed"})}}]}
    return httpx.Response(200, json={"choices": [{"message": msg}]})


def make_app(dry_run, llm_transport=None):
    from importarr.config import Config
    from importarr.lidarr import LidarrClient
    from importarr.notify import Notifier
    cfg = Config(
        lidarr_url="http://lidarr.test", lidarr_api_key="k",
        llm_base_url="http://llm.test/v1", llm_api_key="k", llm_model="m",
        llm_max_rounds=5, llm_transport=llm_transport,
        dry_run=dry_run, retry_backoffs=(), log_level="WARNING",
    )
    lidarr = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key,
                          transport=httpx.MockTransport(FakeLidarr()))
    return SimpleNamespace(state=SimpleNamespace(
        cfg=cfg, lidarr=lidarr, notifier=Notifier(None),
    ))


def committed_paths() -> set[str]:
    out = set()
    for cmd in state["commands"]:
        for f in cmd["files"]:
            out.add(f["path"])
    return out

async def main():
    from importarr import takeout

    # --- real (non-dry) run: clean committed directly, agent file via LLM ---
    state["commands"].clear()
    app = make_app(dry_run=False, llm_transport=httpx.MockTransport(llm_folder))
    result = await takeout.import_paths(app, [FOLDER])
    await app.state.lidarr.close()
    rep = result["reports"][0]

    assert rep["clean"] == ["clean.flac"], rep["clean"]
    assert rep["agent"] == ["misassigned.flac"], rep["agent"]
    assert rep["env"] == ["locked.flac"], rep["env"]
    assert rep["unresolved"] == ["unknown.flac"], rep["unresolved"]
    assert rep["committed"] == 1, rep  # only the clean file, committed directly
    assert rep["agent_outcome"] == "imported", rep
    # clean committed directly + agent committed misassigned -> both in the
    # ManualImport command, no env/unresolved
    assert committed_paths() == {
        f"{FOLDER}/clean.flac", f"{FOLDER}/misassigned.flac"}, committed_paths()
    assert rep["skipped"] and any("env: locked.flac" in s for s in rep["skipped"]), rep["skipped"]
    assert rep["skipped"] and any("unresolved: unknown.flac" in s for s in rep["skipped"]), rep["skipped"]

    # --- dry run: nothing committed, clean listed as would_commit ---
    state["commands"].clear()
    app2 = make_app(dry_run=True, llm_transport=httpx.MockTransport(llm_folder))
    result2 = await takeout.import_paths(app2, [FOLDER])
    await app2.state.lidarr.close()
    rep2 = result2["reports"][0]
    assert state["commands"] == [], state["commands"]  # no mutation at all
    assert rep2["committed"] == 0, rep2
    assert rep2["would_commit"] == ["clean.flac"], rep2["would_commit"]
    assert rep2["agent_outcome"] == "imported", rep2  # agent still reasoned

    # --- redirect: proxy/Lidarr 307s the first manualimport hit; client must
    # follow it (httpx defaults off; production hit this and saw 0 candidates)
    redirects = {"n": 0}

    class RedirectLidarr:
        def __call__(self, request: httpx.Request):
            if (request.url.path == "/api/v1/manualimport"
                    and request.method == "GET" and redirects["n"] == 0):
                redirects["n"] += 1
                return httpx.Response(307, headers={"location": "/api/v1/manualimport/"})
            if request.url.path == "/api/v1/manualimport/" and request.method == "GET":
                return httpx.Response(200, json=CANDIDATES)
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

    from importarr.lidarr import LidarrClient
    from importarr import config as _cfg
    c3 = _cfg.Config(lidarr_url="http://lidarr.test", lidarr_api_key="k",
                     llm_base_url="http://llm.test/v1", llm_api_key="k",
                     llm_model="m", llm_max_rounds=5,
                     llm_transport=httpx.MockTransport(llm_folder),
                     dry_run=True, retry_backoffs=(), log_level="WARNING")
    lidarr3 = LidarrClient(c3.lidarr_url, c3.lidarr_api_key,
                           transport=httpx.MockTransport(RedirectLidarr()))
    from types import SimpleNamespace
    app3 = SimpleNamespace(state=SimpleNamespace(
        cfg=c3, lidarr=lidarr3, notifier=Notifier(None)))
    result3 = await takeout.import_paths(app3, [FOLDER])
    await lidarr3.close()
    assert redirects["n"] == 1, redirects
    rep3 = result3["reports"][0]
    assert not rep3.get("error"), rep3
    assert rep3["would_commit"] == ["clean.flac"], rep3  # candidates arrived post-redirect
    assert "no candidates found" not in "; ".join(rep3["skipped"]), rep3["skipped"]


if __name__ == "__main__":
    asyncio.run(main())
    print("3 passed")
