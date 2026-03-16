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

# Stop any current LE advertising
sudo hciconfig hci0 noleadv

# Set the custom Google FMDN advertisement data payload
# 1D = length (29 bytes)
# 02 01 06 = Flags
# 19 16 AA FE 41 = FMDN Service Header
sudo hcitool -i hci0 cmd 0x08 0x0008 1D 02 01 06 19 16 AA FE 41 $EID_SPACED 00 00 00

# Start LE advertising (3 = non-connectable undirected advertising)
sudo hciconfig hci0 leadv 3

echo "Raspberry Pi is now broadcasting as a Find My Device tracker!"