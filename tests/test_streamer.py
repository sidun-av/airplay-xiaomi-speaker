"""End-to-end test of streamer.py against a fake Home Assistant.

Runs the real script (needs ffmpeg on PATH), feeds PCM into its FIFO the way
shairport-sync does, and checks the HA calls and the stream lifecycle.
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHA(http.server.BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHA.calls.append((self.path, self.headers["Authorization"], body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[]")


def wait_for(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class StreamerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ha = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)
        threading.Thread(target=cls.ha.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fifo = os.path.join(cls.tmp.name, "audio")
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(
            os.environ,
            FIFO=cls.fifo,
            PORT=str(cls.port),
            HA_URL=f"http://127.0.0.1:{cls.ha.server_port}",
            HA_TOKEN="test-token",
            SPEAKER_ENTITY="media_player.test_speaker",
            STREAM_URL=f"{cls.base}/live.mp3",
            STOP_GRACE="0.5",
        )
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "streamer.py")], env=env)

        def up():
            try:
                urllib.request.urlopen(f"{cls.base}/health", timeout=1)
                return True
            except OSError:
                return False

        assert wait_for(up), "streamer did not start"

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.ha.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeHA.calls.clear()

    def hook(self, path):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as r:
            self.assertEqual(r.status, 204)

    def calls(self, service):
        return [c for c in FakeHA.calls if c[0] == f"/api/services/media_player/{service}"]

    def test_session_lifecycle(self):
        self.hook("/hook/start")
        self.assertTrue(wait_for(lambda: self.calls("play_media")))
        _, auth, body = self.calls("play_media")[0]
        self.assertEqual(auth, "Bearer test-token")
        self.assertEqual(body["entity_id"], "media_player.test_speaker")
        self.assertEqual(body["media_content_type"], "music")
        url = body["media_content_id"]
        self.assertRegex(url, r"/live\.mp3\?t=\d+$")

        # A second start inside the same session must not re-issue play_media.
        self.hook("/hook/start")
        time.sleep(0.3)
        self.assertEqual(len(self.calls("play_media")), 1)

        # The stream is MP3 and carries real audio written to the FIFO.
        stream = urllib.request.urlopen(url, timeout=5)
        self.assertEqual(stream.headers["Content-Type"], "audio/mpeg")
        with open(self.fifo, "wb") as f:
            f.write(b"\x10\x00" * 44100 * 2)
        data = stream.read(8192)
        self.assertGreater(len(data), 1000)
        self.assertTrue(data[:3] == b"ID3" or data[0] == 0xFF, data[:4])

        # Stop: after the grace period the stream ends, the old token is
        # refused, and media_pause is sent.
        self.hook("/hook/stop")
        self.assertTrue(wait_for(lambda: self.calls("media_pause")))

        def drained():
            try:
                return stream.read(65536) == b""
            except OSError:
                return True

        self.assertTrue(wait_for(drained))
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(err.exception.code, 404)

    def test_quick_resume_keeps_stream(self):
        self.hook("/hook/start")
        self.assertTrue(wait_for(lambda: self.calls("play_media")))
        self.hook("/hook/stop")
        self.hook("/hook/start")  # resumed within STOP_GRACE
        time.sleep(1.0)
        self.assertEqual(self.calls("media_pause"), [])
        self.assertEqual(len(self.calls("play_media")), 1)
        self.hook("/hook/stop")
        self.assertTrue(wait_for(lambda: self.calls("media_pause")))

    def test_volume_mapping(self):
        for db, level in (("-15.0", 0.5), ("0.0", 1.0), ("-144.0", 0.0)):
            FakeHA.calls.clear()
            self.hook(f"/hook/volume?db={db}")
            self.assertTrue(wait_for(lambda: self.calls("volume_set")))
            self.assertEqual(self.calls("volume_set")[0][2]["volume_level"], level)

    def test_volume_debounce(self):
        for db in ("-30.0", "-20.0", "-10.0", "-3.0"):
            self.hook(f"/hook/volume?db={db}")
        time.sleep(1.0)
        levels = [c[2]["volume_level"] for c in self.calls("volume_set")]
        self.assertEqual(levels, [0.9])

    def test_bad_requests(self):
        for path in ("/hook/volume", "/hook/volume?db=abc", "/nope", "/live.mp3?t=stale"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as err:
                urllib.request.urlopen(f"{self.base}{path}", timeout=5)
            self.assertIn(err.exception.code, (400, 404))


if __name__ == "__main__":
    unittest.main()
