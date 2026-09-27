# airplay-xiaomi-speaker

Turn a **Xiaomi Smart Speaker Pro** (and probably other XiaoAI speakers) into an
**AirPlay 2 receiver**. It shows up in the AirPlay menu on iPhone, iPad and Mac, and
whatever you play comes out of the speaker.

The speaker can't speak AirPlay itself and its firmware can't be replaced. So a small
server on your LAN acts as the receiver, and Home Assistant tells the speaker to play
that server's live stream.

```
iPhone / Mac ──AirPlay──► shairport-sync ──PCM──► streamer ──MP3 over HTTP──► speaker
                           (receiver)              │                            ▲
                                                   └─ Home Assistant ───────────┘
                                                      media_player.play_media(stream URL)
```

## Contents

- [How it works](#how-it-works)
- [Limitations](#limitations): read these before you start
- [Latency](#latency)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## How it works

Two containers, both on the host network:

1. **`shairport-sync`** ([mikebrady/shairport-sync](https://github.com/mikebrady/shairport-sync),
   the AirPlay 2 build, with [nqptp](https://github.com/mikebrady/nqptp) for timing) announces
   the receiver over mDNS and writes the incoming audio into a FIFO as raw 16-bit 44.1 kHz
   PCM. Its session hooks call the streamer when playback starts or stops and when the
   AirPlay volume changes (the volume goes through the small `volume-hook.sh`).
2. **`streamer`** (this repo, `streamer.py`, Python standard library + ffmpeg):
   - Reads the FIFO and encodes it into an endless MP3 stream at `http://<host>:8095/live.mp3`.
     When nothing is playing it sends silence, so the speaker's connection never starves
     during a pause.
   - **On start**, it calls `media_player.play_media` in Home Assistant with a fresh stream URL
     (`/live.mp3?t=<token>`). The speaker then connects and plays.
   - **On stop**, after a grace period (`STOP_GRACE`, 15 s by default, so a quick
     pause/resume doesn't restart anything), it closes the speaker's connection and retires the
     token. The speaker's repeat mode retries the URL, gets `404` and stops. After that it
     sends `media_pause`, just to keep HA's state tidy.
   - **On volume change**, it maps the AirPlay slider (−30…0 dB) to `media_player.volume_set`
     0…1, debounced so dragging the slider doesn't flood the cloud API.

Why close the stream instead of just calling pause? On the speaker this was built for, Home
Assistant's `media_pause` / `media_play_pause` stop a finite file, but they don't stop an
endless URL stream: the speaker keeps playing. Ending the stream on our side always works.

## Limitations

- **Latency of a few seconds.** The speaker buffers ~4–5 s of the stream before playing. Part
  of that is compensated (see [Latency](#latency)), but a few seconds remain. That's fine for
  music and podcasts, but not for video: the sound will lag behind the picture.
- **Multi-room isn't sample-accurate.** It's announced as an AirPlay 2 receiver, so you can
  group it with other AirPlay 2 speakers, but how well it keeps in step depends on the
  speaker's buffer. Tune `audio_backend_latency_offset_in_seconds` by ear (see
  [Latency](#latency)). The Home app and multi-room grouping haven't been tested much yet.
- **Starting playback needs the internet.** Xiaomi Miot Auto sends the "play this URL"
  command through Xiaomi's cloud. The audio itself goes over your LAN, straight from this
  server to the speaker.
- **One speaker per stack.** For several speakers, run several stacks with different
  receiver names and ports.
- **Pause/next on the speaker.** The speaker's own buttons and voice commands act on the
  speaker's player, not on your iPhone.

## Requirements

- **Home Assistant** with [Xiaomi Miot Auto](https://github.com/al-one/hass-xiaomi-miot)
  (`xiaomi_miot`), with the speaker added through your Xiaomi account (cloud mode), so that it
  has a `media_player` entity that supports `play_media`. The core **Xiaomi Home** integration
  is not enough on its own.
- A **Linux Docker host on the same LAN/subnet** as your iPhone/Mac and the speaker, able to
  run containers with `network_mode: host`. Multicast/mDNS has to reach your devices, so
  Docker Desktop on macOS/Windows won't work. Any small Linux box, VM, LXC or Raspberry Pi
  will do. The streamer image is built for `amd64` and `arm64`.
- The shairport-sync image runs its own dbus + avahi-daemon for mDNS. If the host already
  runs `avahi-daemon`, have the container use the host's instead (see
  [Troubleshooting](#troubleshooting)).
- Free host ports: **TCP 8095** (stream + hooks), **TCP 7000** (AirPlay 2), **UDP 319 and 320**
  (nqptp), plus the ports shairport-sync picks for audio. Only one nqptp can run per host, so
  this can't share a host with another AirPlay 2 shairport-sync.

Tested on the Xiaomi Smart Speaker Pro, model `xiaomi.wifispeaker.oh2p` (the mainland-China
model, Xiaomi account on the `cn` server). Other XiaoAI speakers that Miot Auto can control
through `play_media` should work too, but haven't been tested.

## Setup

### 1. Check that the speaker can play a URL

In Home Assistant, go to **Developer tools → Actions** and run:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.xiaomi_oh2p_xxxx_play_control   # your speaker
data:
  media_content_id: https://download.samplelib.com/mp3/sample-3s.mp3
  media_content_type: music
```

If you hear the sample, you're good. If not, fix this first, because the bridge depends on
it. On the Smart Speaker Pro, Miot Auto names the entity like
`media_player.xiaomi_oh2p_<id>_play_control`.

### 2. Create a Home Assistant token

Go to **Profile → Security → Long-Lived Access Tokens → Create token**. The bridge only calls
`media_player.play_media`, `media_player.media_pause` and `media_player.volume_set`.

### 3. Deploy

```bash
git clone https://github.com/sidun-av/airplay-xiaomi-speaker.git
cd airplay-xiaomi-speaker
cp .env.example .env    # fill in HA_URL, HA_TOKEN, SPEAKER_ENTITY, HOST_IP
docker compose up -d
docker compose logs -f
```

`HOST_IP` is this Docker host's LAN address. The speaker downloads the stream from
`http://HOST_IP:8095/live.mp3`, so it must be reachable from the speaker. `localhost` won't
work.

If you use a stack manager (Portainer, Komodo, Dockge…), point it at this repo's
`docker-compose.yml` and set the four variables in its environment settings. You'll need
`shairport-sync.conf` and `volume-hook.sh` next to the compose file.

**AirPlay 1 only?** Use `image: mikebrady/shairport-sync:classic` and delete the two latency
lines at the top of `shairport-sync.conf`: the classic buffer is only ~2 s, too short for a
−3 s offset. Classic needs TCP 5000 and UDP 6001–6011 instead of 7000/319/320.

### 4. Play something

On an iPhone, open **Control Center → AirPlay** (or the AirPlay button in any app) and pick
**Xiaomi Speaker**. The speaker should start after a couple of seconds.

To rename the receiver, change `name` in `shairport-sync.conf` and restart.

## Latency

The measured delays on a Xiaomi Smart Speaker Pro were:

- **The speaker's own buffer: ~4–5 s.** Between connecting to the stream and starting
  playback, the speaker waits a fixed ~3.5 s plus time to buffer data. All of that becomes
  delay, because the stream is live. Lowering the bitrate doesn't help: at 64k it started
  *later* (~7.4 s) than at 192k (~4.8 s).
- **AirPlay: ~2 s** by default. The sender sets this.

AirPlay 2 senders deliver audio seconds ahead of its play time. So shairport-sync is told to
hand audio over early, with `audio_backend_latency_offset_in_seconds = -3.0` and a larger
`audio_decoded_buffer_desired_length_in_seconds = 5.0`. With that, the first audio reaches the
streamer ~0.8 s after the session starts instead of ~2 s, and the overall delay drops
noticeably.

To tune it, change the offset, restart `shairport-sync`, and listen:
- More negative means less delay. Too far, and the first seconds of each track may get cut off (not measured yet).
- For multi-room, adjust it until the Xiaomi plays in step with the other speakers.

The streamer logs the timing of each session (`first audio X s after start hook`, `speaker
connected X s after start hook`), which helps when tuning.

## Configuration

Streamer environment variables:

| Variable | Default | Description |
|---|---|---|
| `HA_URL` | required | Home Assistant base URL, e.g. `http://homeassistant.local:8123` |
| `HA_TOKEN` | required | Long-lived access token |
| `SPEAKER_ENTITY` | required | The speaker's `media_player` entity |
| `STREAM_URL` | required | The stream URL the speaker is told to play. The compose file builds it from `HOST_IP` |
| `PORT` | `8095` | HTTP port. If you change it, update the hook URLs in `shairport-sync.conf` too |
| `FIFO` | `/pipe/audio` | Path of the PCM FIFO. Must match `pipe.name` in `shairport-sync.conf` |
| `BITRATE` | `192k` | MP3 bitrate |
| `STOP_GRACE` | `15` | Seconds to keep the speaker on the (silent) stream after AirPlay stops, so a quick pause/resume stays seamless |
| `VOLUME_SYNC` | `1` | Forward the AirPlay volume slider to the speaker. Set `0` to control volume on the speaker only |
| `VOLUME_MAX` | `1.0` | Speaker volume at the top of the AirPlay slider (e.g. `0.6` to cap it) |
| `PAUSE_ON_STOP` | `1` | Also send `media_pause` after ending the stream |

The streamer's HTTP endpoints, used by the `shairport-sync` hooks:

| Endpoint | Purpose |
|---|---|
| `GET /live.mp3?t=<token>` | The stream. A token from an older session gets `404` |
| `GET /hook/start` | Playback began: start the speaker (once per session) |
| `GET /hook/stop` | Playback ended: stop the speaker after `STOP_GRACE` |
| `GET /hook/volume?db=<-30..0, -144>` | AirPlay volume changed |
| `GET /health` | Returns `204` |

`/hook/*` can start your speaker and change its volume. Anyone on your LAN can reach them,
so don't expose port 8095 to the internet.

## Troubleshooting

**"Xiaomi Speaker" doesn't show up in the AirPlay menu**
- Check `docker logs airplay-shairport`, and make sure the container keeps running.
- If the host runs its own `avahi-daemon`, add this to the `shairport-sync` service so it uses
  the host's avahi instead of starting a second one (this is the option from the
  [upstream compose example](https://github.com/mikebrady/shairport-sync/blob/master/docker/docker-compose.yaml)):
  ```yaml
      environment:
        - ENABLE_AVAHI=0
      volumes:
        - /var/run/dbus:/var/run/dbus
        - /var/run/avahi-daemon:/var/run/avahi-daemon
  ```
- The iPhone and the Docker host must be on the same subnet, with multicast allowed. Guest
  networks and "client isolation" or "AP isolation" block this.
- On a Mac you can check the announcement with `dns-sd -B _airplay._tcp local` (AirPlay 2) or
  `dns-sd -B _raop._tcp local` (AirPlay 1).

**Loud white noise instead of music**
- The PCM format in the FIFO doesn't match what the streamer reads (16-bit, 44.1 kHz,
  stereo). The AirPlay 2 build writes 32-bit 48 kHz unless the `pipe` section sets
  `output_rate`, `output_format` and `output_channels`, as the shipped `shairport-sync.conf`
  does. Check that your config still has them.

**The speaker is listed, but no sound**
- Check `docker logs airplay-streamer`. You should see `HA play_media ... -> 200`, followed
  by a `GET /live.mp3?t=...` from the speaker's IP.
- If you see `HA play_media failed`, check `HA_URL`, `HA_TOKEN` and `SPEAKER_ENTITY`.
- If `play_media` returns 200 but there's no `GET /live.mp3` from the speaker, the speaker
  can't reach `STREAM_URL`. Check `HOST_IP` and the host firewall (TCP 8095). Also redo
  [step 1](#1-check-that-the-speaker-can-play-a-url).
- If the speaker does fetch the stream but stays silent, check that it isn't muted (the
  `is_volume_muted` attribute in HA) and that its volume isn't 0.

**The speaker keeps playing after I stop AirPlay**
- It stops `STOP_GRACE` seconds (15 by default) after shairport-sync reports that playback
  ended. While the sender only pauses and keeps the AirPlay session open, the speaker keeps
  playing silence, so resuming is instant. Picking another output on the iPhone ends the
  session.

**The volume slider does nothing**
- Look for `GET /hook/volume?db=...` in `docker logs airplay-streamer`. If `docker logs
  airplay-shairport` shows `wget: unrecognized option`, the hook is calling `wget` directly.
  shairport-sync passes the volume as a separate argument, which is why the shipped config
  goes through `volume-hook.sh`.

**The volume jumps when I connect**
- iOS sends its current AirPlay volume when you connect, and the speaker follows it. Lower
  `VOLUME_MAX`, or set `VOLUME_SYNC=0`.

**Testing without an iPhone**

You can push audio straight into the FIFO, the same way shairport-sync does:

```bash
docker exec airplay-streamer sh -c '
  wget -qO- http://127.0.0.1:8095/hook/start
  ffmpeg -loglevel error -re -f lavfi -i sine=frequency=440:duration=8 -af volume=0.3 \
         -ac 2 -ar 44100 -f s16le - > /pipe/audio
  wget -qO- http://127.0.0.1:8095/hook/stop'
```

`pyatv`'s `atvremote stream_file` can't be used as a test sender. It fails to parse
shairport-sync's `/info` reply.

## Development

`streamer.py` is a single file with no dependencies beyond the Python standard library and
`ffmpeg`. The tests run the real script against a fake Home Assistant:

```bash
python3 -m unittest discover -s tests -v   # needs ffmpeg on PATH
```

CI runs the tests on every push and pull request. Pushes to `main` publish
`ghcr.io/sidun-av/airplay-xiaomi-speaker:latest`, and `v*` tags publish versioned images
(`linux/amd64`, `linux/arm64`).

## Not affiliated

This is an unofficial hobby project and isn't affiliated with Xiaomi or Apple. AirPlay is a
trademark of Apple Inc.

## License

[MIT](LICENSE)
