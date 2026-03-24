#!/bin/bash

# --- Configuration: choose ONE mode ---
#
# MODE 1 — Static EID
#   Use when registered with flip_e2ee=True (ESP32-style, no GATT server needed).
#   Paste the 40-char Advertisement Key from registration. EIK and PAIR_DATE are
#   ignored. EID never changes; simpler but lower privacy than commercial trackers.
EID=""                # 40-char hex Advertisement Key (leave empty to use Mode 2)
#
# MODE 2 — Rotating EID  (recommended)
#   Use when registered with flip_e2ee=False (Raspberry Pi with GATT server).
#   EID rotates every ~1024 s, matching the behaviour of commercial FHN trackers.
#   Leave EID empty above and fill in EIK and PAIR_DATE below.
EIK="INSERT_YOUR_EIK_64CHAR_HEX_HERE"
PAIR_DATE=0           # Unix timestamp shown at registration (e.g. 1742000000)

# Path to compute_eid.py — same directory as this script (Mode 2 only).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPUTE_EID="$SCRIPT_DIR/compute_eid.py"

# File used to persist the last known good timestamp across reboots.
CLOCK_SAVE_FILE="$SCRIPT_DIR/.last_known_time"

# --- Validate configuration and select mode ---
EID=$(echo "$EID" | tr -d ' ')
if [ -n "$EID" ]; then
    # Mode 1: Static EID
    if [ ${#EID} -ne 40 ]; then
        echo "Error: EID must be exactly 40 hex characters (20 bytes)."
        exit 1
    fi
    ROTATING=0
    echo "Starting Google Find My Device Network Tracker (static EID)..."
else
    # Mode 2: Rotating EID
    EIK=$(echo "$EIK" | tr -d ' ')
    if [ ${#EIK} -ne 64 ]; then
        echo "Error: EIK must be exactly 64 hex characters (32 bytes)."
        exit 1
    fi
    if [ "$PAIR_DATE" -eq 0 ]; then
        echo "Error: PAIR_DATE must be set to the Unix timestamp from registration."
        exit 1
    fi
    ROTATING=1
    echo "Starting Google Find My Device Network Tracker (rotating EID)..."
fi

# --- Compute EID for the current window (Mode 2 only) ---
# Passes the tracked CURRENT_TIME; never reads the system clock.
# Outputs two lines: EID hex, then the timestamp used.
compute_current_eid() {
    python3 "$COMPUTE_EID" "$EIK" "$PAIR_DATE" "$CURRENT_TIME"
}

# --- Persist current time so the next boot can restore it if the clock is wrong ---
save_clock() {
    echo "$1" > "$CLOCK_SAVE_FILE" 2>/dev/null
}

# --- Initialize monotonic time counter (Mode 2 only) ---
# We never read the system clock for EID computation.
# Use PAIR_DATE when it is newer than the saved file, or when no file exists.
if [ "$ROTATING" -eq 1 ]; then
    CURRENT_TIME=""
    if [ -f "$CLOCK_SAVE_FILE" ]; then
        SAVED_TIME=$(cat "$CLOCK_SAVE_FILE" 2>/dev/null)
        if [ -n "$SAVED_TIME" ] && [ "$SAVED_TIME" -gt 0 ] 2>/dev/null; then
            CURRENT_TIME="$SAVED_TIME"
        fi
    fi
    if [ -z "$CURRENT_TIME" ] || [ "$PAIR_DATE" -gt "$CURRENT_TIME" ] 2>/dev/null; then
        CURRENT_TIME="$PAIR_DATE"
        save_clock "$CURRENT_TIME"
        echo "Time initialized from PAIR_DATE: $CURRENT_TIME"
    else
        echo "Time restored from saved file: $CURRENT_TIME"
    fi
fi

# Make sure bluetooth service is running
sudo systemctl is-active --quiet bluetooth || sudo systemctl start bluetooth

# Wait a second for bluetooth to settle
sleep 1

# Bring up the Bluetooth interface
sudo hciconfig hci0 up

# Make the device discoverable (piscom/iscan) so it can broadcast
sudo hciconfig hci0 piscan

# MAC + EID rotation period: 1024 seconds base + 1-204s random jitter (per FHN spec).
# MAC address and EID rotate together at the same time, as required by the spec.
ROTATE_BASE=1024
ROTATE_JITTER_MIN=1
ROTATE_JITTER_MAX=204

# How often to re-enable advertising (in seconds).
# BLE advertising stops when a phone connects via GATT. This interval controls
# how quickly advertising resumes after a disconnect so the device stays visible.
ADV_CHECK_INTERVAL=30

echo "Raspberry Pi is now broadcasting as a Find My Device tracker (MAC + EID rotation enabled)!"

setup_advertising() {
    if [ "$ROTATING" -eq 1 ]; then
        OUTPUT=$(compute_current_eid)
        EID=$(echo "$OUTPUT" | sed -n '1p')
        if [ -z "$EID" ] || [ ${#EID} -ne 40 ]; then
            echo "Error: failed to compute EID (got: '$EID'). Check compute_eid.py and dependencies."
            exit 1
        fi
        echo "EID for this window: $EID (t=$CURRENT_TIME)"
    else
        echo "EID (static): $EID"
    fi
    EID_SPACED=$(echo "$EID" | sed 's/\(..\)/\1 /g')

    # Generate a random 6-byte Non-Resolvable Private Address (top 2 bits = 00)
    RND=$(hexdump -n 6 -e '6/1 "%02X "' /dev/urandom)
    read -r b1 b2 b3 b4 b5 b6 <<< "$RND"
    b6_dec=$(( 16#$b6 & 16#3F ))
    b6=$(printf "%02X" $b6_dec)

    # Disable advertising, set new random address, configure parameters and payload
    sudo hcitool -i hci0 cmd 0x08 0x000a 00 >/dev/null 2>&1
    sudo hcitool -i hci0 cmd 0x08 0x0005 $b1 $b2 $b3 $b4 $b5 $b6 >/dev/null
    sudo hcitool -i hci0 cmd 0x08 0x0006 00 08 00 08 00 01 00 00 00 00 00 00 00 07 00 >/dev/null

    # 1C = 28 bytes. 0x40 = normal FHN frame type.
    sudo hcitool -i hci0 cmd 0x08 0x0008 1C 02 01 06 18 16 AA FE 40 $EID_SPACED 00 00 00 >/dev/null
    sudo hcitool -i hci0 cmd 0x08 0x000a 01 >/dev/null
}

# Initial advertising setup with a fresh MAC address and current EID
JITTER=$(( RANDOM % (ROTATE_JITTER_MAX - ROTATE_JITTER_MIN + 1) + ROTATE_JITTER_MIN ))
NEXT_ROTATE=$(( ROTATE_BASE + JITTER ))
ELAPSED=0
setup_advertising

while true; do
    sleep $ADV_CHECK_INTERVAL
    ELAPSED=$(( ELAPSED + ADV_CHECK_INTERVAL ))

    # Advance and persist the monotonic time counter (Mode 2 only)
    if [ "$ROTATING" -eq 1 ]; then
        CURRENT_TIME=$(( CURRENT_TIME + ADV_CHECK_INTERVAL ))
        save_clock "$CURRENT_TIME"
    fi

    if [ $ELAPSED -ge $NEXT_ROTATE ]; then
        # Time to rotate: new MAC address + new EID for the current window
        setup_advertising
        JITTER=$(( RANDOM % (ROTATE_JITTER_MAX - ROTATE_JITTER_MIN + 1) + ROTATE_JITTER_MIN ))
        NEXT_ROTATE=$(( ROTATE_BASE + JITTER ))
        ELAPSED=0
    else
        # Just re-enable advertising in case a GATT connection stopped it
        sudo hcitool -i hci0 cmd 0x08 0x000a 01 >/dev/null 2>&1
    fi
done
