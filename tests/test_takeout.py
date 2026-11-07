"""Mixed dump CLI regression with real tagged FLAC files and fake HTTP services."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mutagen.flac import FLAC

ROOT = Path(__file__).resolve().parents[1]
commands = []
seen = []
scenario = {"state": "incomplete", "dump": ""}
completed_ids = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/album/2":
            return self.respond({"id": 2, "releases": [{"id": 3}]})
        if parsed.path == "/api/v1/track":
            params = parse_qs(parsed.query)
            existing = scenario["state"] in ("existing_mp3", "existing_flac", "existing_unknown")
            if "trackIds" in params:
                track_id = int(params["trackIds"][0])
                seen_track = scenario["state"] in ("already_imported",) or existing
                return self.respond([{"id": track_id, "albumId": 2, "hasFile": seen_track,
                                      "trackFileId": 100 + track_id if seen_track else 0}])
            if params == {"albumId": ["2"]}:
                after_import = len([c for c in commands if c["name"] == "ManualImport"]) >= 2
                present = existing or (after_import and scenario["state"] != "unconfirmed")
                def fid(base):
                    if existing and after_import and scenario["state"] in ("existing_mp3",):
                        return 1000 + base  # upgrade replaced the file
                    return base if existing else 0
                return self.respond([{"id": 4, "hasFile": present, "trackFileId": fid(104)},
                                     {"id": 5, "hasFile": present, "trackFileId": fid(105)},
                                     {"id": 6, "hasFile": after_import and scenario["state"] == "complete"}])
            assert params == {"albumReleaseId": ["3"]}
            return self.respond([{"id": 4, "trackNumber": "1", "title": "song"},
                                 {"id": 5, "trackNumber": "2", "title": "second"}])
        if parsed.path.startswith("/api/v1/trackfile/"):
            file_id = int(parsed.path.rsplit("/", 1)[-1])
            if scenario["state"] == "already_imported":
                name = "song.flac" if file_id == 104 else "second.flac"
                return self.respond({"id": file_id, "albumId": 2,
                                     "path": f"{scenario['dump']}/{name}"})
            ext = {"existing_mp3": ".mp3", "existing_flac": ".flac",
                   "existing_unknown": ".unknown"}[scenario["state"]]
            return self.respond({"id": file_id, "albumId": 2, "path": f"/music/{file_id}{ext}"})
        if parsed.path.startswith("/api/v1/command/"):
            completed_ids.add(parsed.path.rsplit("/", 1)[-1])
            status = "failed" if scenario["state"] == "failed" else "completed"
            return self.respond({"status": status, "result": "successful"})
        if parsed.path == "/api/v1/manualimport":
            self.send_response(307)
            self.send_header("Location", "/api/v1/manualimport/?" + parsed.query)
            self.end_headers()
            return
        assert parsed.path == "/api/v1/manualimport/", self.path
        path = parse_qs(parsed.query)["folder"][0]
        assert os.path.isfile(path), path  # never probe the dump as one album
        tracks = []
        rejections = [{"reason": "unknown artist"}]
        if scenario["state"] == "already_imported" and os.path.basename(path) in ("song.flac", "second.flac"):
            track_id = 4 if os.path.basename(path) == "song.flac" else 5
            tracks = [{"id": track_id}]
            rejections = [{"reason": "Has missing tracks"}]
        self.respond([{"id": 1, "path": path, "name": os.path.basename(path),
                       "artist": None, "album": None, "tracks": tracks, "downloadId": None,
                       "quality": {"quality": {"id": 1}},
                       "rejections": rejections}])

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/chat/completions":
            user = json.loads(data["messages"][1]["content"])
            candidate = user["candidates"][0]
            path = candidate["path"]
            track_id = 5 if path.endswith("second.flac") else 4
            seen.append((path, candidate["audioTags"]))
            if path.endswith("unknown.flac"):
                calls = [("finish", {"outcome": "gave_up", "summary": "no matching artist"})]
            elif path.endswith("malicious.flac"):
                calls = [("get_album_details", {"albumId": 2, "albumReleaseId": 3}),
                         ("update_manual_import", {"path": path, "artistId": 1,
                          "albumId": 2, "albumReleaseId": 3, "trackId": 4}),
                         ("commit_import", {"path": "/etc/passwd"}),
                         ("finish", {"outcome": "imported", "summary": "attempted"})]
            else:
                calls = [("get_album_details", {"albumId": 2, "albumReleaseId": 3}),
                         ("update_manual_import", {"path": path, "artistId": 1,
                          "albumId": 2, "albumReleaseId": 3, "trackId": track_id}),
                         ("commit_import", {"path": path}),
                         ("finish", {"outcome": "imported", "summary": "matched tags"})]
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": str(i), "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(calls)]}
            self.respond({"choices": [{"message": msg}]})
        elif self.path == "/api/v1/manualimport":
            item = data[0]
            rejections = [{"reason": "Has missing tracks", "type": "permanent"}]
            if item["path"].endswith("blocked.flac"):
                rejections.append({"reason": "Disk is not writable", "type": "permanent"})
            if scenario["state"] == "existing_mp3":
                rejections.append({"reason": "Not an upgrade for existing track file(s)", "type": "permanent"})
            self.respond([{"id": 1, "path": item["path"], "name": item["name"],
                           "artist": {"id": 1, "artistName": "Known Artist"},
                           "album": {"id": 2, "title": "Known Album"},
                           "albumReleaseId": 3, "tracks": [{"id": 5, "title": "wrong song"}],
                           "downloadId": None, "quality": item["quality"], "rejections": rejections}])
        elif self.path == "/api/v1/command":
            if data["name"] == "AlbumSearch":
                assert completed_ids == {"1", "2"}, completed_ids
            commands.append(data)
            self.respond({"id": len(commands), "status": "queued"})
        else:
            raise AssertionError(self.path)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump"
            (dump / "nested").mkdir(parents=True)
            for name, artist in [("song.flac", "Known Artist"), ("second.flac", "Known Artist"),
                                 ("nested/unknown.flac", "Unknown Artist"),
                                 ("malicious.flac", "Untrusted Artist"), ("blocked.flac", "Known Artist")]:
                path = dump / name
                shutil.copyfile(ROOT / "tests/fixtures/one.flac", path)
                audio = FLAC(path)
                audio["artist"] = artist
                audio["album"] = "Known Album"
                audio.save()
            base = f"http://127.0.0.1:{server.server_port}"
            env = {**os.environ, "LIDARR_URL": base, "LIDARR_API_KEY": "test",
                   "LLM_BASE_URL": base + "/v1", "LLM_MODEL": "test",
                   "AUDIO_PATHS": str(dump)}
            scenario["dump"] = str(dump)
            for dry, state in (("true", "incomplete"), ("false", "incomplete"),
                               ("false", "complete"), ("false", "failed"),
                               ("false", "unconfirmed"), ("false", "existing_mp3"),
                               ("false", "existing_flac"), ("false", "existing_unknown"),
                               ("false", "already_imported")):
                scenario["state"] = state
                print("scenario:", dry, state)
                commands.clear()
                completed_ids.clear()
                seen.clear()
                result = subprocess.run([sys.executable, "-m", "importarr", "import", str(dump)],
                                        cwd=ROOT, env={**env, "DRY_RUN": dry},
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr
                seen_names = sorted(Path(p).name for p, _ in seen)
                assert len(seen) == (5 if state != "already_imported" else 3), (state, seen_names)
                if state == "already_imported":
                    assert "already the library file" in result.stdout, result.stdout
                    assert not commands, commands
                    continue
                assert "Known Artist" in json.dumps(seen), seen
                assert "unknown.flac" in result.stdout and "no matching artist" in result.stdout, result.stdout
                if dry == "true":
                    assert not commands, commands
                    assert "would commit" in result.stdout, result.stdout
                else:
                    imports = [c for c in commands if c["name"] == "ManualImport"]
                    searches = [c for c in commands if c["name"] == "AlbumSearch"]
                    assert len(imports) == (0 if state in ("existing_flac", "existing_unknown") else 2), commands
                    if imports:
                        assert {Path(c["files"][0]["path"]).name for c in imports} == {"song.flac", "second.flac"}, commands
                        assert {c["files"][0]["trackIds"][0] for c in imports} == {4, 5}, commands
                        assert all(c["importMode"] == "copy" and not c["replaceExistingFiles"] for c in imports), commands
                    assert searches == ([{"name": "AlbumSearch", "albumIds": [2]}]
                                        if state in ("incomplete", "existing_mp3") else []), commands
    finally:
        server.shutdown()
        server.server_close()
    print("mixed dump dry-run and import passed")


if __name__ == "__main__":
    main()
