# SPDX-FileCopyrightText: 2026 Google LLC
#
# SPDX-License-Identifier: Apache-2.0

"""
Robust CircuitPython Main Program for Matrix Portal S3 Traffic Sign Display.

Fixes & Enhancements:
- MatrixPortal hardware initialization moved OUTSIDE the loop.
- SocketPool and Requests Session created once and reused.
- Fixed off-by-one index bug when matching and displaying signs.
- Implemented aggressive garbage collection to prevent memory exhaustion.
- Integrated hardware Watchdog Timer to automatically reboot if frozen.
- Keeps track of runtime cycles and recovers gracefully from network dropped states.
- Integrated GitHub OTA (Over-The-Air) automatic self-update engine.
- Displays local and cloud versions on the LED matrix during the update check sequence.
"""

import ssl
import wifi
import socketpool
import adafruit_requests
import time
import sys
import terminalio
import gc
from microcontroller import watchdog as w
from watchdog import WatchDogMode
from adafruit_matrixportal.matrixportal import MatrixPortal

# --- Configuration & Secrets Setup ---
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

# URL construction for Traffic Signs
DATA_SOURCE_URL = (secrets["url_prefix"]) + (secrets["ny511key"]) + (secrets["url_suffix"])

# --- OTA Update Configuration (GitHub) ---
ENABLE_OTA = secrets.get("enable_ota", False)
LOCAL_VERSION = "1.0.5"  # Update this when pushing new code to GitHub!
VERSION_URL = secrets.get("github_version_url", "")
CODE_URL = secrets.get("github_code_url", "")

# Configuration settings
debug = secrets.get("debug", 0)
width = int(secrets.get("width", 64))
height = int(secrets.get("height", 32))
bit_depth = int(secrets.get("depth", 4))
matrix_debug = secrets.get("matrix_debug", False)
characters_per_line = int(secrets.get("characters_per_line", 10))
sign_text_color = secrets.get("sign_text_color", 0xF7B500)  # Road sign yellow

# --- Initialize Hardware Watchdog Timer ---
w.timeout = 45.0
w.mode = WatchDogMode.RESET
w.feed()

# --- Initialize Matrix Portal S3 (ONCE at Startup) ---
print("Initializing Matrix Portal display...")
matrixportal = MatrixPortal(
    width=width,
    height=height,
    bit_depth=bit_depth,
    debug=str(matrix_debug)
)

# Create a single label for the sign text
matrixportal.add_text(
    text_font=terminalio.FONT,
    text_position=(0, 15),
    scrolling=False,
    line_spacing=0.8,
    text_color=sign_text_color
)
w.feed()

# --- Helper Functions ---
def center_multiline_string(text, width_chars):
    """Centers each line of a multi-line string to fit the matrix display."""
    lines = text.splitlines()
    centered_lines = [line.center(width_chars) for line in lines]
    return "\n".join(centered_lines)

def clean_string(text):
    """Safely strips out brackets, quotes, and cleans line endings."""
    if text is None:
        return ""
    if isinstance(text, list):
        text = " ".join(str(item) for item in text)
    else:
        text = str(text)
    
    cleaned = text.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
    return cleaned

def connect_wifi():
    """Connects/reconnects to the configured WiFi access point."""
    w.feed()
    if wifi.radio.connected:
        return True

    print(f"Connecting to WiFi {secrets['ssid']}...")
    try:
        wifi.radio.connect(secrets["ssid"], secrets["password"])
        print(f"Connected! IP: {wifi.radio.ipv4_address}")
        w.feed()
        return True
    except Exception as e:
        print(f"WiFi Connection failed: {e}")
        return False

# --- Initial Network Connection ---
gc.collect()
connect_wifi()

# Set up reusable sockets and request sessions to avoid leaking system ports
pool = socketpool.SocketPool(wifi.radio)
ssl_context = ssl.create_default_context()
requests = adafruit_requests.Session(pool, ssl_context)

# --- GitHub self-updating OTA function ---
def perform_ota_check(requests_session):
    if not ENABLE_OTA or not VERSION_URL or not CODE_URL:
        print("OTA Updates are disabled or URLs are not configured in secrets.py.")
        return

    print(f"Checking for updates... Local Version: {LOCAL_VERSION}")
    
    # 1. Display Current Local Version on Boot
    matrixportal.set_text_color("#00FFFF")  # Cyan
    matrixportal.set_text(center_multiline_string(f"LOCAL VER\n{LOCAL_VERSION}", characters_per_line))
    time.sleep(2)
    w.feed()
    
    response = None
    try:
        response = requests_session.get(VERSION_URL, timeout=10)
        if response.status_code == 200:
            remote_version = response.text.strip()
            print(f"Remote version found on GitHub: '{remote_version}'")
            
            # 2. Display Latest Cloud Version
            matrixportal.set_text_color("#FFFF00")  # Yellow
            matrixportal.set_text(center_multiline_string(f"CLOUD VER\n{remote_version}", characters_per_line))
            time.sleep(2)
            w.feed()
            
            if remote_version != LOCAL_VERSION:
                print("New version available! Starting update download...")
                matrixportal.set_text_color("#00FF00")  # Green
                matrixportal.set_text(center_multiline_string("UPDATING\nCODE...", characters_per_line))
                
                # Close the version checker response stream
                response.close()
                w.feed()
                
                # Download new code.py
                response = requests_session.get(CODE_URL, timeout=15)
                if response.status_code == 200:
                    new_code = response.text
                    
                    print("Attempting to overwrite code.py...")
                    try:
                        with open("code.py", "w") as f:
                            f.write(new_code)
                        print("Update complete! Rebooting board...")
                        
                        # 3. Display Successful Update State with the Target Version
                        matrixportal.set_text_color("#00FF00")  # Green
                        matrixportal.set_text(center_multiline_string(f"SUCCESS\nNEW: {remote_version}", characters_per_line))
                        time.sleep(4)
                        
                        import microcontroller
                        microcontroller.reset()
                    except OSError as fs_err:
                        print(f"Filesystem Write Error: {fs_err}")
                        matrixportal.set_text_color("#FF0000")  # Red
                        matrixportal.set_text(center_multiline_string("WRITE\nLOCKED", characters_per_line))
                        time.sleep(5)
                else:
                    print(f"Failed to fetch code.py: HTTP {response.status_code}")
            else:
                print("Your firmware is up to date!")
                matrixportal.set_text_color("#00FF00")  # Green
                matrixportal.set_text(center_multiline_string("VER VERIFIED\nUP TO DATE
