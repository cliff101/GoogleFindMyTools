# GoogleFindMyTools
> Maintained by Gemini 3.1 Pro & Claude Opus 4.6

This repository includes some useful tools that reimplement parts of Google's Find My Device Network (now called Find Hub Network). Note that the code of this repo is still very experimental.

### What's possible?
Currently, it is possible to query Find My Device / Find Hub trackers and Android devices, read out their E2EE keys, and decrypt encrypted locations sent from the Find My Device / Find Hub network. You can also register your own ESP32-, Zephyr-, or Raspberry Pi-based trackers, as described below.

### How to use

> [!CAUTION]
> Before starting, ensure you have Chrome and Python updated.
> 
> **If Chrome is not up to date, the script will NOT work, guaranteed!**

- Clone this repository: `git clone` or download the ZIP file
- Change into the directory: `cd GoogleFindMyTools`
- Optional: Create venv: `python -m venv venv`
- Optional: Activate venv: `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Linux & macOS)
- Install all required packages: `pip install -r requirements.txt`
- Install the latest version of Google Chrome: https://www.google.com/chrome/
- Start the program by running [main.py](main.py): `python main.py` or `python3 main.py`

### Authentication

On the first run, an authentication sequence is executed, which requires a computer with access to Google Chrome.

The authentication results are stored in `Auth/secrets.json`. If you intend to run this tool on a headless machine, you can just copy this file to avoid having to use Chrome.

### Known Issues
- "Your encryption data is locked on your device" is shown if you have never set up Find My Device on an Android device. Solution: Login with your Google Account on an Android device, go to Settings > Google > All Services > Find My Device > Find your offline devices > enable "With network in all areas" or "With network in high-traffic areas only". If "Find your offline devices" is not shown in Settings, you will need to download the Find My Device app from Google's Play Store, and pair a real Find My Device tracker with your device to force-enable the Find My Device network.
- No support for trackers using the P-256 curve and 32-Byte advertisements. Regular trackers don't seem to use this curve at all - I can only confirm that it is used with Sony's WH1000XM5 headphones.
- No support for the authentication process on ARM Linux
- If you receive "ssl.SSLCertVerificationError" when running the script, try to follow [this answer](https://stackoverflow.com/a/53310545).
- Please also consider the issues listed in the [README in the ESP32Firmware folder](ESP32Firmware/README.md) if you want to register custom trackers.

### Firmware for custom ESP32-based trackers
If you want to use an ESP32 as a custom Find My Device tracker, you can find the firmware in the folder ESP32Firmware. To register a new tracker, run main.py and press 'r' if you are asked to. Afterward, follow the instructions on-screen.

For more information, check the [README in the ESP32Firmware folder](ESP32Firmware/README.md).

### Turning a Raspberry Pi into a custom tracker
Because a Raspberry Pi has a built-in Bluetooth adapter (using `BlueZ`), you don't need to compile any custom C firmware for it. You can simply run a bash script to broadcast the tracker advertisement. The EID rotates automatically every ~1024 seconds (synced with MAC rotation), matching the behaviour of commercial FHN trackers.

1. Run `python main.py` on your Windows/Mac/Linux PC (or use the GUI: `python main_gui.py`).
2. Press 'r' to register a new tracker. You will be asked whether to **hide the location** from the official Google Find My Device app. This choice determines which tracker mode to use:

   | | **flip_e2ee = Yes (hide location)** | **flip_e2ee = No (full support)** |
   |---|---|---|
   | FMD app can decrypt location | No | Yes |
   | GATT server required | No | Yes |
   | `fmd_tracker.sh` mode | **Mode 1 — Static EID** | **Mode 2 — Rotating EID** |
   | EID rotates like commercial trackers | No | Yes |
   | Setup complexity | Simplest | Full |

3. Several values will be displayed — save all of them:
   - **Advertisement Key (EID)** (40-char hex / 20 bytes):
     - *Mode 1 (static):* this is the fixed EID the tracker will always broadcast. Paste it into `EID=` in `fmd_tracker.sh`.
     - *Mode 2 (rotating):* this is only the initial EID for reference; the tracker computes EIDs dynamically from EIK + Pair Date.
   - **Ephemeral Identity Key (EIK)** (64-char hex / 32 bytes) — master secret for rotating EIDs, GATT auth, ringing, and location decryption. Required for Mode 2.
   - **Account Key** (32-char hex / 16 bytes) — used by the GATT server for provisioning/device removal.
   - **Pair Date** (Unix timestamp) — registration timestamp, required for Mode 2 (`EID` rotation) and the GATT server.
4. Copy the required files to your Raspberry Pi:
   - **Mode 1 (static EID):** copy `fmd_tracker.sh` only.
   - **Mode 2 (rotating EID):** copy both `fmd_tracker.sh` **and** `compute_eid.py` (both must be in the same directory).
5. Edit the script on your Pi (`nano fmd_tracker.sh`) and fill in the configuration at the top:
   - **Mode 1:** set `EID=` to your 40-char Advertisement Key. Leave `EIK` and `PAIR_DATE` at their defaults.
   - **Mode 2:** leave `EID=""` empty, then set `EIK=` and `PAIR_DATE=`.
6. Make the script executable and run it to test:
   ```bash
   chmod +x fmd_tracker.sh
   sudo ./fmd_tracker.sh
   ```

> [!TIP]
> If you didn't save the keys during registration, you can retrieve them later by selecting the device in `main_gui.py` or `main.py` — the EIK, Account Key, EID, and Pair Date are all displayed when location data is fetched.

**Make it run automatically on boot (Permanent Setup)**
If your Raspberry Pi restarts, the tracker will stop. To keep it running permanently in the background, set it up as a `systemd` service:
1. Copy files to a system path:
   - **Mode 1 (static EID):**
     ```bash
     sudo cp fmd_tracker.sh /usr/local/bin/fmd_tracker.sh
     ```
   - **Mode 2 (rotating EID):**
     ```bash
     sudo cp fmd_tracker.sh /usr/local/bin/fmd_tracker.sh
     sudo cp compute_eid.py /usr/local/bin/compute_eid.py
     ```
2. Create a service file: `sudo nano /etc/systemd/system/fmd_tracker.service`
3. Paste the following configuration:
   ```ini
   [Unit]
   Description=Google Find My Device BLE Tracker
   After=bluetooth.target
   Requires=bluetooth.target

   [Service]
   Type=simple
   Restart=always
   RestartSec=5
   ExecStart=/usr/local/bin/fmd_tracker.sh

   [Install]
   WantedBy=multi-user.target
   ```
4. Enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable fmd_tracker.service
   sudo systemctl start fmd_tracker.service
   ```
   *(You can check if it's running successfully with `sudo systemctl status fmd_tracker.service`)*

### FHN GATT Server (Play Sound / Ring support)

The official Google Find My Device app communicates with trackers over a GATT service defined in the [Find Hub Network Accessory Specification](https://developers.google.com/nearby/fast-pair/specifications/findmy/find-hub-network). Without this service running, the app will show "Connection Failed" when trying to ring your Raspberry Pi tracker.

`fmd_fake_gatt_server.py` implements the Beacon Actions characteristic (`FE2C1238-8366-4814-8EB0-01DE32100BEA`) and handles all FHN operations (ring, provisioning state, unwanted tracking protection, etc.) with proper HMAC-SHA256 authentication. When `--pair-date` is provided, the GATT server reports the correct clock offset and returns the current rotating EID in provisioning state responses.

**Prerequisites on the Raspberry Pi:**
```bash
sudo apt-get install python3-dbus python3-gi python3-pycryptodome python3-ecdsa
```

**Running manually:**
```bash
python fmd_fake_gatt_server.py --eik <EIK> --account-key <AccountKey> --pair-date <PairDate>
```

| Argument | Description | Required |
|---|---|---|
| `--eik` | 64-char hex EIK | Yes |
| `--account-key` | 32-char hex Account Key (needed for provisioning/device removal) | Recommended |
| `--pair-date` | Unix timestamp from registration (enables EID rotation and correct clock) | Recommended |
| `--eid` | 40-char hex EID (fallback only, used if `--pair-date` is not set) | No |
| `--adapter` | Bluetooth adapter (default: `hci0`) | No |

> [!TIP]
> You can get all keys from the GUI: select a device in `main_gui.py` and use the copy buttons for EIK, Account Key, EID, and Pair Date.

**Make it run as a systemd service (Permanent Setup):**

1. Copy the script:
   ```bash
   sudo cp fmd_fake_gatt_server.py /usr/local/bin/fmd_fake_gatt_server.py
   ```

2. Create an environment file to store your keys securely:
   ```bash
   sudo nano /etc/fmd_gatt.conf
   ```
   Paste:
   ```
   EIK=your_64_char_hex_eik_here
   ACCOUNT_KEY=your_32_char_hex_account_key_here
   PAIR_DATE=your_unix_timestamp_here
   ```
   Lock down permissions:
   ```bash
   sudo chmod 600 /etc/fmd_gatt.conf
   ```

3. Create the service file:
   ```bash
   sudo nano /etc/systemd/system/fmd_gatt.service
   ```
   Paste:
   ```ini
   [Unit]
   Description=FHN GATT Server for Google Find My Device
   After=bluetooth.target
   Requires=bluetooth.target

   [Service]
   Type=simple
   Restart=always
   RestartSec=5
   EnvironmentFile=/etc/fmd_gatt.conf
   ExecStart=/usr/bin/python3 /usr/local/bin/fmd_fake_gatt_server.py --eik ${EIK} --account-key ${ACCOUNT_KEY} --pair-date ${PAIR_DATE}

   [Install]
   WantedBy=multi-user.target
   ```

4. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable fmd_gatt.service
   sudo systemctl start fmd_gatt.service
   ```

5. Check status:
   ```bash
   sudo systemctl status fmd_gatt.service
   ```

> [!NOTE]
> Both `fmd_tracker.service` (BLE advertising) and `fmd_gatt.service` (GATT server) should run simultaneously for a fully functional tracker that the Find My Device app can both locate and ring.

### Firmware for custom Zephyr-based trackers
If you want to use a Zephyr-supported BLE device (e.g. nRF51/52) as a custom Find My Device tracker, you can find the firmware in the folder ZephyrFirmware. To register a new tracker, run main.py and press 'r' if you are asked to. Afterward, follow the instructions on-screen.

For more information, check the [README in the ZephyrFirmware folder](ZephyrFirmware/README.md).

### iOS App
You can also use my [iOS App](https://testflight.apple.com/join/rGqa2mTe) to access your Find My Device trackers on the go.
