#!/bin/bash
# mirror.sh — keep the Pixel's screen flowing into /dev/video10 (v4l2loopback) over adb-over-WireGuard.
# serve.py turns /dev/video10 into /mirror.mjpg for the deck.
# Capture is locked to the phone's natural (portrait) orientation; the deck rotates the frame itself.
DEV=${MIRROR_ADB:-10.100.100.19:5555}
while true; do
    adb connect "$DEV" >/dev/null 2>&1
    scrcpy -s "$DEV" --v4l2-sink=/dev/video10 --no-video-playback --no-audio --no-control \
        --capture-orientation=@0 --max-size=1280 --video-bit-rate=6M --max-fps=60 --video-codec=h264
    sleep 2
done
