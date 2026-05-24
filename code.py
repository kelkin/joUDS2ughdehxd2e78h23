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

# URL construction
DATA_SOURCE_URL = (secrets["url_prefix"]) + (secrets["ny511key"]) + (secrets["url_suffix"])

# Configuration settings
LOCAL_VERSION = "1.0.3"
debug = secrets.get("debug", 0)
width = int(secrets.get("width", 64))
height = int(secrets.get("height", 32))
bit_depth = int(secrets.get("depth", 4))
matrix_debug = secrets.get("matrix_debug", False)
characters_per_line = int(secrets.get("characters_per_line", 10))
sign_text_color = secrets.get("sign_text_color", 0xF7B500)  # Road sign yellow

# --- Initialize Hardware Watchdog Timer ---
# If the ESP32 hangs for more than 45 seconds, the hardware will auto-reset the board
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
            # Keep watchdog alive during wait
            for _ in range(10):
                w.feed()
                time.sleep(1)
            continue

    # 2. Fetch data from 511NY API
    print(f"Fetching data from API...")
    response = None
    try:
        response = requests.get(DATA_SOURCE_URL, timeout=15)
        w.feed()

        if response.status_code == 200:
            print("API fetch successful. Processing JSON payload...")
            json_data = response.json()
            w.feed()

            if isinstance(json_data, list):
                # Temporary containers for parsed API signs
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
                    # Look for this favorite sign in our downloaded API data
                    for sign_data in api_signs:
                        if fav_name == sign_data['name']:
                            print(f"\nMATCH FOUND: {fav_name}")
                            
                            # Clean and format the name
                            raw_name = sign_data['name']
                            clean_name = clean_string(raw_name)
                            centered_name = center_multiline_string(clean_name, characters_per_line)
                            
                            # Display Sign Name in Blue
                            print(f"Displaying Name:\n{centered_name}")
                            matrixportal.set_text_color("#0000FF")
                            matrixportal.set_text(centered_name)
                            
                            # Hold name on screen (feeds WDT)
                            for _ in range(3):
                                w.feed()
                                time.sleep(1)

                            # Clean and format the Sign Message
                            raw_msg = sign_data['messages']
                            clean_msg = clean_string(raw_msg)
                            # Convert escaped \\n to real newlines
                            clean_msg = clean_msg.replace('\\n', '\n')
                            centered_msg = center_multiline_string(clean_msg, characters_per_line)
                            
                            # Display Sign Message in road-sign yellow
                            print(f"Displaying Message:\n{centered_msg}")
                            matrixportal.set_text_color(sign_text_color)
                            matrixportal.set_text(centered_msg)
                            
                            # Hold message on screen for 10 seconds (feeds WDT)
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
        # Guarantee network connection closure to avoid socket resource leaks
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        
        # Explicitly delete local references to big data payloads so GC can sweep them
        json_data = None
        api_signs = None
        gc.collect()

    # 4. Safe sleep interval before next complete loop cycle (default 30 seconds)
    print("Cycle completed. Sleeping...")
    for _ in range(30):
        w.feed()
        time.sleep(1)

