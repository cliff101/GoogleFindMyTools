#!/bin/bash

# Replace this with your 40-character Advertisement Key from main.py
EID="INSERT_YOUR_ADVERTISEMENT_KEY_HERE"

# Remove any spaces and ensure correct length
EID=$(echo $EID | sed 's/ //g')

if [ ${#EID} -ne 40 ]; then
    echo "Error: EID must be exactly 40 characters long."
    exit 1
fi

# Format the EID into space-separated bytes for hcitool
EID_SPACED=$(echo $EID | sed 's/\(..\)/\1 /g')

echo "Starting Google Find My Device Network Tracker..."

# Make sure bluetooth service is running
sudo systemctl is-active --quiet bluetooth || sudo systemctl start bluetooth

# Wait a second for bluetooth to settle
sleep 1

# Bring up the Bluetooth interface
sudo hciconfig hci0 up

# Make the device discoverable (piscom/iscan) so it can broadcast
sudo hciconfig hci0 piscan

# MAC rotation period: 1024 seconds base + 1-204s random jitter (per FHN spec).
ROTATE_BASE=1024
ROTATE_JITTER_MIN=1
ROTATE_JITTER_MAX=204

# How often to re-enable advertising (in seconds).
# BLE advertising stops when a phone connects via GATT. This interval controls
# how quickly advertising resumes after a disconnect so the device stays visible.
ADV_CHECK_INTERVAL=30

echo "Raspberry Pi is now broadcasting as a Find My Device tracker with privacy (MAC rotation) enabled!"

setup_advertising() {
    # Generate a random 6-byte Non-Resolvable Private Address (top 2 bits = 00)
    RND=$(hexdump -n 6 -e '6/1 "%02X "' /dev/urandom)
    read -r b1 b2 b3 b4 b5 b6 <<< "$RND"
    b6_dec=$(( 16#$b6 & 16#3F ))
    b6=$(printf "%02X" $b6_dec)

    # Disable advertising, set new random address, configure parameters and payload
    sudo hcitool -i hci0 cmd 0x08 0x000a 00 >/dev/null 2>&1
    sudo hcitool -i hci0 cmd 0x08 0x0005 $b1 $b2 $b3 $b4 $b5 $b6 >/dev/null
    sudo hcitool -i hci0 cmd 0x08 0x0006 00 08 00 08 00 01 00 00 00 00 00 00 00 07 00 >/dev/null

    # 1C = 28 bytes (no hashed flags). 0x40 = normal FHN frame.
    sudo hcitool -i hci0 cmd 0x08 0x0008 1C 02 01 06 18 16 AA FE 40 $EID_SPACED 00 00 00 >/dev/null
    sudo hcitool -i hci0 cmd 0x08 0x000a 01 >/dev/null
}

# Initial advertising setup with a fresh MAC address
JITTER=$(( RANDOM % (ROTATE_JITTER_MAX - ROTATE_JITTER_MIN + 1) + ROTATE_JITTER_MIN ))
NEXT_ROTATE=$(( ROTATE_BASE + JITTER ))
ELAPSED=0
setup_advertising

while true; do
    sleep $ADV_CHECK_INTERVAL
    ELAPSED=$(( ELAPSED + ADV_CHECK_INTERVAL ))

    if [ $ELAPSED -ge $NEXT_ROTATE ]; then
        # Time to rotate: new MAC address + fresh advertising setup
        setup_advertising
        JITTER=$(( RANDOM % (ROTATE_JITTER_MAX - ROTATE_JITTER_MIN + 1) + ROTATE_JITTER_MIN ))
        NEXT_ROTATE=$(( ROTATE_BASE + JITTER ))
        ELAPSED=0
    else
        # Just re-enable advertising in case a GATT connection stopped it
        sudo hcitool -i hci0 cmd 0x08 0x000a 01 >/dev/null 2>&1
    fi
done