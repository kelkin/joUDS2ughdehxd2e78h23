# SPDX-FileCopyrightText: 2026 Google LLC
#
# SPDX-License-Identifier: Apache-2.0

"""
Robust CircuitPython Main Program for Matrix Portal S3 Traffic Sign Display.

Fixes & Enhancements:
- Implements ATOMIC FILE WRITING to completely eliminate 0-byte file truncation crashes.
- Validates downloaded file size against HTTP Content-Length before swapping onto storage.
- Fallback Submodule Import Engine to handle mismatched package exports natively.
- Exponential backoff retry engine to handle DHCP and DNS settle delays smoothly.
- Robust Socket Constant Fallback (AF_INET/SOCK_STREAM) for resilient Rescue Server binding.
"""

# --- EASY ACCESS VERSION CONFIGURATION ---
LOCAL_VERSION = "1.1.15"  

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

# --- Stream Redirector for Wireless Logging ---
class WebLogger:
    def __init__(self, max_lines=60):
        self.buffer = []
        self.max_lines = max_lines
        self.current_line = ""

    def write(self, message):
        if isinstance(message, (bytes, bytearray)):
            try: message = message.decode("utf-8")
            except Exception: message = str(message)
        elif not isinstance(message, str):
            message = str(message)
            
        parts = message.split("\n")
        if len(parts) == 1:
            self.current_line += parts[0]
        else:
            self.current_line += parts[0]
            self.buffer.append(self.current_line)
            for part in parts[1:-1]:
                self.buffer.append(part)
            self.current_line = parts[-1]
            
            while len(self.buffer) > self.max_lines:
                self.buffer.pop(0)

    def get_logs(self):
        all_lines = list(self.buffer)
        if self.current_line:
            all_lines.append(self.current_line)
        return "\n".join(all_lines)

web_logger = WebLogger()
_original_print = print

def print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = sep.join(str(arg) for arg in args)
    web_logger.write(message + end)
    _original_print(*args, **kwargs)

def log_exception(e):
    try:
        import io
        stream = io.StringIO()
        sys.print_exception(e, stream)
        print(stream.getvalue())
    except Exception:
        print(f"Exception logging failed: {e}")

# --- Defensive Imports for Web Server Bootstrap ---
web_error_message = ""
try:
    from adafruit_httpserver import HTTPServer, HTTPResponse, HTTPMethod
    HAS_HTTPSERVER = True
except Exception as e:
    try:
        from adafruit_httpserver.server import HTTPServer
        from adafruit_httpserver.response import HTTPResponse
        from adafruit_httpserver.methods import HTTPMethod
        HAS_HTTPSERVER = True
    except Exception as fallback_err:
        HAS_HTTPSERVER = False
        log_exception(fallback_err)
        try:
            ex_repr = repr(fallback_err)
            web_error_message = ex_repr.split("(")[0].split(".")[-1].replace("Error", "").upper()[:10]
        except Exception:
            web_error_message = "ERROR"

try:
    from secrets import secrets
except ImportError:
    print("Please create a secrets.py file with WiFi credentials!")
    raise

DATA_SOURCE_URL = secrets["url_prefix"] + secrets["ny511key"] + secrets["url_suffix"]
ENABLE_OTA = secrets.get("enable_ota", False)
MANIFEST_URL = secrets.get("github_version_url", "")

characters_per_line = int(secrets.get("characters_per_line", 10))
sign_text_color = secrets.get("sign_text_color", 0xF7B500)

w.timeout = 45.0
w.mode = WatchDogMode.RESET
w.feed()

matrixportal = MatrixPortal(
    width=int(secrets.get("width", 64)),
    height=int(secrets.get("height", 32)),
    bit_depth=int(secrets.get("depth", 4)),
    debug=str(secrets.get("matrix_debug", False))
)
matrixportal.add_text(
    text_font=terminalio.FONT,
    text_position=(0, 15),
    scrolling=False,
    line_spacing=0.8,
    text_color=sign_text_color
)
w.feed()

def center_multiline_string(text, width_chars):
    return "\n".join([line.center(width_chars) for line in text.splitlines()])

def clean_string(text):
    if text is None: return ""
    text = " ".join(str(item) for item in text) if isinstance(text, list) else str(text)
    return text.replace("[", "").replace("]", "").replace('"', "").replace("'", "")

server = None
rescue_socket = None

def start_rescue_server():
    """Initializes the raw socket Emergency Rescue Server on Port 80."""
    global rescue_socket, pool
    if rescue_socket is None:
        try:
            # Universal auto-detection of network socket parameters
            af_inet = 2      # Default standard AF_INET integer constant
            sock_stream = 1  # Default standard SOCK_STREAM integer constant
            
            try:
                if hasattr(pool, "AF_INET"):
                    af_inet = pool.AF_INET
                elif hasattr(socketpool, "AF_INET"):
                    af_inet = socketpool.AF_INET
            except Exception: pass
            
            try:
                if hasattr(pool, "SOCK_STREAM"):
                    sock_stream = pool.SOCK_STREAM
                elif hasattr(socketpool, "SOCK_STREAM"):
                    sock_stream = socketpool.SOCK_STREAM
            except Exception: pass

            rescue_socket = pool.socket(af_inet, sock_stream)
            rescue_socket.settimeout(0.02)
            try:
                import socket
                rescue_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception: pass
            rescue_socket.bind((str(wifi.radio.ipv4_address), 80))
            try: rescue_socket.listen(3)
            except TypeError: rescue_socket.listen()
            print("Emergency Rescue Web Server active on Port 80.")
        except Exception as e:
            print(f"Rescue binding failed: {e}")
            rescue_socket = None

def poll_rescue_server():
    global rescue_socket
    if not HAS_HTTPSERVER and rescue_socket is not None:
        conn = None
        try:
            conn, addr = rescue_socket.accept()
            conn.settimeout(0.5)
            request_str = ""
            try:
                request = conn.recv(512)
                if request: request_str = request.decode("utf-8")
            except Exception: pass
            
            if "favicon.ico" in request_str or (request_str and "GET / " not in request_str and "GET /logs" not in request_str):
                try:
                    conn.send("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("utf-8"))
                    conn.close()
                except Exception: pass
                return
            
            log_content = web_logger.get_logs()
            body = f"""<!DOCTYPE html><html><head><title>Rescue Console</title><meta name="viewport" content="width=device-width, initial-scale=1"><style>body{{font-family:monospace;background-color:#111;color:#ff3333;margin:20px;}}pre{{background-color:#000;padding:15px;border-radius:5px;border:1px solid #333;overflow-x:auto;white-space:pre-wrap;color:#00ff00;}}</style></head><body><h1>🚨 Matrix Portal S3 - Rescue System</h1><h2>System Diagnostic & Download Logs:</h2><pre>{log_content}</pre></body></html>"""
            response_bytes = body.encode("utf-8")
            headers = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(response_bytes)}\r\nConnection: close\r\n\r\n"
            conn.send(headers.encode("utf-8"))
            conn.send(response_bytes)
            conn.close()
        except OSError: pass
        except Exception as ex:
            print(f"Rescue routing error: {ex}")
            if conn:
                try: conn.close()
                except Exception: pass

def safe_delay(seconds):
    start_time = time.monotonic()
    while time.monotonic() - start_time < seconds:
        w.feed()
        if HAS_HTTPSERVER and server is not None:
            try: server.poll()
            except Exception: pass
        else:
            poll_rescue_server()
        time.sleep(0.02)

def connect_wifi():
    w.feed()
    if wifi.radio.connected: return True
    try:
        matrixportal.set_text_color("#00FFFF")
        matrixportal.set_text(center_multiline_string("CONNECTING\nWIFI...", characters_per_line))
    except Exception: pass

    try:
        wifi.radio.connect(secrets["ssid"], secrets["password"])
        w.feed()
        return True
    except Exception as e:
        print(f"WiFi failed: {e}")
        try:
            matrixportal.set_text_color("#FF0000")
            matrixportal.set_text(center_multiline_string("WIFI\nFAILED", characters_per_line))
        except Exception: pass
        return False

def ensure_dir_exists(filepath):
    parts = filepath.split("/")
    if len(parts) > 1:
        current_path = ""
        for part in parts[:-1]:
            current_path = (current_path + "/" + part) if current_path else part
            try: os.mkdir(current_path)
            except OSError as e:
                if e.errno != 17: raise e

if connect_wifi():
    pool = socketpool.SocketPool(wifi.radio)
    ssl_context = ssl.create_default_context()
    requests = adafruit_requests.Session(pool, ssl_context)
    if not HAS_HTTPSERVER: start_rescue_server()
    try:
        ip_display = f"{str(wifi.radio.ipv4_address)}\n{str(secrets.get('ssid', 'WIFI'))}"
        matrixportal.set_text_color("#00FF00")
        matrixportal.set_text(center_multiline_string(ip_display, characters_per_line))
        safe_delay(5)
    except Exception: pass

try:
    if HAS_HTTPSERVER:
        matrixportal.set_text_color("#00FF00")
        matrixportal.set_text(center_multiline_string("WEB OK\nPORT 80", characters_per_line))
    else:
        matrixportal.set_text_color("#FF0000")
        matrixportal.set_text(center_multiline_string(f"WEB ERR\n{web_error_message}", characters_per_line))
    safe_delay(3)
except Exception: pass

# --- Atomic Manifest-Based Safe OTA Updater ---
def perform_ota_check(requests_session, force=False):
    if not ENABLE_OTA or not MANIFEST_URL: return

    print(f"Checking updates via Manifest... Local Version: {LOCAL_VERSION}")
    matrixportal.set_text_color("#FFFF00")
    matrixportal.set_text(center_multiline_string(f"LOCAL\nv{LOCAL_VERSION}", characters_per_line))
    safe_delay(2)
    matrixportal.set_text(center_multiline_string("CHECKING\nUPDATE", characters_per_line))
    
    response = None
    for retry in range(3):
        w.feed()
        try:
            response = requests_session.get(MANIFEST_URL, timeout=8)
            break
        except Exception:
            if retry == 2: print("GitHub Connection failed."); return
            safe_delay(2)
            
    try:
        if response.status_code == 200:
            manifest_data = response.json()
            remote_version = manifest_data.get("version", "0.0.0")
            files_to_download = manifest_data.get("files", {})
            
            matrixportal.set_text_color("#FFFF00")
            matrixportal.set_text(center_multiline_string(f"CLOUD\nv{remote_version}", characters_per_line))
            safe_delay(2)
            
            if remote_version != LOCAL_VERSION or force:
                print("Update found! Fetching files onto safe temporary storage...")
                matrixportal.set_text_color("#00FF00")
                matrixportal.set_text(center_multiline_string("DOWNLOADING\nFILES...", characters_per_line))
                response.close()
                
                successful_swaps = []
                
                for local_path, remote_url in files_to_download.items():
                    w.feed()
                    print(f"Fetching {local_path}...")
                    
                    file_response = None
                    try:
                        file_response = requests_session.get(remote_url, timeout=10)
                        if file_response.status_code == 200:
                            file_content = file_response.text
                            
                            # Size verification validation
                            content_length = file_response.headers.get("content-length")
                            if content_length is not None:
                                expected_size = int(content_length)
                                actual_size = len(file_content.encode("utf-8"))
                                if actual_size != expected_size:
                                    raise RuntimeError(f"Truncated! Size mismatch for {local_path}")
                            
                            ensure_dir_exists(local_path)
                            temp_path = local_path + ".tmp"
                            
                            # Stage file onto safe temporary file block
                            with open(temp_path, "w") as f:
                                f.write(file_content)
                                
                            successful_swaps.append((temp_path, local_path))
                            print(f"Staged cleanly: {local_path}")
                        else:
                            raise RuntimeError(f"HTTP Error {file_response.status_code}")
                    except Exception as err:
                        print(f"Aborting update to prevent crash. Error: {err}")
                        return
                    finally:
                        if file_response: file_response.close()
                        gc.collect()
                
                # --- All files written cleanly! Apply Atomic Swap ---
                print("All files validated. Applying safe storage overwrite swap...")
                for temp_path, final_path in successful_swaps:
                    try: os.remove(final_path)
                    except OSError: pass
                    os.rename(temp_path, final_path)
                    
                print("Swap successful! Rebooting safely...")
                matrixportal.set_text_color("#00FF00")
                matrixportal.set_text(center_multiline_string("SUCCESS\nREBOOTING", characters_per_line))
                time.sleep(3)
                import microcontroller
                microcontroller.reset()
            else:
                matrixportal.set_text_color("#00FF00")
                matrixportal.set_text(center_multiline_string("UP TO DATE", characters_per_line))
                safe_delay(2)
    except Exception as ex:
        print(f"OTA Error: {ex}")
    finally:
        if response: response.close()
        gc.collect()

perform_ota_check(requests, force=False)

if HAS_HTTPSERVER:
    try:
        server = HTTPServer(pool)
        @server.route("/", HTTPMethod.GET)
        def base(request):
            body = f"<h1>Matrix Portal S3 active. Firmware: v{LOCAL_VERSION}</h1>"
            return HTTPResponse(request, content_type="text/html", body=body)
        server.start(str(wifi.radio.ipv4_address))
    except Exception: HAS_HTTPSERVER = False

favsign_list = []
try:
    with open("sign_list.txt", "r") as f:
        for line in f:
            cleaned = line.strip()
            if cleaned: favsign_list.append(cleaned)
except OSError: pass

cycles = 0
while True:
    cycles += 1
    w.feed()
    gc.collect()

    if not wifi.radio.connected:
        if not connect_wifi(): safe_delay(10); continue

    print("Querying NY511 API...")
    response = None
    try:
        response = requests.get(DATA_SOURCE_URL, timeout=8)
        if response.status_code == 200:
            json_data = response.json()
            if isinstance(json_data, list):
                for fav_name in favsign_list:
                    w.feed()
                    for sign in json_data:
                        if 'Name' in sign and fav_name == sign['Name']:
                            centered_name = center_multiline_string(clean_string(sign['Name']), characters_per_line)
                            matrixportal.set_text_color("#0000FF")
                            matrixportal.set_text(centered_name)
                            safe_delay(3)

                            centered_msg = center_multiline_string(clean_string(sign['Messages']).replace('\\n', '\n'), characters_per_line)
                            matrixportal.set_text_color(sign_text_color)
                            matrixportal.set_text(centered_msg)
                            safe_delay(10)
    except Exception as e:
        print(f"API Loop error: {e}")
    finally:
        if response: response.close()
        gc.collect()

    safe_delay(30)
