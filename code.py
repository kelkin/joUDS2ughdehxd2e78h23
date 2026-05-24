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
- Integrated Manifest-Based GitHub OTA (Over-The-Air) automatic self-update engine.
- Defensive try/except imports to allow auto-bootstrapping missing libraries.
- Integrated Local Web Configuration Server (runs if libraries are present).
- Displays visual Wi-Fi status and splits/centers the IP address on bootup.
"""

import ssl
import wifi
import socketpool
import adafruit_requests
import time
import sys
import terminalio
import gc
import os
from microcontroller import watchdog as w
from watchdog import WatchDogMode
from adafruit_matrixportal.matrixportal import MatrixPortal

# --- Defensive Imports for Web Server Bootstrap ---
# If the library is missing, we flag it as False instead of crashing,
# allowing the OTA updater to fetch the missing libraries on boot!
try:
    from adafruit_httpserver import HTTPServer, HTTPResponse, HTTPMethod
    HAS_HTTPSERVER = True
    print("Web server libraries loaded successfully.")
except ImportError:
    HAS_HTTPSERVER = False
    print("WARNING: adafruit_httpserver library not found. Running in Bootstrap Mode.")

# --- Configuration & Secrets Setup ---
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

# URL construction for Traffic Signs
DATA_SOURCE_URL = (secrets["url_prefix"]) + (secrets["ny511key"]) + (secrets["url_suffix"])

# --- OTA Update Configuration (GitHub JSON Manifest) ---
ENABLE_OTA = secrets.get("enable_ota", False)
LOCAL_VERSION = "1.1.1"  # MATCHES the version inside ota_manifest.json to prevent bootloop!
# We reuse your existing 'github_version_url' to point to your raw 'ota_manifest.json' file
MANIFEST_URL = secrets.get("github_version_url", "")

# Configuration settings
debug = secrets.get("debug", 0)
width = int(secrets.get("width", 64))
height = int(secrets.get("height", 32))
bit_depth = int(secrets.get("depth", 4))
matrix_debug = secrets.get("matrix_debug", False)
characters_per_line = int(secrets.get("characters_per_line", 10))
sign_text_color = secrets.get("sign_text_color", 0xF7B500)  # Road sign yellow

# Global triggers for on-demand web portal requests
force_ota_triggered = False

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
        # Show cyan connecting message on display
        matrixportal.set_text_color("#00FFFF")
        matrixportal.set_text(center_multiline_string("CONNECTING\nWIFI...", characters_per_line))
    except Exception:
        pass

    try:
        wifi.radio.connect(secrets["ssid"], secrets["password"])
        print(f"Connected! IP: {wifi.radio.ipv4_address}")
        w.feed()
        return True
    except Exception as e:
        print(f"WiFi Connection failed: {e}")
        try:
            # Show red failure message
            matrixportal.set_text_color("#FF0000")
            matrixportal.set_text(center_multiline_string("WIFI\nFAILED", characters_per_line))
        except Exception:
            pass
        return False

def ensure_dir_exists(filepath):
    """Recursively creates directory structures on the local storage if they do not exist."""
    parts = filepath.split("/")
    if len(parts) > 1:
        current_path = ""
        for part in parts[:-1]:
            if current_path:
                current_path += "/" + part
            else:
                current_path = part
            try:
                os.mkdir(current_path)
                print(f"Created directory: {current_path}")
            except OSError as e:
                # Error 17 means the folder already exists, which is fine!
                if e.errno != 17:
                    raise e

# --- Initial Network Connection ---
gc.collect()
if connect_wifi():
    # Format and show the IP Address nicely split across two lines on the LED panel
    try:
        ip_str = str(wifi.radio.ipv4_address)
        octets = ip_str.split('.')
        if len(octets) == 4:
            # Split "192.168.100.47" into "192.168." and "100.47" to fit characters_per_line nicely
            ip_display = f"{octets[0]}.{octets[1]}.\n{octets[2]}.{octets[3]}"
        else:
            ip_display = ip_str
        
        # Display IP on matrix in green for 5 seconds
        matrixportal.set_text_color("#00FF00")
        matrixportal.set_text(center_multiline_string(ip_display, characters_per_line))
        for _ in range(5):
            w.feed()
            time.sleep(1)
    except Exception as display_err:
        print(f"Error displaying IP: {display_err}")

# Set up reusable sockets and request sessions
pool = socketpool.SocketPool(wifi.radio)
ssl_context = ssl.create_default_context()
requests = adafruit_requests.Session(pool, ssl_context)

# --- GitHub Manifest-Based Multi-File OTA function ---
def perform_ota_check(requests_session, force=False):
    if not ENABLE_OTA or not MANIFEST_URL:
        print("OTA Updates are disabled or Manifest URL is not configured.")
        return

    print(f"Checking updates via Manifest... Local Version: {LOCAL_VERSION}")
    matrixportal.set_text_color("#FFFF00")  # Yellow
    matrixportal.set_text(center_multiline_string("CHECKING\nUPDATE", characters_per_line))
    
    response = None
    try:
        w.feed()
        response = requests_session.get(MANIFEST_URL, timeout=15)
        if response.status_code == 200:
            manifest_data = response.json()
            w.feed()
            
            remote_version = manifest_data.get("version", "0.0.0")
            files_to_download = manifest_data.get("files", {})
            
            print(f"Remote version found on GitHub: '{remote_version}'")
            
            if remote_version != LOCAL_VERSION or force:
                print("Update triggered! Starting multi-file download bootstrap...")
                matrixportal.set_text_color("#00FF00")  # Green
                matrixportal.set_text(center_multiline_string("DOWNLOADING\nFILES...", characters_per_line))
                
                # Close manifest connection to save resources
                response.close()
                w.feed()
                
                total_files = len(files_to_download)
                file_idx = 0
                
                # Iterate through all files specified in the JSON manifest
                for local_path, remote_url in files_to_download.items():
                    file_idx += 1
                    w.feed()  # Keep feeding the watchdog before starting every download
                    print(f"[{file_idx}/{total_files}] Fetching {local_path} from {remote_url}")
                    
                    file_response = None
                    try:
                        file_response = requests_session.get(remote_url, timeout=20)
                        if file_response.status_code == 200:
                            file_content = file_response.text
                            w.feed()
                            
                            # Auto-create parent folders (like 'lib/adafruit_httpserver')
                            ensure_dir_exists(local_path)
                            
                            # Save file directly onto filesystem
                            with open(local_path, "w") as f:
                                f.write(file_content)
                            w.feed()
                            print(f"Successfully saved: {local_path}")
                        else:
                            print(f"Failed to fetch {local_path}: HTTP {file_response.status_code}")
                    except Exception as download_error:
                        print(f"Error downloading {local_path}: {download_error}")
                    finally:
                        if file_response is not None:
                            try:
                                file_response.close()
                            except Exception:
                                pass
                        gc.collect()
                
                print("All files processed successfully! Rebooting board...")
                matrixportal.set_text_color("#00FF00")
                matrixportal.set_text(center_multiline_string("SUCCESS\nREBOOTING", characters_per_line))
                time.sleep(3)
                import microcontroller
                microcontroller.reset()
            else:
                print("Your firmware is completely up to date!")
        else:
            print(f"Failed to fetch remote manifest: HTTP {response.status_code}")
    except Exception as ex:
        print(f"Error during Manifest OTA check: {ex}")
        import traceback
        traceback.print_exception(ex)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        gc.collect()

# Run the update check at bootup
perform_ota_check(requests)

# --- Initialize Web Server only if library is present ---
server = None
if HAS_HTTPSERVER:
    try:
        server = HTTPServer(pool)
        
        html_template = """<!DOCTYPE html>
        <html>
        <head>
            <title>Matrix Portal Dashboard</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: Arial, sans-serif; text-align: center; background-color: #222; color: #fff; margin: 40px; }}
                h1 {{ color: #F7B500; }}
                .card {{ background: #333; padding: 20px; border-radius: 10px; max-width: 400px; margin: 0 auto; box-shadow: 0 4px 8px rgba(0,0,0,0.3); }}
                .btn {{ display: block; width: 100%; padding: 12px; margin: 15px 0; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; text-decoration: none; font-size: 16px; }}
                .btn-update {{ background-color: #00FF00; color: #000; }}
                .btn-update:hover {{ background-color: #00CC00; }}
                .info {{ color: #aaa; margin-bottom: 20px; font-size: 14px; }}
            </style>
        </head>
        <body>
            <h1>Matrix Portal S3</h1>
            <div class="card">
                <p><strong>Device Status:</strong> Active</p>
                <p><strong>Local Firmware Version:</strong> v{version}</p>
                <p class="info">IP Address: {ip_addr}</p>
                <hr style="border: 0.5px solid #444;">
                <form method="POST" action="/force-update">
                    <button type="submit" class="btn btn-update">Force GitHub OTA Update</button>
                </form>
            </div>
        </body>
        </html>
        """

        @server.route("/", HTTPMethod.GET)
        def base(request):
            response_body = html_template.format(version=LOCAL_VERSION, ip_addr=wifi.radio.ipv4_address)
            return HTTPResponse(request, content_type="text/html", body=response_body)

        @server.route("/force-update", HTTPMethod.POST)
        def force_update_handler(request):
            global force_ota_triggered
            force_ota_triggered = True
            return HTTPResponse(request, content_type="text/html", body="<h1>Trigger Sent! Checking GitHub... Check your LED Matrix display!</h1>")

        print("Starting local configuration server...")
        server.start(str(wifi.radio.ipv4_address))
    except Exception as server_init_err:
        print(f"Failed to start local server: {server_init_err}")
        HAS_HTTPSERVER = False

w.feed()

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

    gc.collect()

    # 1. Verify connection status
    if not wifi.radio.connected:
        print("WiFi disconnected. Reconnecting...")
        if not connect_wifi():
            print("Could not reconnect. Waiting 10 seconds...")
            for _ in range(10):
                w.feed()
                if HAS_HTTPSERVER and server is not None:
                    try: server.poll() 
                    except Exception: pass
                time.sleep(1)
            continue

    # 2. Check if a web client pressed the force update button
    if force_ota_triggered:
        force_ota_triggered = False
        perform_ota_check(requests, force=True)

    # 3. Fetch data from 511NY API
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
                
                # 4. Match signs and display them
                print("Checking for favorited sign matches...")
                for fav_name in favsign_list:
                    w.feed()
                    
                    if HAS_HTTPSERVER and server is not None:
                        try: server.poll()
                        except Exception: pass
                    
                    for sign_data in api_signs:
                        if fav_name == sign_data['name']:
                            print(f"\nMATCH FOUND: {fav_name}")
                            
                            raw_name = sign_data['name']
                            clean_name = clean_string(raw_name)
                            centered_name = center_multiline_string(clean_name, characters_per_line)
                            
                            print(f"Displaying Name:\n{centered_name}")
                            matrixportal.set_text_color("#0000FF")
                            matrixportal.set_text(centered_name)
                            
                            for _ in range(3):
                                w.feed()
                                if HAS_HTTPSERVER and server is not None:
                                    try: server.poll()
                                    except Exception: pass
                                time.sleep(1)

                            raw_msg = sign_data['messages']
                            clean_msg = clean_string(raw_msg)
                            clean_msg = clean_msg.replace('\\n', '\n')
                            centered_msg = center_multiline_string(clean_msg, characters_per_line)
                            
                            print(f"Displaying Message:\n{centered_msg}")
                            matrixportal.set_text_color(sign_text_color)
                            matrixportal.set_text(centered_msg)
                            
                            for _ in range(10):
                                w.feed()
                                if HAS_HTTPSERVER and server is not None:
                                    try: server.poll()
                                    except Exception: pass
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
            try: response.close()
            except Exception: pass
        
        json_data = None
        api_signs = None
        gc.collect()

    if force_ota_triggered:
        force_ota_triggered = False
        perform_ota_check(requests, force=True)

    # 5. Safe sleep interval polling web server
    print("Cycle completed. Sleeping...")
    for _ in range(30):
        w.feed()
        if HAS_HTTPSERVER and server is not None:
            try:
                server.poll()
            except Exception:
                pass
        time.sleep(1)
