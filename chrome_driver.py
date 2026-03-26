#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
import undetected_chromedriver as uc
import os
import shutil
import platform

def _linux_arm64():
    """True when Google Chrome for Testing 'linux64' (x86_64) driver cannot run here."""
    return platform.system() == "Linux" and platform.machine() in ("aarch64", "arm64")

def find_system_chromedriver():
    """Chromedriver from PATH or distro packages (required on Linux aarch64; uc downloads x86_64 only)."""
    candidates = [
        os.environ.get("CHROMEDRIVER_PATH"),
        shutil.which("chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None

def find_chrome():
    """Find Chrome executable using known paths and system commands."""
    possiblePaths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\ProgramData\chocolatey\bin\chrome.exe",
        r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/local/bin/google-chrome",
        "/opt/google/chrome/chrome",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    ]
    # Check predefined paths
    for path in possiblePaths:
        if os.path.exists(path):
            return path
    # Use system command to find Chrome
    try:
        if platform.system() == "Windows":
            chrome_path = shutil.which("chrome")
        else:
            chrome_path = shutil.which("google-chrome") or shutil.which("chromium")
        if chrome_path:
            return chrome_path
    except Exception as e:
        print(f"[ChromeDriver] Error while searching system paths: {e}")
    return None

def get_options():
    chrome_options = uc.ChromeOptions()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    return chrome_options

def _arm64_linux_driver_help():
    return (
        "On Linux ARM64, undetected-chromedriver downloads an x86_64 ChromeDriver that cannot run in this environment.\n"
        "Install a matching driver and ensure it is on PATH, e.g. on Debian/Ubuntu: sudo apt install chromium-driver\n"
        "Or set CHROMEDRIVER_PATH to the chromedriver binary that matches your Chromium version."
    )

def create_driver():
    """Create a Chrome WebDriver with undetected_chromedriver.

    version_main=None lets uc match the installed Chrome major version (avoids
    crashes / "target window already closed" when Chrome auto-updates past a
    pinned driver version).
    """
    chrome_path = find_chrome()
    driver_path = find_system_chromedriver() if _linux_arm64() else None
    if _linux_arm64() and not driver_path:
        raise Exception(
            "[ChromeDriver] No usable chromedriver on this ARM64 system.\n" + _arm64_linux_driver_help()
        )

    def _chrome_kwargs():
        opts = get_options()
        if chrome_path:
            opts.binary_location = chrome_path
        kw = dict(options=opts, version_main=None)
        if driver_path:
            kw["driver_executable_path"] = driver_path
        return kw

    try:
        driver = uc.Chrome(**_chrome_kwargs())
        loc = driver_path or "bundled"
        print(f"[ChromeDriver] Browser started (chromedriver: {loc}).")
        return driver
    except Exception as e:
        print(f"[ChromeDriver] Default ChromeDriver creation failed: {e}")
        print("[ChromeDriver] Trying alternative paths...")
        if chrome_path:
            chrome_options = get_options()
            chrome_options.binary_location = chrome_path
            try:
                kw = dict(options=chrome_options, version_main=None)
                if driver_path:
                    kw["driver_executable_path"] = driver_path
                driver = uc.Chrome(**kw)
                print(f"[ChromeDriver] ChromeDriver started using chrome binary {chrome_path}")
                return driver
            except Exception as e2:
                print(f"[ChromeDriver] ChromeDriver failed using path {chrome_path}: {e2}")
        else:
            print("[ChromeDriver] No Chrome executable found in known paths.")

        # Final fallback - try headless mode (skip on ARM64 without system driver — same failure)
        if not (_linux_arm64() and not driver_path):
            print("[ChromeDriver] Trying headless mode as last resort...")
            try:
                chrome_options = get_options()
                chrome_options.add_argument("--headless")
                if chrome_path:
                    chrome_options.binary_location = chrome_path
                kw = dict(options=chrome_options, version_main=None)
                if driver_path:
                    kw["driver_executable_path"] = driver_path
                driver = uc.Chrome(**kw)
                print("[ChromeDriver] Started in headless mode successfully.")
                return driver
            except Exception as e3:
                print(f"[ChromeDriver] Headless mode also failed: {e3}")

        raise Exception(
            "[ChromeDriver] Failed to start ChromeDriver.\n"
            + (_arm64_linux_driver_help() if _linux_arm64() else (
                "A current version of Chrome was not detected on your system.\n"
                "If you know that Chrome is installed, update Chrome to the latest version. If the script is still not working, "
                "set the path to your Chrome executable manually inside the script."
            ))
        )

if __name__ == '__main__':
    create_driver()
