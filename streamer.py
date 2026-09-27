"""AirPlay -> Xiaomi Smart Speaker bridge (https://github.com/sidun-av/airplay-xiaomi-speaker).

shairport-sync writes raw PCM (s16le, 44.1 kHz, stereo) into a FIFO. This
service turns it into an endless MP3 stream at /live.mp3 (silence when
nothing plays, so the speaker's connection never starves) and tells the
speaker, through Home Assistant, to start/stop playing that stream and to
follow the AirPlay volume slider. shairport-sync reaches us via its
sessioncontrol hooks: /hook/start, /hook/stop, /hook/volume?db=<-30..0|-144>.
"""

import http.server
import sys
import json
import logging
import os
import queue
import socketserver
import subprocess
import threading
import time
import urllib.parse
import urllib.request

_missing = [v for v in ("HA_URL", "HA_TOKEN", "SPEAKER_ENTITY", "STREAM_URL") if not os.environ.get(v)]
if _missing:
    sys.exit(f"missing required environment variables: {', '.join(_missing)}")

FIFO = os.environ.get("FIFO", "/pipe/audio")
PORT = int(os.environ.get("PORT", "8095"))
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_AUTH = os.environ["HA_TOKEN"]
SPEAKER = os.environ["SPEAKER_ENTITY"]
STREAM_URL = os.environ["STREAM_URL"]
BITRATE = os.environ.get("BITRATE", "192k")
# How long to keep the speaker on the (now silent) stream after AirPlay
# stops, so a quick pause/resume doesn't re-issue play_media.
STOP_GRACE = float(os.environ.get("STOP_GRACE", "15"))
# AirPlay volume -> speaker volume. Empty/0 disables volume sync.
VOLUME_SYNC = os.environ.get("VOLUME_SYNC", "1") not in ("", "0", "false", "no")
VOLUME_MAX = float(os.environ.get("VOLUME_MAX", "1.0"))
# Also send media_pause after closing the stream (keeps HA's state tidy).
PAUSE_ON_STOP = os.environ.get("PAUSE_ON_STOP", "1") not in ("", "0", "false", "no")

RATE, CHANNELS, SAMPLE_BYTES = 44100, 2, 2
TICK = 0.02
TICK_BYTES = int(RATE * TICK) * CHANNELS * SAMPLE_BYTES
MAX_BUFFER = RATE * CHANNELS * SAMPLE_BYTES  # 1 s; beyond that we drop old audio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("streamer")

pcm = bytearray()
pcm_lock = threading.Lock()
clients: set[queue.Queue] = set()
clients_lock = threading.Lock()


def read_fifo():
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO, 0o666)
    os.chmod(FIFO, 0o666)
    # O_RDWR keeps a writer attached, so we never see EOF between sessions.
    fd = os.open(FIFO, os.O_RDWR)
    while True:
        data = os.read(fd, 65536)
        session.saw_audio(data)
        with pcm_lock:
            pcm.extend(data)
            if len(pcm) > MAX_BUFFER:
                del pcm[: len(pcm) - MAX_BUFFER]


def feed_encoder(stdin):
    """Push PCM to ffmpeg at real-time pace, padding gaps with silence."""
    silence = bytes(TICK_BYTES)
    nxt = time.monotonic()
    while True:
        with pcm_lock:
            chunk = bytes(pcm[:TICK_BYTES])
            del pcm[:TICK_BYTES]
        if len(chunk) < TICK_BYTES:
            chunk += silence[len(chunk):]
        stdin.write(chunk)
        stdin.flush()
        nxt += TICK
        delay = nxt - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            nxt = time.monotonic()


def broadcast(stdout):
    while True:
        data = stdout.read1(4096)
        if not data:
            raise RuntimeError("ffmpeg exited")
        with clients_lock:
            for q in list(clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    clients.discard(q)


def kick_clients():
    """End every open stream; the handler exits on a None chunk."""
    with clients_lock:
        for q in clients:
            with q.mutex:
                q.queue.clear()
            q.put_nowait(None)
        clients.clear()


def run_encoder():
    while True:
        proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE),
             "-ac", str(CHANNELS), "-i", "pipe:0", "-c:a", "libmp3lame",
             "-b:a", BITRATE, "-flush_packets", "1", "-f", "mp3", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        threading.Thread(target=feed_encoder, args=(proc.stdin,), daemon=True).start()
        try:
            broadcast(proc.stdout)
        except Exception as e:  # noqa: BLE001
            log.error("encoder: %s, restarting", e)
            proc.kill()
            time.sleep(1)


def ha(service, **data):
    body = json.dumps({"entity_id": SPEAKER, **data}).encode()
    req = urllib.request.Request(
        f"{HA_URL}/api/services/media_player/{service}", data=body, method="POST",
        headers={"Authorization": f"Bearer {HA_AUTH}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log.info("HA %s %s -> %s", service, data, r.status)
    except Exception as e:  # noqa: BLE001
        log.error("HA %s failed: %s", service, e)


class Session:
    """Start/stop the speaker, debounced, one HA call at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.playing = False
        self.token = None  # only /live.mp3?t=<token> of the current session streams
        self.stop_timer = None
        self.hook_at = None  # monotonic time of the last /hook/start, for timing logs
        self.vol_level = None
        self.vol_cond = threading.Condition()
        threading.Thread(target=self._volume_worker, daemon=True).start()

    def start(self):
        with self.lock:
            self.hook_at = time.monotonic()
            if self.stop_timer:
                self.stop_timer.cancel()
                self.stop_timer = None
            if self.playing:
                return
            self.playing = True
            # Unique URL so the speaker doesn't treat it as "same track, resume".
            self.token = str(time.time_ns())
        url = f"{STREAM_URL}?t={self.token}"
        threading.Thread(target=ha, args=("play_media",),
                         kwargs={"media_content_id": url, "media_content_type": "music"},
                         daemon=True).start()

    def stop(self):
        with self.lock:
            if self.stop_timer:
                self.stop_timer.cancel()
            self.stop_timer = threading.Timer(STOP_GRACE, self._stop_now)
            self.stop_timer.daemon = True
            self.stop_timer.start()

    def _stop_now(self):
        with self.lock:
            self.stop_timer = None
            if not self.playing:
                return
            self.playing = False
            self.token = None
        # media_pause over the Xiaomi cloud doesn't stop a URL stream on this
        # speaker, so end the stream ourselves; the stale token then gets 404,
        # which keeps repeat mode from reconnecting.
        kick_clients()
        if PAUSE_ON_STOP:
            ha("media_pause")

    def since_hook(self):
        """Seconds since the last /hook/start (for timing logs), or None."""
        at = self.hook_at
        return None if at is None else time.monotonic() - at

    def saw_audio(self, data):
        """Log how long after /hook/start the first non-silent PCM arrived."""
        if self.hook_at is None or data.count(0) == len(data):
            return
        log.info("first audio %.2fs after start hook", self.since_hook())
        self.hook_at = None

    def volume(self, db):
        if not VOLUME_SYNC:
            return
        level = 0.0 if db <= -30 else min(1.0, max(0.0, (db + 30) / 30)) * VOLUME_MAX
        with self.vol_cond:
            self.vol_level = round(level, 2)
            self.vol_cond.notify()

    def _volume_worker(self):
        # One call at a time, always with the latest level: cloud calls take
        # seconds, and parallel ones could land out of order.
        while True:
            with self.vol_cond:
                while self.vol_level is None:
                    self.vol_cond.wait()
            time.sleep(0.4)  # debounce slider drags
            with self.vol_cond:
                level, self.vol_level = self.vol_level, None
            ha("volume_set", volume_level=level)


session = Session()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/live.mp3":
            t = urllib.parse.parse_qs(u.query).get("t", [None])[0]
            if t is not None and t != session.token:
                return self.send_error(404)
            return self.stream()
        if u.path == "/hook/start":
            session.start()
        elif u.path == "/hook/stop":
            session.stop()
        elif u.path == "/hook/volume":
            try:
                session.volume(float(urllib.parse.parse_qs(u.query)["db"][0]))
            except (KeyError, ValueError):
                return self.send_error(400)
        elif u.path == "/health":
            pass
        else:
            return self.send_error(404)
        self.send_response(204)
        self.end_headers()

    def stream(self):
        if (t := session.since_hook()) is not None:
            log.info("speaker connected %.2fs after start hook", t)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("icy-name", "AirPlay")
        self.end_headers()
        # A paused speaker keeps the socket open but stops reading; drop it.
        self.connection.settimeout(30)
        q: queue.Queue = queue.Queue(maxsize=500)
        with clients_lock:
            clients.add(q)
        try:
            while (chunk := q.get(timeout=10)) is not None:
                self.wfile.write(chunk)
        except (OSError, queue.Empty):
            pass
        finally:
            with clients_lock:
                clients.discard(q)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    threading.Thread(target=read_fifo, daemon=True).start()
    threading.Thread(target=run_encoder, daemon=True).start()
    log.info("listening on :%d, speaker %s", PORT, SPEAKER)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
