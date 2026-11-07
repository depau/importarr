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
            assert parse_qs(parsed.query) == {"albumReleaseId": ["3"]}
            return self.respond([{"id": 4, "trackNumber": "1", "title": "song"}])
        if parsed.path == "/api/v1/manualimport":
            self.send_response(307)
            self.send_header("Location", "/api/v1/manualimport/?" + parsed.query)
            self.end_headers()
            return
        assert parsed.path == "/api/v1/manualimport/", self.path
        path = parse_qs(parsed.query)["folder"][0]
        assert os.path.isfile(path), path  # never probe the dump as one album
        self.respond([{"id": 1, "path": path, "name": os.path.basename(path),
                       "artist": None, "album": None, "tracks": [], "downloadId": None,
                       "quality": {"quality": {"id": 1}},
                       "rejections": [{"reason": "unknown artist"}]}])

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/chat/completions":
            user = json.loads(data["messages"][1]["content"])
            candidate = user["candidates"][0]
            path = candidate["path"]
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
                          "albumId": 2, "albumReleaseId": 3, "trackId": 4}),
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
            self.respond([{"id": 1, "path": item["path"], "name": item["name"],
                           "artist": {"id": 1, "artistName": "Known Artist"},
                           "album": {"id": 2, "title": "Known Album"},
                           "albumReleaseId": 3, "tracks": [{"id": 5, "title": "wrong song"}],
                           "downloadId": None, "quality": item["quality"], "rejections": rejections}])
        elif self.path == "/api/v1/command":
            commands.append(data)
            self.respond({"status": "queued"})
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
            for name, artist in [("song.flac", "Known Artist"), ("nested/unknown.flac", "Unknown Artist"),
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
            for dry in ("true", "false"):
                commands.clear()
                seen.clear()
                result = subprocess.run([sys.executable, "-m", "importarr", "import", str(dump)],
                                        cwd=ROOT, env={**env, "DRY_RUN": dry},
                                        capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr
                assert len(seen) == 4, seen
                assert "Known Artist" in json.dumps(seen), seen
                assert "unknown.flac" in result.stdout and "no matching artist" in result.stdout, result.stdout
                if dry == "true":
                    assert not commands, commands
                    assert "would commit" in result.stdout, result.stdout
                else:
                    assert len(commands) == 1, commands
                    assert commands[0]["files"][0]["path"].endswith("song.flac"), commands
                    assert commands[0]["files"][0]["trackIds"] == [4], commands
                    assert commands[0]["importMode"] == "copy", commands
    finally:
        server.shutdown()
        server.server_close()
    print("mixed dump dry-run and import passed")


if __name__ == "__main__":
    main()
