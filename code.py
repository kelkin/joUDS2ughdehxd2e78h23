# SPDX-FileCopyrightText: 2026 Joe Engineer (original structure via Google LLC template)
#
# SPDX-License-Identifier: Apache-2.0

"""
Robust CircuitPython Main Program for Matrix Portal S3 Traffic Sign Display.

Features:
- Fetches live traffic sign data from the NY511 API and displays matched signs.
- MatrixPortal hardware initialized once at startup (not inside the loop).
- SocketPool and Requests Session created once and reused across cycles.
- Aggressive garbage collection to prevent heap fragmentation/memory exhaustion.
- Hardware Watchdog Timer (45s) automatically reboots the board if frozen.
- Graceful WiFi reconnection with cycle tracking.
- GitHub OTA (Over-The-Air) update engine driven by a JSON manifest file.
  The manifest contains the target version string and a map of all files to
  download, enabling full multi-file updates (code + libraries) in one pass.
- Local and cloud version numbers displayed on the LED matrix at boot.

Bugfixes applied:
- set_text_color() calls use integer literals (0xRRGGBB), not CSS strings.
- set_text_color() and set_text() pass label index 0 explicitly.
- matrix_debug cast to bool before passing to MatrixPortal().
- OTA version comparison now parses manifest JSON properly instead of
  comparing the entire raw JSON blob to the local version string.
- os.makedirs() replaced with per-level os.mkdir() (CircuitPython compatible).
- All imports moved to module top level — no deferred imports inside functions.
"""

import ssl
import wifi
import socketpool
import adafruit_requests
import time
import sys
import os
import json
import traceback
import terminalio
import gc
import microcontroller
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
LOCAL_VERSION = "1.1.2"  # Update this when pushing new code to GitHub!
# VERSION_URL must point to a JSON manifest (see perform_ota_check for format).
# File download URLs are read from the manifest itself — no separate CODE_URL needed.
VERSION_URL = secrets.get("github_version_url", "")

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
# Expects VERSION_URL to point to a JSON manifest with this structure:
# {
#   "version": "1.1.2",
#   "files": {
#     "code.py": "https://raw.githubusercontent.com/.../code.py",
#     "lib/adafruit_logging.py": "https://raw.githubusercontent.com/...",
#     ...
#   }
# }
def perform_ota_check(requests_session):
    if not ENABLE_OTA or not VERSION_URL:
        print("OTA Updates are disabled or VERSION_URL is not configured in secrets.py.")
        return

    print(f"Checking for updates... Local Version: {LOCAL_VERSION}")

    # 1. Display Current Local Version on Boot
    matrixportal.set_text_color(0x00FFFF, 0)  # Cyan
    matrixportal.set_text(center_multiline_string(f"LOCAL VER\n{LOCAL_VERSION}", characters_per_line), 0)
    time.sleep(2)
    w.feed()

    response = None
    try:
        response = requests_session.get(VERSION_URL, timeout=10)
        if response.status_code != 200:
            print(f"Failed to fetch manifest: HTTP {response.status_code}")
            return

        # Parse the JSON manifest — read text first, close stream, then parse
        # to free the socket before allocating the parsed object
        raw_text = response.text
        response.close()
        response = None
        w.feed()
        gc.collect()

        try:
            manifest = json.loads(raw_text)
        except Exception as parse_err:
            print(f"Failed to parse version manifest as JSON: {parse_err}")
            print(f"Raw content received: {raw_text[:200]}")
            return
        finally:
            raw_text = None
            gc.collect()

        remote_version = manifest.get("version", "").strip()
        file_map = manifest.get("files", {})

        if not remote_version:
            print("Manifest is missing 'version' field. Aborting OTA.")
            return

        print(f"Remote version from manifest: '{remote_version}'")

        # 2. Display Latest Cloud Version (just the version number, not the whole JSON)
        matrixportal.set_text_color(0xFFFF00, 0)  # Yellow
        matrixportal.set_text(center_multiline_string(f"CLOUD VER\n{remote_version}", characters_per_line), 0)
        time.sleep(2)
        w.feed()

        if remote_version == LOCAL_VERSION:
            print("Firmware is up to date!")
            matrixportal.set_text_color(0x00FF00, 0)  # Green
            matrixportal.set_text(center_multiline_string("VER VERIFIED\nUP TO DATE", characters_per_line), 0)
            time.sleep(1.5)
            return

        # 3. New version available — download all files listed in the manifest
        print(f"New version available: {remote_version}. Downloading {len(file_map)} file(s)...")
        matrixportal.set_text_color(0x00FF00, 0)  # Green
        matrixportal.set_text(center_multiline_string("UPDATING\nCODE...", characters_per_line), 0)
        w.feed()

        failed_files = []
        total = len(file_map)
        current = 0

        for dest_path, file_url in file_map.items():
            current += 1
            w.feed()
            print(f"  [{current}/{total}] Downloading: {dest_path}")

            # CircuitPython only has os.mkdir() — no os.makedirs().
            # For nested paths like lib/adafruit_httpserver/ we must create
            # each directory level individually, ignoring errors if it exists.
            parts = dest_path.split("/")
            if len(parts) > 1:
                path_so_far = ""
                for part in parts[:-1]:  # Walk every directory segment, skip filename
                    path_so_far = part if path_so_far == "" else path_so_far + "/" + part
                    try:
                        os.mkdir(path_so_far)
                        print(f"    Created dir: {path_so_far}")
                    except OSError:
                        pass  # Already exists — that's fine

            file_response = None
            try:
                file_response = requests_session.get(file_url, timeout=20)
                w.feed()
                if file_response.status_code == 200:
                    with open(dest_path, "w") as f:
                        f.write(file_response.text)
                    print(f"    Saved: {dest_path}")
                else:
                    print(f"    HTTP {file_response.status_code} for {dest_path} — skipping.")
                    failed_files.append(dest_path)
            except OSError as fs_err:
                print(f"    Write error for {dest_path}: {fs_err}")
                print("    Hint: Check boot.py — filesystem may not be remounted for board writes.")
                failed_files.append(dest_path)
                if dest_path == "code.py":
                    # If we can't write code.py at all the filesystem is locked — abort early
                    matrixportal.set_text_color(0xFF0000, 0)  # Red
                    matrixportal.set_text(center_multiline_string("WRITE\nLOCKED", characters_per_line), 0)
                    time.sleep(5)
                    return
            except Exception as dl_err:
                print(f"    Download error for {dest_path}: {dl_err}")
                failed_files.append(dest_path)
            finally:
                if file_response is not None:
                    try:
                        file_response.close()
                    except Exception:
                        pass
                gc.collect()

        # 4. Report result and reboot
        if failed_files:
            print(f"Update completed with {len(failed_files)} failure(s): {failed_files}")
            matrixportal.set_text_color(0xFF8800, 0)  # Orange — partial success
            matrixportal.set_text(center_multiline_string(f"PARTIAL\n{len(failed_files)} FAIL", characters_per_line), 0)
        else:
            print(f"All {total} file(s) updated successfully!")
            matrixportal.set_text_color(0x00FF00, 0)  # Green
            matrixportal.set_text(center_multiline_string(f"SUCCESS\nNEW:{remote_version}", characters_per_line), 0)

        time.sleep(4)
        print("Rebooting...")
        microcontroller.reset()

    except Exception as ex:
        print(f"Error during OTA check: {ex}")
        traceback.print_exception(ex)
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

