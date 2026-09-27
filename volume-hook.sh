#!/bin/sh
# shairport-sync's run_this_when_volume_is_set hook. The AirPlay volume
# (-30.0..0.0, -144.0 = mute) arrives as a separate argument; take the last
# non-empty one and forward it to the streamer.
for a in "$@"; do [ -n "$a" ] && v="$a"; done
exec /usr/bin/wget -q -O /dev/null "http://127.0.0.1:${STREAMER_PORT:-8095}/hook/volume?db=$v"
