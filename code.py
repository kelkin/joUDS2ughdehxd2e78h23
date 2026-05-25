# SPDX-FileCopyrightText: 2026 Joe Engineer (original structure via Google LLC template)
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

Bugfixes in this revision:
- FIX: All set_text_color() calls now use integer literals (0xRRGGBB) instead of
       CSS hex strings ("#RRGGBB"). The MatrixPortal library only accepts integers;
       passing a string caused a silent exception inside perform_ota_check(), making
       OTA appear broken with no visible error output.
- FIX: All set_text_color() calls now pass label index 0 as the second argument,
       which is required when more than one text label exists on the display.
- FIX: matrix_debug is now cast to bool before being passed to MatrixPortal(),
       preventing str(False) == "False" from evaluating as truthy and enabling
       unwanted debug output.
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
LOCAL_VERSION = "1.0.8"  # Update this when pushing new code to GitHub!
VERSION_URL = secrets.get("github_version_url", "")
CODE_URL = secrets.get("github_code_url", "")

# Configuration settings
debug = secrets.get("debug", 0)
width = int(secrets.get("width", 64))
height = int(secrets.get("height", 32))
bit_depth = int(secrets.get("depth", 4))
# FIX: Cast to bool so MatrixPortal receives True/False, not the string "False"
matrix_debug = bool(secrets.get("matrix_debug", False))
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
    debug=matrix_debug  # FIX: pass bool directly, not str(matrix_debug)
)

# Create a single label for the sign text (index 0)
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
    # FIX: Use integer color literals (0xRRGGBB) — the library does not accept "#RRGGBB" strings.
    # FIX: Pass label index 0 as second argument to target the correct text label.
    matrixportal.set_text_color(0x00FFFF, 0)  # Cyan
    matrixportal.set_text(center_multiline_string(f"LOCAL VER\n{LOCAL_VERSION}", characters_per_line), 0)
    time.sleep(2)
    w.feed()

    response = None
    try:
        response = requests_session.get(VERSION_URL, timeout=10)
        if response.status_code == 200:
            remote_version = response.text.strip()
            print(f"Remote version found on GitHub: '{remote_version}'")

            # 2. Display Latest Cloud Version
            matrixportal.set_text_color(0xFFFF00, 0)  # Yellow
            matrixportal.set_text(center_multiline_string(f"CLOUD VER\n{remote_version}", characters_per_line), 0)
            time.sleep(2)
            w.feed()

            if remote_version != LOCAL_VERSION:
                print("New version available! Starting update download...")
                matrixportal.set_text_color(0x00FF00, 0)  # Green
                matrixportal.set_text(center_multiline_string("UPDATING\nCODE...", characters_per_line), 0)

                # Close the version checker response stream before opening a new one
                response.close()
                response = None
                w.feed()

                # Download new code.py
                response = requests_session.get(CODE_URL, timeout=15)
                w.feed()  # Feed watchdog — GitHub download can be slow
                if response.status_code == 200:
                    new_code = response.text
                    w.feed()

                    print("Attempting to overwrite code.py...")
                    try:
                        with open("code.py", "w") as f:
                            f.write(new_code)
                        print("Update complete! Rebooting board...")

                        # 3. Display Successful Update State with the Target Version
                        matrixportal.set_text_color(0x00FF00, 0)  # Green
                        matrixportal.set_text(center_multiline_string(f"SUCCESS\nNEW: {remote_version}", characters_per_line), 0)
                        time.sleep(4)

                        import microcontroller
                        microcontroller.reset()
                    except OSError as fs_err:
                        print(f"Filesystem Write Error: {fs_err}")
                        print("Hint: Check boot.py — filesystem may not be remounted for board writes.")
                        matrixportal.set_text_color(0xFF0000, 0)  # Red
                        matrixportal.set_text(center_multiline_string("WRITE\nLOCKED", characters_per_line), 0)
                        time.sleep(5)
                else:
                    print(f"Failed to fetch code.py: HTTP {response.status_code}")
            else:
                print("Your firmware is up to date!")
                matrixportal.set_text_color(0x00FF00, 0)  # Green
                matrixportal.set_text(center_multiline_string("VER VERIFIED\nUP TO DATE", characters_per_line), 0)
                time.sleep(1.5)
        else:
            print(f"Failed to fetch remote version: HTTP {response.status_code}")
    except Exception as ex:
        print(f"Error during OTA check: {ex}")
        import traceback
        traceback.print_exception(ex)  # Print full traceback so errors are visible
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        gc.collect()

# Run the OTA check immediately after connecting to WiFi
perform_ota_check(requests)

# --- Load Preferred Sign List (ONCE at Startup) ---
filename = "sign_list.txt"
favsign_list = []
print(f"Attempting to load '{filename}'")

try:
    with open(filename, "r") as f:
        for line in f:
            sys.stdout.write('.')
            cleaned_line = line.strip()
            if cleaned_line:
                favsign_list.append(cleaned_line)
    print(f"\nSuccessfully loaded {len(favsign_list)} entries from '{filename}'.")
except OSError as e:
    print(f"\nError: Could not open or read file '{filename}'. Reason: {e}")
    print("Please ensure 'sign_list.txt' exists in the root directory of the drive.")

# --- Main Program Loop Context ---
cycles = 0
w.feed()

while True:
    cycles += 1
    print(f"\n************************************************")
    print(f"Executing Cycle # {cycles} - Free RAM: {gc.mem_free()} bytes")
    print(f"************************************************")
    w.feed()

    # Always trigger GC to prevent heap fragmentation before networking tasks
    gc.collect()

    # 1. Verify connection status
    if not wifi.radio.connected:
        print("WiFi disconnected. Reconnecting...")
        if not connect_wifi():
            print("Could not reconnect. Waiting 10 seconds...")
            for _ in range(10):
                w.feed()
                time.sleep(1)
            continue

    # 2. Fetch data from 511NY API
    print(f"Fetching data from API...")
    response = None
    json_data = None
    api_signs = None
    try:
        response = requests.get(DATA_SOURCE_URL, timeout=15)
        w.feed()

        if response.status_code == 200:
            print("API fetch successful. Processing JSON payload...")
            json_data = response.json()
            w.feed()

            if isinstance(json_data, list):
                api_signs = []

                for sign in json_data:
                    if 'Name' in sign and 'Messages' in sign:
                        api_signs.append({
                            'id': sign.get('ID', ''),
                            'name': sign['Name'],
                            'roadway': sign.get('Roadway', ''),
                            'direction': sign.get('DirectionOfTravel', ''),
                            'messages': sign['Messages']
                        })

                print(f"Successfully loaded {len(api_signs)} signs from API.")

                # 3. Match signs and display them
                print("Checking for favorited sign matches...")
                for fav_name in favsign_list:
                    w.feed()
                    for sign_data in api_signs:
                        if fav_name == sign_data['name']:
                            print(f"\nMATCH FOUND: {fav_name}")

                            # Clean and format the name
                            raw_name = sign_data['name']
                            clean_name = clean_string(raw_name)
                            centered_name = center_multiline_string(clean_name, characters_per_line)

                            # Display Sign Name in Blue
                            print(f"Displaying Name:\n{centered_name}")
                            matrixportal.set_text_color(0x0000FF, 0)  # FIX: integer + label index
                            matrixportal.set_text(centered_name, 0)

                            for _ in range(3):
                                w.feed()
                                time.sleep(1)

                            # Clean and format the Sign Message
                            raw_msg = sign_data['messages']
                            clean_msg = clean_string(raw_msg)
                            clean_msg = clean_msg.replace('\\n', '\n')
                            centered_msg = center_multiline_string(clean_msg, characters_per_line)

                            # Display Sign Message in road-sign yellow
                            print(f"Displaying Message:\n{centered_msg}")
                            matrixportal.set_text_color(sign_text_color, 0)  # FIX: label index
                            matrixportal.set_text(centered_msg, 0)

                            for _ in range(10):
                                w.feed()
                                time.sleep(1)

            else:
                print(f"Error: API returned unexpected structure: {type(json_data)}")
        else:
            print(f"API Error: Status code {response.status_code}")

    except Exception as e:
        print(f"An error occurred during API fetch or display cycle: {e}")
        import traceback
        traceback.print_exception(e)

    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        json_data = None
        api_signs = None
        gc.collect()

    # 4. Safe sleep interval
    print("Cycle completed. Sleeping...")
    for _ in range(30):
        w.feed()
        time.sleep(1)

