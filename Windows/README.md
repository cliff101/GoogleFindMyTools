### FHN GATT Server for Windows (Play Sound / Ring support)

The official Google Find My Device app communicates with trackers over a GATT service defined in the [Find Hub Network Accessory Specification](https://developers.google.com/nearby/fast-pair/specifications/findmy/find-hub-network). Without this service running, the app will show "Connection Failed" when trying to ring your Windows-based tracker.

`fmd_fake_gatt_server_windows.py` is the Windows port of the Raspberry Pi GATT server. It uses [PyWinRT](https://github.com/pywinrt/pywinrt) (`winrt-*` packages) for the BLE peripheral APIs (`Windows.Devices.Bluetooth.GenericAttributeProfile`) instead of BlueZ/D-Bus. It implements the Beacon Actions characteristic (`FE2C1238-8366-4814-8EB0-01DE32100BEA`) and handles all FHN operations (ring, provisioning state, unwanted tracking protection, etc.) with proper HMAC-SHA256 authentication. The EID rotates automatically every ~1024 seconds, matching the behaviour of commercial FHN trackers.

When the app sends a ring command, the server plays an audible beep through the Windows speaker using `winsound`.

> [!IMPORTANT]
> Your Bluetooth adapter **must support the BLE peripheral (GATT server) role**. Many built-in laptop adapters do **not**. If the script prints `Bluetooth adapter does not support peripheral (GATT server) role!`, you need a compatible USB BLE dongle.

**Prerequisites:**

Use **64-bit CPython on Windows** (3.9+ recommended; PyWinRT ships pre-built wheels for current CPython releases).

From the repository root, install the main project, then the Windows BLE extras:

```
python -m pip install -r requirements.txt
python -m pip install -r requirements-windows-ble.txt
```

Or install the script dependencies only (same packages as `requirements-windows-ble.txt`, plus `psutil` for battery reporting):

```
python -m pip install pycryptodomex ecdsa psutil winrt-windows-foundation==3.2.1 winrt-windows-foundation-collections==3.2.1 winrt-windows-devices-bluetooth==3.2.1 winrt-windows-devices-bluetooth-genericattributeprofile==3.2.1 winrt-windows-devices-bluetooth-advertisement==3.2.1 winrt-windows-storage-streams==3.2.1
```

**Running manually:**
```
python fmd_fake_gatt_server_windows.py --eik <EIK> --account-key <AccountKey> --pair-date <PairDate>
```

| Argument | Description | Required |
|---|---|---|
| `--eik` | 64-char hex EIK (Ephemeral Identity Key) | Yes |
| `--account-key` | 32-char hex Account Key (needed for provisioning/device removal) | Recommended |
| `--pair-date` | Unix timestamp from registration (enables EID rotation and correct clock) | Recommended |

> [!TIP]
> You can get all keys from the GUI: select a device in `main_gui.py` and use the copy buttons for EIK, Account Key, and Pair Date.

**Make it run automatically at startup (Task Scheduler)**

If your PC restarts, the GATT server will stop. To keep it running permanently in the background, create a Windows Task Scheduler task.

The easiest way is to import the task from an **elevated** (Run as Administrator) PowerShell. Replace the placeholder values with your actual keys and paths:

```powershell
$python = "C:\Users\YourUser\AppData\Local\Programs\Python\Python312\pythonw.exe"   # or venv: C:\path\to\venv\Scripts\pythonw.exe
$script = "C:\Users\YourUser\GoogleFindMyTools\Windows\fmd_fake_gatt_server_windows.py"
$workdir = "C:\Users\YourUser\GoogleFindMyTools\Windows"
$eik = "YOUR_EIK"
$accountKey = "YOUR_ACCOUNT_KEY"
$pairDate = "YOUR_PAIR_DATE"

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "$script --eik $eik --account-key $accountKey --pair-date $pairDate" `
    -WorkingDirectory $workdir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit 0 `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "FHN GATT Server" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest `
    -Description "FHN GATT Server for Google Find My Device"

Start-ScheduledTask -TaskName "FHN GATT Server"
```

> [!NOTE]
> The command above uses **`pythonw.exe`** (not `python.exe`) so the script runs as a background process with no console window. The trigger is set to **At log on** so Bluetooth is available. If your adapter works before login you can change `-AtLogOn` to `-AtStartup`.

You can also create the task manually through the GUI:

1. Open **Task Scheduler** (`taskschd.msc` or search for "Task Scheduler" in Start).

2. Click **Create Task** (not "Create Basic Task") in the right-hand Actions pane.

3. **General** tab:
   - Name: `FHN GATT Server`
   - Select **Run only when user is logged on**
   - Check **Run with highest privileges**

4. **Triggers** tab → **New…**:
   - Begin the task: **At log on**
   - Optionally add a delay (e.g. 30 seconds) to let Bluetooth initialise

5. **Actions** tab → **New…**:
   - Action: **Start a program**
   - Program/script: path to `pythonw.exe` (runs without a console window), e.g.:
     ```
     C:\Users\YourUser\AppData\Local\Programs\Python\Python312\pythonw.exe
     ```
     or if using a venv:
     ```
     C:\path\to\venv\Scripts\pythonw.exe
     ```
   - Add arguments:
     ```
     fmd_fake_gatt_server_windows.py --eik YOUR_EIK --account-key YOUR_ACCOUNT_KEY --pair-date YOUR_PAIR_DATE
     ```
   - Start in: the full path to the `Windows` folder, e.g.:
     ```
     C:\Users\YourUser\GoogleFindMyTools\Windows
     ```

6. **Conditions** tab:
   - Uncheck **Start the task only if the computer is on AC power** (so it runs on battery too)

7. **Settings** tab:
   - Check **If the task fails, restart every:** `1 minute`, up to `3` times
   - Uncheck **Stop the task if it runs longer than:** (the server is meant to run indefinitely)
   - Set **If the task is already running:** to **Do not start a new instance**

8. Click **OK**.

You can verify it is running from an elevated PowerShell:
```powershell
Get-ScheduledTask -TaskName "FHN GATT Server" | Get-ScheduledTaskInfo
```

To start or stop the task manually:
```powershell
Start-ScheduledTask -TaskName "FHN GATT Server"
Stop-ScheduledTask -TaskName "FHN GATT Server"
```

> [!NOTE]
> For a fully functional tracker that the Find My Device app can both locate and ring, you also need a separate process broadcasting BLE advertisements with the current EID (e.g. `test_win_adv_eid.py`). The GATT server handles the connection-based operations (ring, provisioning) while the advertiser handles the broadcast-based location reporting.
