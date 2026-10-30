#!/bin/bash
# mirror.sh — keep the Pixel's screen flowing into /dev/video10 (v4l2loopback).
# serve.py turns /dev/video10 into /mirror.mjpg for the deck.
# Capture is locked to the phone's natural (portrait) orientation; the deck rotates the frame itself.
#
# Paths, best first: USB, same Wi-Fi (LAN), Tailscale (direct on the same Wi-Fi), WireGuard via the HK hub.
# The WireGuard hop is thin (a few Mbit/s, ~150 ms, lossy from 5G), so it gets a smaller stream.
USB=${MIRROR_USB:-3A110DLJG0019U}
LAN=${MIRROR_LAN:-10.0.110.157:5555}
TS=${MIRROR_TS:-100.64.0.84:5555}
WG=${MIRROR_WG:-10.100.100.19:5555}
SERIAL_FILE="$(dirname "$0")/.mirror-serial"   # serve.py reads this to poll rotation on the same path

pick() {
    if [ "$(adb -s "$USB" get-state 2>/dev/null)" = device ]; then echo "$USB fast"; return; fi
    if timeout 3 adb connect "$LAN" 2>/dev/null | grep -q connected && [ "$(timeout 3 adb -s "$LAN" get-state 2>/dev/null)" = device ]; then
        echo "$LAN fast"; return
    fi
    if timeout 3 adb connect "$TS" 2>/dev/null | grep -q connected && [ "$(timeout 3 adb -s "$TS" get-state 2>/dev/null)" = device ]; then
        echo "$TS fast"; return
    fi
    timeout 10 adb connect "$WG" >/dev/null 2>&1
    echo "$WG slow"
}

while true; do
    read -r DEV SPEED < <(pick)
    echo "$DEV" > "$SERIAL_FILE"
    if [ "$SPEED" = fast ]; then
        Q=(--max-size=1080 --video-bit-rate=4M --max-fps=60)
    else
        Q=(--max-size=720 --video-bit-rate=800K --max-fps=24)
    fi
    echo "mirroring via $DEV ($SPEED)"
    scrcpy -s "$DEV" --v4l2-sink=/dev/video10 --no-video-playback --no-audio --no-control \
        --capture-orientation=@0 --video-codec=h265 "${Q[@]}"
    sleep 2
done
