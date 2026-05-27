# SPDX-FileCopyrightText: 2026 Joe Engineer
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
- Graceful WiFi reconnection; displays SSID/IP on matrix at connect time.
- WebLogger captures all print() output into an in-memory ring buffer.
- Emergency Rescue Web Server (raw socket, port 80) streams the log buffer
  to any browser when adafruit_httpserver is unavailable.
- adafruit_httpserver used when available, with defensive fallback to rescue server.
- Shadow library cleanup removes stale single-file httpserver remnants on boot.
- GitHub OTA update engine driven by a JSON manifest file.
  Uses atomic .tmp-then-rename writes with Content-Length validation to prevent
  corrupt/truncated files from landing on the filesystem.
  Full multi-file updates (code + libraries) in one pass.
- Local and cloud version numbers displayed on the LED matrix at boot.

Bugfixes vs. earlier revisions:
- set_text_color() uses integer literals (0xRRGGBB), not CSS strings "#RRGGBB".
- set_text_color() and set_text() pass label index 0 explicitly.
- matrix_debug cast to bool before passing to MatrixPortal().
- OTA version comparison parses manifest JSON correctly.
- os.makedirs() replaced with per-level os.mkdir() (CircuitPython compatible).
- All imports at module top level — no deferred imports inside functions.
- safe_delay() polls the web server during all wait periods so the rescue
  page remains responsive while sign data is being displayed.
"""

# --- VERSION (keep at top for easy access) ---
LOCAL_VERSION = "2.2.1"

# --- Imports ---
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
import io
from microcontroller import watchdog as w
from watchdog import WatchDogMode
from adafruit_matrixportal.matrixportal import MatrixPortal

# --- Shadow Library Cleanup ---
# Removes stale single-file adafruit_httpserver.py/.mpy that would shadow the
# package directory and cause import failures after an OTA update.
for _shadow in ["/lib/adafruit_httpserver.py", "/lib/adafruit_httpserver.mpy"]:
    try:
        os.remove(_shadow)
    except OSError:
        pass

# --- WebLogger: in-memory ring buffer that mirrors all print() output ---
class WebLogger:
    def __init__(self, max_lines=60):
        self.buffer = []
        self.max_lines = max_lines
        self.current_line = ""

    def write(self, message):
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8")
            except Exception:
                message = str(message)
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
    """Overrides built-in print() to mirror output into the WebLogger buffer."""
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = sep.join(str(arg) for arg in args)
    web_logger.write(message + end)
    _original_print(*args, **kwargs)

def log_exception(e):
    """Logs a full exception traceback into the WebLogger."""
    try:
        stream = io.StringIO()
        traceback.print_exception(e, limit=None, file=stream)
        print(stream.getvalue())
    except Exception:
        try:
            stream = io.StringIO()
            sys.print_exception(e, stream)
            print(stream.getvalue())
        except Exception:
            print(f"Exception logging failed: {e}")

# --- Defensive adafruit_httpserver import with fallback ---
# The library renamed its classes in newer versions:
#   HTTPServer -> Server
#   HTTPResponse -> Response
#   HTTPMethod.GET -> GET (imported directly from methods)
# We try the new names first, then fall back to the old names for compatibility.
HAS_HTTPSERVER = False
web_error_message = ""

try:
    # New API (current): Server, Response, GET
    from adafruit_httpserver.server import Server
    from adafruit_httpserver.response import Response
    from adafruit_httpserver.methods import GET
    HAS_HTTPSERVER = True
except Exception as _new_err:
    try:
        # Old API fallback: HTTPServer, HTTPResponse, HTTPMethod
        from adafruit_httpserver.server import HTTPServer as Server
        from adafruit_httpserver.response import HTTPResponse as Response
        from adafruit_httpserver.methods import HTTPMethod
        GET = HTTPMethod.GET
        HAS_HTTPSERVER = True
    except Exception as _old_err:
        try:
            # Package-level fallback
            from adafruit_httpserver import HTTPServer as Server, HTTPResponse as Response, HTTPMethod
            GET = HTTPMethod.GET
            HAS_HTTPSERVER = True
        except Exception as _fallback_err:
            HAS_HTTPSERVER = False
            log_exception(_fallback_err)
            try:
                web_error_message = repr(_fallback_err).split("(")[0].split(".")[-1].replace("Error", "").upper()[:10]
            except Exception:
                web_error_message = "ERROR"

# --- Secrets (ssid, password, ny511key only) ---
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

# NY511 API URL — only the key comes from secrets, everything else is static
NY511_URL = "https://511ny.org/api/getmessagesigns?format=json&key=" + secrets["ny511key"]

# OTA manifest URL (GitHub raw) — stored in secrets so it doesn't ship in code
ENABLE_OTA   = secrets.get("enable_ota", False)
MANIFEST_URL = secrets.get("github_version_url", "")

# --- settings.json — all user-configurable values ---
# secrets.py only holds ssid/password/ny511key.
# Everything else lives here and is editable via the web UI.
SETTINGS_FILE = "settings.json"

_default_settings = {
    "color_order":          "RGB",   # Hardware pixel order
    "sign_text_color":      "#F7B500",  # Road sign yellow
    "name_display_seconds": 3,
    "msg_display_seconds":  10,
    "cycle_sleep_seconds":  30,
    "width":                192,     # Total display width in pixels (64 x num panels)
    "height":               32,      # Display height in pixels
    "depth":                6,       # Bit depth (color quality)
    "matrix_debug":         False,   # Enable MatrixPortal debug output
    "characters_per_line":  30,      # Text wrapping width
}

def load_settings():
    """Load settings.json, merging with defaults for any missing keys."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.loads(f.read())
        for k, v in _default_settings.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return dict(_default_settings)

def save_settings(data):
    """Write settings dict to settings.json atomically via .tmp swap."""
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(data))
        try:
            os.remove(SETTINGS_FILE)
        except OSError:
            pass
        os.rename(tmp, SETTINGS_FILE)
        return True
    except Exception as e:
        print(f"save_settings failed: {e}")
        return False

def hex_to_int(hex_str):
    """Convert '#RRGGBB' or 'RRGGBB' string to integer 0xRRGGBB."""
    try:
        return int(hex_str.lstrip("#"), 16)
    except Exception:
        return 0xF7B500  # fallback to sign yellow

settings = load_settings()
# All runtime values read from settings (loaded above)
color_order         = settings.get("color_order", "RGB")
sign_text_color     = hex_to_int(settings.get("sign_text_color", "#F7B500"))
name_disp_secs      = int(settings.get("name_display_seconds", 3))
msg_disp_secs       = int(settings.get("msg_display_seconds", 10))
cycle_sleep_secs    = int(settings.get("cycle_sleep_seconds", 30))
characters_per_line = int(settings.get("characters_per_line", 30))
width               = int(settings.get("width", 192))
height              = int(settings.get("height", 32))
bit_depth           = int(settings.get("depth", 6))
matrix_debug        = bool(settings.get("matrix_debug", False))
print("Settings: color_order=" + color_order + " text_color=" + hex(sign_text_color)
      + " name=" + str(name_disp_secs) + "s msg=" + str(msg_disp_secs)
      + "s cycle=" + str(cycle_sleep_secs) + "s")

# --- signs.json — favourite sign names (replaces sign_list.txt) ---
SIGNS_FILE       = "signs.json"
SIGNS_CACHE_FILE = "signs_cache.json"

def refresh_signs_cache_from_api():
    """Fetch all sign names from NY511 and save to signs_cache.json.
    Called from the main loop so watchdog feeds work correctly throughout."""
    print("Fetching NY511 sign cache...")
    matrixportal.set_text_color(0x00FFFF, 0)
    matrixportal.set_text(center_multiline_string("LOADING\nSIGNS...", characters_per_line), 0)
    w.feed()

    resp_obj = None
    try:
        resp_obj = requests.get(NY511_URL, timeout=20)
        w.feed()
        if resp_obj.status_code != 200:
            print(f"NY511 cache fetch failed: HTTP {resp_obj.status_code}")
            return False

        api_data = resp_obj.json()
        w.feed()
        resp_obj.close()
        resp_obj = None

        if not isinstance(api_data, list):
            print("NY511 cache: unexpected response format")
            return False

        sign_names = []
        for sign in api_data:
            w.feed()  # Feed watchdog while iterating 933 signs
            if "Name" in sign:
                sign_names.append(sign["Name"])

        sign_names.sort()
        api_data = None
        gc.collect()
        gc.collect()

        ok = save_signs_cache(sign_names)
        print(f"NY511 cache: {len(sign_names)} signs saved={ok}")

        matrixportal.set_text_color(0x00FF00, 0)
        matrixportal.set_text(center_multiline_string(
            "SIGNS\nCACHED", characters_per_line), 0)
        safe_delay(2)
        return ok

    except Exception as e:
        print(f"NY511 cache fetch error: {e}")
        log_exception(e)
        return False
    finally:
        if resp_obj is not None:
            try:
                resp_obj.close()
            except Exception:
                pass
        # Restore normal display color
        matrixportal.set_text_color(sign_text_color, 0)
        matrixportal.set_text("", 0)
        gc.collect()

def load_favourite_signs():
    """Load the list of favourite sign names from signs.json."""
    try:
        with open(SIGNS_FILE, "r") as f:
            data = json.loads(f.read())
        return data.get("favorites", [])
    except Exception:
        return []

def save_favourite_signs(favorites_list):
    """Save the list of favourite sign names to signs.json atomically."""
    tmp = SIGNS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps({"favorites": favorites_list}))
        try:
            os.remove(SIGNS_FILE)
        except OSError:
            pass
        os.rename(tmp, SIGNS_FILE)
        return True
    except Exception as e:
        print(f"save_favourite_signs failed: {e}")
        return False

def load_signs_cache():
    """Load cached sign names from signs_cache.json. Returns [] on any error."""
    try:
        with open(SIGNS_CACHE_FILE, "r") as f:
            data = json.loads(f.read())
        return data.get("signs", [])
    except Exception:
        return []

def save_signs_cache(sign_names):
    """Save a list of sign name strings to signs_cache.json atomically."""
    tmp = SIGNS_CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps({"signs": sign_names}))
        try:
            os.remove(SIGNS_CACHE_FILE)
        except OSError:
            pass
        os.rename(tmp, SIGNS_CACHE_FILE)
        return True
    except Exception as e:
        print(f"save_signs_cache failed: {e}")
        return False

# --- Hardware Watchdog ---
w.timeout = 45.0
w.mode = WatchDogMode.RESET
w.feed()

# --- Matrix Portal Init (once at startup) ---
print("Initializing Matrix Portal display...")
matrixportal = MatrixPortal(
    width=width,
    height=height,
    bit_depth=bit_depth,
    debug=matrix_debug,
    color_order=color_order
)
matrixportal.add_text(
    text_font=terminalio.FONT,
    text_position=(0, 15),
    scrolling=False,
    line_spacing=0.8,
    text_color=sign_text_color  # loaded from settings.json
)
w.feed()

# --- Helper Functions ---
def center_multiline_string(text, width_chars):
    """Centers each line of a multi-line string to fit the matrix display."""
    return "\n".join(line.center(width_chars) for line in text.splitlines())

def clean_string(text):
    """Strips brackets, quotes, and normalises type to str."""
    if text is None:
        return ""
    text = " ".join(str(i) for i in text) if isinstance(text, list) else str(text)
    return text.replace("[", "").replace("]", "").replace('"', "").replace("'", "")

def ensure_dir_exists(filepath):
    """Creates all parent directories for filepath using single-level os.mkdir()."""
    parts = filepath.split("/")
    if len(parts) > 1:
        current_path = ""
        for part in parts[:-1]:
            current_path = (current_path + "/" + part) if current_path else part
            try:
                os.mkdir(current_path)
            except OSError as e:
                if e.errno != 17:  # 17 = EEXIST — directory already exists, fine
                    raise

# --- Raw Socket Rescue Web Server ---
rescue_socket = None
server = None

def start_rescue_server():
    """Binds a raw TCP socket on port 80 to serve the log page when
    adafruit_httpserver is unavailable."""
    global rescue_socket, pool
    if rescue_socket is not None:
        return
    try:
        af_inet    = getattr(pool, "AF_INET",    getattr(socketpool, "AF_INET",    2))
        sock_stream = getattr(pool, "SOCK_STREAM", getattr(socketpool, "SOCK_STREAM", 1))
        rescue_socket = pool.socket(af_inet, sock_stream)
        rescue_socket.settimeout(0.02)
        try:
            import socket as _socket
            rescue_socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        rescue_socket.bind((str(wifi.radio.ipv4_address), 80))
        try:
            rescue_socket.listen(3)
        except TypeError:
            rescue_socket.listen()
        print(f"Rescue Web Server active at http://{wifi.radio.ipv4_address}/")
    except Exception as e:
        print(f"Rescue server bind failed: {e}")
        rescue_socket = None

def safe_send(conn, data):
    """Sends bytes/string over a TCP connection, retrying on EAGAIN (errno 11)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    sent_total = 0
    while sent_total < len(data):
        try:
            sent = conn.send(data[sent_total:])
            if sent == 0:
                break
            sent_total += sent
        except OSError as e:
            if e.errno == 11:  # EAGAIN — socket buffer full, retry
                time.sleep(0.01)
                continue
            raise

def poll_rescue_server():
    """Non-blocking poll: accepts one connection, sends the full log page, closes.

    Firefox requires a Content-Length header — it will show a blank page or
    connection reset if the response arrives in unpredictable chunks without one.
    Fix: build the complete HTML body as a single string, measure its byte length,
    send the header with that length, then send the body. One complete transaction.
    """
    global rescue_socket
    if rescue_socket is None:
        return
    conn = None
    try:
        conn, addr = rescue_socket.accept()
        conn.settimeout(0.5)
        request_str = ""
        buf = bytearray(512)
        for _ in range(3):
            try:
                num_bytes = conn.recv_into(buf)
                if num_bytes > 0:
                    request_str = buf[:num_bytes].decode("utf-8")
                    break
            except OSError:
                pass
            time.sleep(0.05)

        # Silently discard favicon requests — browsers always send one
        if request_str and "favicon.ico" in request_str:
            try:
                conn.send(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                conn.close()
            except Exception:
                pass
            return

        print(f"Rescue connection from {addr}")

        # Escape log content so < > & render correctly in the browser
        log_lines = list(web_logger.buffer)
        if web_logger.current_line:
            log_lines.append(web_logger.current_line)
        log_content = "\n".join(log_lines)
        log_escaped = (log_content
                       .replace("&", "&amp;")
                       .replace("<", "&lt;")
                       .replace(">", "&gt;"))

        # Build the complete HTML body as one string so we can measure it
        body = (
            "<!DOCTYPE html><html><head>"
            "<title>Matrix Portal S3 - Rescue Console</title>"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<style>"
            "body{font-family:monospace;background:#111;color:#ff3333;margin:20px;line-height:1.4;}"
            "h1{color:#ff3333;}h2{color:#ffaa00;}"
            "pre{background:#000;padding:15px;border-radius:5px;border:1px solid #333;"
            "overflow-x:auto;white-space:pre-wrap;color:#00ff00;}"
            ".meta{color:#888;font-size:0.85em;margin-bottom:10px;}"
            "</style></head><body>"
            "<h1>&#x1F6A8; Matrix Portal S3 &mdash; Rescue Console</h1>"
            "<div class=\"meta\">Firmware: v" + LOCAL_VERSION +
            " &nbsp;|&nbsp; IP: " + str(wifi.radio.ipv4_address) +
            " &nbsp;|&nbsp; Free RAM: " + str(gc.mem_free()) + " bytes</div>"
            "<h2>Log Output:</h2>"
            "<pre>" + log_escaped + "</pre>"
            "</body></html>"
        )

        # Encode once to get the true byte length for Content-Length
        body_bytes = body.encode("utf-8")
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Length: " + str(len(body_bytes)) + "\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        safe_send(conn, header)
        safe_send(conn, body_bytes)
        time.sleep(0.15)
        conn.close()

    except OSError:
        pass
    except Exception as ex:
        log_exception(ex)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        gc.collect()

def poll_server():
    """Poll the active web server once. Handles both adafruit_httpserver
    and the raw socket rescue server transparently."""
    if HAS_HTTPSERVER and server is not None:
        try:
            server.poll()
        except Exception as _poll_err:
            err_str = str(_poll_err)
            if err_str and "timed out" not in err_str and "ETIMEDOUT" not in err_str:
                print(f"server.poll() error: {err_str}")
    else:
        poll_rescue_server()

def safe_delay(seconds):
    """Sleeps for `seconds` while continuously feeding the watchdog and
    polling the web server so the log page stays responsive.
    Calls poll_server() twice per iteration to help complete multi-packet
    HTTP transactions that need rapid successive poll() calls."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        w.feed()
        poll_server()
        poll_server()  # Second poll helps complete in-progress HTTP transactions
        time.sleep(0.01)

# --- WiFi Connection ---
def connect_wifi():
    """Connects to WiFi, showing status on the matrix display."""
    w.feed()
    if wifi.radio.connected:
        return True

    ssid = secrets.get("ssid", "")
    print(f"Connecting to WiFi: {ssid}")
    try:
        matrixportal.set_text_color(0x00FFFF, 0)  # Cyan
        matrixportal.set_text(center_multiline_string(f"CONNECTING\n{ssid}", characters_per_line), 0)
    except Exception:
        pass

    try:
        wifi.radio.connect(secrets["ssid"], secrets["password"])
        w.feed()
        ip = str(wifi.radio.ipv4_address)
        print(f"Connected! IP: {ip}")
        try:
            matrixportal.set_text_color(0x00FF00, 0)  # Green
            matrixportal.set_text(center_multiline_string(f"{ip}\n{ssid}", characters_per_line), 0)
            safe_delay(5)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"WiFi connection failed: {e}")
        try:
            matrixportal.set_text_color(0xFF0000, 0)  # Red
            matrixportal.set_text(center_multiline_string("WIFI\nFAILED", characters_per_line), 0)
        except Exception:
            pass
        return False

# --- Initial Network Setup ---
gc.collect()
pool = None
ssl_context = None
requests = None

if connect_wifi():
    pool = socketpool.SocketPool(wifi.radio)
    ssl_context = ssl.create_default_context()
    requests = adafruit_requests.Session(pool, ssl_context)

    if not HAS_HTTPSERVER:
        start_rescue_server()

    # Show web server status on matrix
    try:
        if HAS_HTTPSERVER:
            matrixportal.set_text_color(0x00FF00, 0)  # Green
            matrixportal.set_text(center_multiline_string("WEB OK\nPORT 80", characters_per_line), 0)
        else:
            matrixportal.set_text_color(0xFF0000, 0)  # Red
            matrixportal.set_text(center_multiline_string(f"WEB ERR\n{web_error_message}", characters_per_line), 0)
        safe_delay(3)
    except Exception:
        pass

# --- GitHub Manifest-Based Atomic OTA Updater ---
# Manifest format expected at MANIFEST_URL:
# {
#   "version": "1.1.4",
#   "files": {
#     "code.py": "https://raw.githubusercontent.com/.../code.py",
#     "lib/adafruit_logging.py": "https://raw.githubusercontent.com/...",
#     ...
#   }
# }
def perform_ota_check(requests_session, force=False):
    if not ENABLE_OTA or not MANIFEST_URL:
        print("OTA disabled or MANIFEST_URL not set.")
        return
    if requests_session is None:
        print("OTA skipped: no network session.")
        return

    print(f"Checking for updates... Local: {LOCAL_VERSION}")

    # Display local version on matrix
    matrixportal.set_text_color(0x00FFFF, 0)  # Cyan
    matrixportal.set_text(center_multiline_string(f"LOCAL VER\n{LOCAL_VERSION}", characters_per_line), 0)
    safe_delay(2)

    # Fetch manifest with retry
    response = None
    for retry in range(3):
        w.feed()
        try:
            response = requests_session.get(MANIFEST_URL, timeout=8)
            break
        except Exception as e:
            print(f"Manifest fetch attempt {retry + 1} failed: {e}")
            if retry == 2:
                print("Could not reach GitHub. Skipping OTA.")
                return
            safe_delay(2)

    try:
        if response.status_code != 200:
            print(f"Manifest fetch returned HTTP {response.status_code}. Skipping OTA.")
            return

        # Read and close the response before parsing to free the socket first
        raw_text = response.text
        response.close()
        response = None
        w.feed()
        gc.collect()

        try:
            manifest = json.loads(raw_text)
        except Exception as parse_err:
            print(f"Manifest JSON parse failed: {parse_err}")
            print(f"Raw content (first 200 chars): {raw_text[:200]}")
            return
        finally:
            raw_text = None
            gc.collect()

        remote_version = manifest.get("version", "").strip()
        files_to_download = manifest.get("files", {})

        if not remote_version:
            print("Manifest missing 'version' field. Aborting OTA.")
            return

        print(f"Remote version: {remote_version}")

        # Display remote version on matrix
        matrixportal.set_text_color(0xFFFF00, 0)  # Yellow
        matrixportal.set_text(center_multiline_string(f"CLOUD VER\n{remote_version}", characters_per_line), 0)
        safe_delay(2)

        if remote_version == LOCAL_VERSION and not force:
            print("Firmware is up to date!")
            matrixportal.set_text_color(0x00FF00, 0)  # Green
            matrixportal.set_text(center_multiline_string("VER VERIFIED\nUP TO DATE", characters_per_line), 0)
            safe_delay(2)
            return

        # New version — download all files to .tmp paths first, then swap atomically
        print(f"Update available: {LOCAL_VERSION} -> {remote_version}")
        print(f"Downloading {len(files_to_download)} file(s)...")
        matrixportal.set_text_color(0x00FF00, 0)  # Green
        matrixportal.set_text(center_multiline_string("DOWNLOADING\nFILES...", characters_per_line), 0)

        successful_swaps = []  # List of (temp_path, final_path) tuples
        total = len(files_to_download)
        current = 0

        for local_path, remote_url in files_to_download.items():
            current += 1
            w.feed()
            print(f"  [{current}/{total}] {local_path}")

            ensure_dir_exists(local_path)
            temp_path = local_path + ".tmp"
            file_response = None

            try:
                file_response = requests_session.get(remote_url, timeout=15)
                w.feed()
                if file_response.status_code != 200:
                    raise RuntimeError(f"HTTP {file_response.status_code}")

                file_content = file_response.text
                w.feed()

                # Validate size against Content-Length if server provides it
                content_length = file_response.headers.get("content-length")
                if content_length is not None:
                    expected = int(content_length)
                    actual = len(file_content.encode("utf-8"))
                    if actual != expected:
                        raise RuntimeError(f"Size mismatch: got {actual}, expected {expected}")

                # Write to .tmp first — never touch the live file until validated
                with open(temp_path, "w") as f:
                    f.write(file_content)

                successful_swaps.append((temp_path, local_path))
                print(f"    Staged: {temp_path}")

            except OSError as fs_err:
                print(f"    Write error for {local_path}: {fs_err}")
                if local_path == "code.py":
                    matrixportal.set_text_color(0xFF0000, 0)  # Red
                    matrixportal.set_text(center_multiline_string("WRITE\nLOCKED", characters_per_line), 0)
                    safe_delay(5)
                    return
                print("    Aborting update to prevent partial state.")
                return
            except Exception as dl_err:
                print(f"    Download failed for {local_path}: {dl_err}")
                print("    Aborting update to prevent partial state.")
                return
            finally:
                if file_response is not None:
                    try:
                        file_response.close()
                    except Exception:
                        pass
                gc.collect()

        # All files downloaded and validated — now do the atomic rename swap
        print("All files staged. Applying atomic swap...")
        for temp_path, final_path in successful_swaps:
            try:
                os.remove(final_path)
            except OSError:
                pass
            os.rename(temp_path, final_path)
            print(f"    Installed: {final_path}")

        print("Update complete! Rebooting...")
        matrixportal.set_text_color(0x00FF00, 0)  # Green
        matrixportal.set_text(center_multiline_string(f"SUCCESS\nNEW:{remote_version}", characters_per_line), 0)
        safe_delay(4)
        microcontroller.reset()

    except Exception as ex:
        print(f"OTA error: {ex}")
        log_exception(ex)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        gc.collect()

# Run OTA check at startup
perform_ota_check(requests, force=False)

# --- Shared HTML page helpers ---
VALID_COLOR_ORDERS = ["RGB", "RBG", "GRB", "GBR", "BRG", "BGR"]

def html_head(title):
    return (
        "<!DOCTYPE html><html><head>"
        "<title>" + title + "</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>"
        "body{font-family:monospace;background:#111;color:#eee;margin:0;padding:20px;}"
        "h1{color:#ff3333;margin-top:0;}h2{color:#ffaa00;}"
        "nav{background:#1a1a1a;padding:10px 20px;margin:-20px -20px 20px -20px;"
        "border-bottom:1px solid #333;}"
        "nav a{color:#00ccff;text-decoration:none;margin-right:20px;font-size:1.1em;}"
        "nav a:hover{color:#fff;}"
        "nav a.active{color:#fff;border-bottom:2px solid #ff3333;padding-bottom:2px;}"
        ".meta{color:#888;font-size:0.85em;margin-bottom:15px;}"
        ".card{background:#1a1a1a;border:1px solid #333;border-radius:6px;"
        "padding:15px;margin-bottom:15px;}"
        "pre{background:#000;padding:15px;border-radius:5px;color:#00ff00;"
        "white-space:pre-wrap;overflow-x:auto;margin:0;}"
        "button{font-family:monospace;font-size:1em;padding:8px 18px;"
        "border:none;border-radius:4px;cursor:pointer;margin:4px;}"
        "input[type=text],input[type=number]{"
        "font-family:monospace;font-size:1em;padding:6px 10px;"
        "background:#222;color:#eee;border:1px solid #555;border-radius:4px;width:80px;}"
        "select{font-family:monospace;font-size:1em;padding:6px 10px;"
        "background:#222;color:#eee;border:1px solid #555;border-radius:4px;}"
        "label{display:inline-block;margin-bottom:6px;color:#aaa;min-width:120px;}"
        ".row{margin-bottom:10px;}"
        ".btn-red{background:#cc2222;color:#fff;}.btn-red:hover{background:#ff3333;}"
        ".btn-green{background:#226622;color:#fff;}.btn-green:hover{background:#33aa33;}"
        ".btn-blue{background:#224499;color:#fff;}.btn-blue:hover{background:#3366cc;}"
        ".btn-yellow{background:#886600;color:#fff;}.btn-yellow:hover{background:#bbaa00;}"
        ".btn-gray{background:#444;color:#fff;}.btn-gray:hover{background:#666;}"
        ".btn-cyan{background:#006666;color:#fff;}.btn-cyan:hover{background:#009999;}"
        ".status-ok{color:#33ff33;}.status-err{color:#ff3333;}"
        "table.calib{border-collapse:collapse;width:100%;margin-top:10px;}"
        "table.calib td{padding:10px;text-align:center;border:1px solid #333;vertical-align:top;width:33%;}"
        "table.calib .swatch{width:60px;height:60px;border-radius:6px;margin:0 auto 10px auto;}"
        "table.calib .color-btn{display:block;width:100%;margin:3px 0;}"
        "#sign-filter{width:100%;max-width:400px;margin-bottom:10px;padding:8px;}"
        ".sign-list{max-height:400px;overflow-y:auto;border:1px solid #333;"
        "border-radius:4px;padding:8px;background:#000;}"
        ".sign-item{padding:4px 0;border-bottom:1px solid #1a1a1a;}"
        ".sign-item label{color:#ccc;min-width:0;cursor:pointer;}"
        ".sign-item input[type=checkbox]{margin-right:8px;cursor:pointer;}"
        ".sign-item.fav label{color:#F7B500;}"
        ".color-preview{display:inline-block;width:24px;height:24px;"
        "border-radius:3px;border:1px solid #555;vertical-align:middle;margin-left:8px;}"
        "</style></head>"
    )

def html_nav(active):
    tabs = [("log","/","&#x1F4CB; Log"),
            ("settings","/settings","&#x2699;&#xFE0F; Settings"),
            ("signs","/signs","&#x1F6A6; Traffic Signs")]
    nav = "<nav>"
    for key, href, label in tabs:
        cls = " class=\"active\"" if active == key else ""
        nav += "<a href=\"" + href + "\"" + cls + ">" + label + "</a>"
    nav += "</nav>"
    return nav

def html_meta():
    return (
        "<div class=\"meta\">Firmware: v" + LOCAL_VERSION +
        " &nbsp;|&nbsp; IP: " + str(wifi.radio.ipv4_address) +
        " &nbsp;|&nbsp; Free RAM: " + str(gc.mem_free()) + " bytes</div>"
    )

def secs_to_hms(total):
    """Convert total seconds integer to (h, m, s) tuple."""
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return h, m, s

def hms_to_secs(h, m, s):
    """Convert h/m/s to total seconds, clamped to minimum 1 second."""
    total = int(h) * 3600 + int(m) * 60 + int(s)
    return max(1, total)

def parse_post_body(request):
    """Parse a URL-encoded POST body into a dict of key->value strings."""
    params = {}
    try:
        body_str = request.body.decode("utf-8") if request.body else ""
        for part in body_str.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip()
    except Exception as e:
        print(f"parse_post_body error: {e}")
    return params

_calib_state = {"active": False}
_refresh_cache_pending = False  # Set by web UI, consumed by main loop

# --- Start adafruit_httpserver if available ---
if HAS_HTTPSERVER and pool is not None:
    try:
        server = Server(pool)
        server.socket_timeout = 0.05

        # Force Connection: close on all responses so Firefox doesn't wait
        # for persistent connection data that never arrives.
        # adafruit_httpserver doesn't send Content-Length by default,
        # which causes Firefox (strict HTTP/1.1) to hang waiting for more data.
        try:
            server.headers["Connection"] = "close"
        except Exception:
            pass  # If headers API differs, not fatal — Edge will still work

        # ── GET / — Log page ──────────────────────────────────────────────
        @server.route("/", GET)
        def route_index(request):
            logs_html = (web_logger.get_logs()
                         .replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))
            body = (
                html_head("Matrix Portal S3 - Log") +
                "<body>" + html_nav("log") +
                "<h1>&#x1F6A8; Matrix Portal S3</h1>" + html_meta() +
                "<div class=\"card\"><h2>Log Output:</h2>"
                "<pre>" + logs_html + "</pre></div>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── POST /reboot ──────────────────────────────────────────────────
        @server.route("/reboot", "POST")
        def route_reboot(request):
            print("Reboot requested via web UI.")
            body = (
                html_head("Rebooting...") +
                "<body>" + html_nav("log") +
                "<h1>&#x1F6A8; Rebooting...</h1>"
                "<p>The board is rebooting. Reconnecting in 8 seconds...</p>"
                "<script>setTimeout(function(){window.location='/'},8000);</script>"
                "</body></html>"
            )
            resp = Response(request, content_type="text/html", body=body)
            import supervisor
            supervisor.reload()
            return resp

        # ── GET /settings ─────────────────────────────────────────────────
        @server.route("/settings", GET)
        def route_settings(request):
            cur_order = settings.get("color_order", "RGB")
            cur_color = settings.get("sign_text_color", "#F7B500")
            n_h, n_m, n_s = secs_to_hms(int(settings.get("name_display_seconds", 3)))
            m_h, m_m, m_s = secs_to_hms(int(settings.get("msg_display_seconds", 10)))
            c_h, c_m, c_s = secs_to_hms(int(settings.get("cycle_sleep_seconds", 30)))

            order_opts = ""
            for o in VALID_COLOR_ORDERS:
                sel = " selected" if o == cur_order else ""
                order_opts += "<option value=\"" + o + "\"" + sel + ">" + o + "</option>"

            def hms_inputs(prefix, h, m, s):
                return (
                    "<input type=\"number\" name=\"" + prefix + "_h\" value=\"" + str(h) +
                    "\" min=\"0\" max=\"23\" style=\"width:55px\"> h &nbsp;"
                    "<input type=\"number\" name=\"" + prefix + "_m\" value=\"" + str(m) +
                    "\" min=\"0\" max=\"59\" style=\"width:55px\"> m &nbsp;"
                    "<input type=\"number\" name=\"" + prefix + "_s\" value=\"" + str(s) +
                    "\" min=\"0\" max=\"59\" style=\"width:55px\"> s"
                )

            body = (
                html_head("Matrix Portal S3 - Settings") +
                "<body>" + html_nav("settings") +
                "<h1>&#x2699;&#xFE0F; Settings</h1>" + html_meta() +

                # Reboot
                "<div class=\"card\"><h2>System</h2>"
                "<form method=\"POST\" action=\"/reboot\" style=\"display:inline\">"
                "<button class=\"btn-red\" type=\"submit\">&#x1F504; Reboot Board</button>"
                "</form></div>"

                # All settings in one form
                "<div class=\"card\"><h2>Display Settings</h2>"
                "<form method=\"POST\" action=\"/save-settings\">"

                "<div class=\"row\"><label>Color Order:</label>"
                "<select name=\"color_order\">" + order_opts + "</select></div>"

                "<div class=\"row\"><label>Sign Text Color:</label>"
                "<input type=\"color\" name=\"sign_text_color\" value=\"" + cur_color + "\">"
                "<span class=\"color-preview\" id=\"cprev\" "
                "style=\"background:" + cur_color + "\"></span>"
                "<script>document.querySelector('[name=sign_text_color]').oninput=function(){"
                "document.getElementById('cprev').style.background=this.value;};</script>"
                "</div>"

                "<div class=\"row\"><label>Name display:</label>" +
                hms_inputs("name", n_h, n_m, n_s) + "</div>"

                "<div class=\"row\"><label>Message display:</label>" +
                hms_inputs("msg", m_h, m_m, m_s) + "</div>"

                "<div class=\"row\"><label>Cycle interval:</label>" +
                hms_inputs("cycle", c_h, c_m, c_s) + "</div>"

                "<br><button class=\"btn-green\" type=\"submit\">&#x1F4BE; Save Settings</button>"
                "</form></div>"

                # RGB Calibration
                "<div class=\"card\"><h2>&#x1F3A8; RGB Order Calibration Wizard</h2>"
                "<p style=\"color:#aaa\">Illuminates each panel a different primary color "
                "at 20% brightness so you can identify the correct order for your hardware.</p>"
                "<form method=\"POST\" action=\"/calibrate\">"
                "<button class=\"btn-yellow\" type=\"submit\">&#x25B6; Start Calibration</button>"
                "</form></div>"

                "</body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── POST /save-settings ───────────────────────────────────────────
        @server.route("/save-settings", "POST")
        def route_save_settings(request):
            global sign_text_color, name_disp_secs, msg_disp_secs, cycle_sleep_secs
            try:
                p = parse_post_body(request)
                new_order = p.get("color_order", "RGB").upper()
                if new_order not in VALID_COLOR_ORDERS:
                    new_order = "RGB"
                new_color = p.get("sign_text_color", "#F7B500")
                if not new_color.startswith("#"):
                    new_color = "#F7B500"

                new_name_secs  = hms_to_secs(p.get("name_h",0), p.get("name_m",0), p.get("name_s",3))
                new_msg_secs   = hms_to_secs(p.get("msg_h",0),  p.get("msg_m",0),  p.get("msg_s",10))
                new_cycle_secs = hms_to_secs(p.get("cycle_h",0),p.get("cycle_m",0),p.get("cycle_s",30))

                settings["color_order"]          = new_order
                settings["sign_text_color"]       = new_color
                settings["name_display_seconds"]  = new_name_secs
                settings["msg_display_seconds"]   = new_msg_secs
                settings["cycle_sleep_seconds"]   = new_cycle_secs

                ok = save_settings(settings)

                # Apply timing changes immediately (no reboot needed)
                name_disp_secs   = new_name_secs
                msg_disp_secs    = new_msg_secs
                cycle_sleep_secs = new_cycle_secs
                sign_text_color  = hex_to_int(new_color)
                matrixportal.set_text_color(sign_text_color, 0)

                status = "Saved! Color order change requires reboot." if ok else "Save failed."
                cls = "status-ok" if ok else "status-err"
                print("Settings saved: order=" + new_order + " color=" + new_color
                      + " name=" + str(new_name_secs) + "s msg=" + str(new_msg_secs)
                      + "s cycle=" + str(new_cycle_secs) + "s")
            except Exception as e:
                status = "Error: " + str(e)
                cls = "status-err"
                log_exception(e)

            body = (
                html_head("Settings Saved") +
                "<body>" + html_nav("settings") +
                "<h1>&#x2699;&#xFE0F; Settings</h1>" + html_meta() +
                "<div class=\"card\"><p class=\"" + cls + "\">" + status + "</p>"
                "<a href=\"/settings\"><button class=\"btn-gray\">&#x2190; Back</button></a>"
                "&nbsp;<form method=\"POST\" action=\"/reboot\" style=\"display:inline\">"
                "<button class=\"btn-red\" type=\"submit\">&#x1F504; Reboot Now</button>"
                "</form></div></body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── GET /signs — Traffic Signs page ───────────────────────────────
        @server.route("/signs", GET)
        def route_signs(request):
            cached = load_signs_cache()
            favs   = set(load_favourite_signs())
            cache_count = len(cached)

            if cached:
                # Build checkbox list sorted alphabetically, favourites first
                fav_items   = sorted([s for s in cached if s in favs])
                other_items = sorted([s for s in cached if s not in favs])
                all_items   = fav_items + other_items

                items_html = ""
                for name in all_items:
                    checked = " checked" if name in favs else ""
                    fav_cls = " fav" if name in favs else ""
                    safe_name = (name.replace("&","&amp;").replace("<","&lt;")
                                     .replace(">","&gt;").replace("\"","&quot;"))
                    items_html += (
                        "<div class=\"sign-item" + fav_cls + "\">"
                        "<label><input type=\"checkbox\" name=\"fav\" value=\"" +
                        safe_name + "\"" + checked + "> " + safe_name + "</label></div>"
                    )

                sign_section = (
                    "<p style=\"color:#aaa\">" + str(cache_count) +
                    " signs cached. Favourites shown in "
                    "<span style=\"color:#F7B500\">yellow</span>.</p>"
                    "<input type=\"text\" id=\"sign-filter\" placeholder=\"Filter signs...\" "
                    "oninput=\"filterSigns(this.value)\" style=\"width:100%;max-width:500px;"
                    "margin-bottom:10px;padding:8px;background:#222;color:#eee;"
                    "border:1px solid #555;border-radius:4px;font-family:monospace;\">"
                    "<form method=\"POST\" action=\"/save-signs\">"
                    "<div class=\"sign-list\" id=\"signlist\">" + items_html + "</div><br>"
                    "<button class=\"btn-green\" type=\"submit\">&#x1F4BE; Save Favourites</button>"
                    "</form>"
                    "<script>"
                    "function filterSigns(q){"
                    "q=q.toLowerCase();"
                    "var items=document.querySelectorAll('.sign-item');"
                    "for(var i=0;i<items.length;i++){"
                    "items[i].style.display="
                    "items[i].textContent.toLowerCase().indexOf(q)>=0?'':'none';}}"
                    "</script>"
                )
            else:
                sign_section = (
                    "<p style=\"color:#aaa\">No sign cache found. "
                    "Click Refresh to load signs from NY511.</p>"
                )

            body = (
                html_head("Traffic Signs") +
                "<body>" + html_nav("signs") +
                "<h1>&#x1F6A6; Traffic Signs</h1>" + html_meta() +
                "<div class=\"card\">"
                "<form method=\"POST\" action=\"/refresh-signs-cache\" style=\"display:inline\">"
                "<button class=\"btn-cyan\" type=\"submit\">&#x1F504; Refresh from NY511</button>"
                "</form>"
                "<span style=\"color:#888;margin-left:15px;font-size:0.9em\">"
                "Fetches ~930 signs. Takes 15-20 seconds.</span>"
                "</div>"
                "<div class=\"card\">" + sign_section + "</div>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── POST /refresh-signs-cache — Queue a NY511 cache refresh ────────
        # Returns immediately to the browser to avoid watchdog timeout.
        # The actual fetch is done by the main loop on the next cycle.
        @server.route("/refresh-signs-cache", "POST")
        def route_refresh_cache(request):
            global _refresh_cache_pending
            _refresh_cache_pending = True
            print("NY511 cache refresh queued — will run on next main loop cycle.")
            body = (
                html_head("Refreshing Signs...") +
                "<body>" + html_nav("signs") +
                "<h1>&#x1F6A6; Traffic Signs</h1>" + html_meta() +
                "<div class=\"card\">"
                "<p style=\"color:#ffaa00\">&#x23F3; Fetching signs from NY511...</p>"
                "<p style=\"color:#aaa\">This takes 15-20 seconds. "
                "The page will redirect automatically when done.</p>"
                "<script>setTimeout(function(){window.location='/signs'},25000);</script>"
                "</div></body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── POST /save-signs — Save favourite selections ──────────────────
        @server.route("/save-signs", "POST")
        def route_save_signs(request):
            global favsign_list
            try:
                body_str = request.body.decode("utf-8") if request.body else ""
                selected = []
                for part in body_str.split("&"):
                    if part.startswith("fav="):
                        # URL-decode the sign name (replace + with space, %XX)
                        name = part[4:]
                        name = name.replace("+", " ")
                        # Basic percent-decode for common chars
                        for code, char in [("%2C",","),("%28","("),("%29",")")
                                           ,("%2F","/"),("%3A",":")]:
                            name = name.replace(code, char)
                        if name:
                            selected.append(name)
                ok = save_favourite_signs(selected)
                favsign_list = selected  # Update live list immediately
                status = "Saved " + str(len(selected)) + " favourite sign(s)."
                cls = "status-ok" if ok else "status-err"
                print(f"Favourites saved: {len(selected)} signs ok={ok}")
            except Exception as e:
                status = "Error: " + str(e)
                cls = "status-err"
                log_exception(e)

            body = (
                html_head("Signs Saved") +
                "<body>" + html_nav("signs") +
                "<h1>&#x1F6A6; Traffic Signs</h1>" + html_meta() +
                "<div class=\"card\"><p class=\"" + cls + "\">" + status + "</p>"
                "<a href=\"/signs\"><button class=\"btn-gray\">&#x2190; Back to Signs</button></a>"
                "</div></body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── POST /calibrate — Light panels with numbers, show color picker ──
        @server.route("/calibrate", "POST")
        def route_calibrate(request):
            global _calib_state
            _calib_state["active"] = True
            try:
                import displayio

                # 5x7 pixel digit bitmaps for 1, 2, 3
                # Each is a list of (col, row) pixel offsets to SET (foreground)
                DIGITS = {
                    "1": [(2,0),(1,1),(2,1),(2,2),(2,3),(2,4),(2,5),(2,6)],
                    "2": [(1,0),(2,0),(3,0),(0,1),(3,1),(3,2),(2,3),(1,4),(0,5),(0,6),(1,6),(2,6),(3,6)],
                    "3": [(0,0),(1,0),(2,0),(3,0),(3,1),(3,2),(1,2),(2,2),(3,3),(3,4),(0,5),(3,5),(1,6),(2,6)],
                }

                display = matrixportal.display
                panel_w = width // 3

                # 4 palette entries: bg colors + white for digit
                # 0=dim red, 1=dim green, 2=dim blue, 3=white digit
                bmp = displayio.Bitmap(width, height, 4)
                pal = displayio.Palette(4)
                pal[0] = 0x330000  # dim red   — panel 1
                pal[1] = 0x003300  # dim green — panel 2
                pal[2] = 0x000033  # dim blue  — panel 3
                pal[3] = 0x555555  # dim white — digit pixels (20% brightness)

                # Fill panel background colors
                for y in range(height):
                    for x in range(width):
                        bmp[x, y] = 0 if x < panel_w else (1 if x < panel_w * 2 else 2)

                # Draw digit for each panel, centered within that panel
                for panel_idx, digit in enumerate(["1", "2", "3"]):
                    panel_start_x = panel_idx * panel_w
                    # Center the 5x7 digit within the 64x32 panel
                    digit_x = panel_start_x + (panel_w - 5) // 2
                    digit_y = (height - 7) // 2
                    for (dx, dy) in DIGITS[digit]:
                        px = digit_x + dx
                        py = digit_y + dy
                        if 0 <= px < width and 0 <= py < height:
                            bmp[px, py] = 3  # white digit pixel

                tg = displayio.TileGrid(bmp, pixel_shader=pal)
                splash = displayio.Group()
                splash.append(tg)
                display.root_group = splash
                w.feed()
            except Exception as disp_err:
                print(f"Calibration display error: {disp_err}")
                log_exception(disp_err)

            # Web UI — panels now labeled P1/P2/P3 to match numbers on display
            rows = ""
            panels = [("P1","R","330000","1"),("P2","G","003300","2"),("P3","B","000033","3")]
            for name, sent, swatch, num in panels:
                btns = ""
                for c, lbl, cls in [("R","Red","btn-red"),("G","Green","btn-green"),("B","Blue","btn-blue")]:
                    btns += (
                        "<button class=\"" + cls + " color-btn\" type=\"button\" "
                        "onclick=\"pick('" + name + "','" + c + "',this)\">" + lbl + "</button>"
                    )
                rows += (
                    "<td>"
                    "<div class=\"swatch\" style=\"background:#" + swatch + ";"
                    "font-size:28px;font-weight:bold;color:#aaa;line-height:60px;\">" + num + "</div>"
                    "<strong>Panel " + num + "</strong><br>"
                    "<small style=\"color:#888\">Board sent: " + sent + "</small><br><br>"
                    + btns +
                    "<div id=\"sel_" + name + "\" style=\"margin-top:6px;color:#ffaa00;\"></div></td>"
                )

            body = (
                html_head("RGB Calibration") +
                "<body>" + html_nav("settings") +
                "<h1>&#x1F3A8; RGB Calibration</h1>" + html_meta() +
                "<div class=\"card\">"
                "<p style=\"color:#aaa\">Your display now shows each panel lit a dim color "
                "with its panel number (1, 2, 3). "
                "For each numbered panel, click the color you "
                "<strong>actually see</strong> on that panel below.</p>"
                "<form method=\"POST\" action=\"/calibrate-result\" id=\"calform\">"
                "<input type=\"hidden\" name=\"p1\" id=\"v_P1\">"
                "<input type=\"hidden\" name=\"p2\" id=\"v_P2\">"
                "<input type=\"hidden\" name=\"p3\" id=\"v_P3\">"
                "<table class=\"calib\"><tr>" + rows + "</tr></table><br>"
                "<button class=\"btn-green\" type=\"submit\" id=\"applybtn\" disabled "
                "style=\"font-size:1.1em;padding:10px 30px;\">&#x2713; Apply Color Order</button>"
                "<a href=\"/settings\"><button class=\"btn-gray\" type=\"button\" "
                "style=\"margin-left:10px;\">Cancel</button></a>"
                "</form></div>"
                "<script>"
                "var picks={};"
                "function pick(panel,color,btn){"
                "picks[panel]=color;"
                "document.getElementById('v_'+panel).value=color;"
                "var cell=btn.parentNode;"
                "var btns=cell.querySelectorAll('button');"
                "for(var i=0;i<btns.length;i++){btns[i].style.opacity='0.4';}"
                "btn.style.opacity='1';btn.style.outline='2px solid #fff';"
                "document.getElementById('sel_'+panel).textContent='Selected: '+color;"
                "if(picks['P1']&&picks['P2']&&picks['P3']){"
                "document.getElementById('applybtn').disabled=false;}}"
                "</script>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        # ── POST /calibrate-result ────────────────────────────────────────
        @server.route("/calibrate-result", "POST")
        def route_calibrate_result(request):
            global _calib_state
            _calib_state["active"] = False
            try:
                matrixportal.display.root_group = matrixportal.splash
            except Exception:
                pass
            try:
                p = parse_post_body(request)
                p1 = p.get("p1", "R").upper()
                p2 = p.get("p2", "G").upper()
                p3 = p.get("p3", "B").upper()
                # Board sent P1=R, P2=G, P3=B.
                # User reports what they see on each numbered panel.
                # Concatenating gives the correct color_order string.
                order_str = p1 + p2 + p3
                if order_str not in VALID_COLOR_ORDERS:
                    raise ValueError("Invalid order: " + order_str)
                settings["color_order"] = order_str
                ok = save_settings(settings)
                print(f"Calibration: P1={p1} P2={p2} P3={p3} -> {order_str} saved={ok}")
                status = ("Color order set to <strong>" + order_str + "</strong>. " +
                          ("Saved! Reboot to apply." if ok else "Save failed."))
                cls = "status-ok" if ok else "status-err"
            except Exception as e:
                status = "Calibration error: " + str(e)
                cls = "status-err"
                log_exception(e)

            body = (
                html_head("Calibration Complete") +
                "<body>" + html_nav("settings") +
                "<h1>&#x1F3A8; Calibration Complete</h1>" + html_meta() +
                "<div class=\"card\"><p class=\"" + cls + "\">" + status + "</p>"
                "<a href=\"/settings\"><button class=\"btn-gray\">&#x2190; Back to Settings</button></a>"
                "&nbsp;<form method=\"POST\" action=\"/reboot\" style=\"display:inline\">"
                "<button class=\"btn-red\" type=\"submit\">&#x1F504; Reboot Now</button>"
                "</form></div></body></html>"
            )
            return Response(request, content_type="text/html", headers={"Connection":"close"}, body=body)

        server.start("0.0.0.0", port=80)
        print(f"Web server active at http://{wifi.radio.ipv4_address}/")
    except Exception as e:
        print(f"Web server start failed: {e}")
        log_exception(e)
        HAS_HTTPSERVER = False

# --- Load Favourite Signs List ---
favsign_list = load_favourite_signs()
if favsign_list:
    print(f"Loaded {len(favsign_list)} favourite sign(s) from signs.json.")
else:
    print("No favourite signs loaded (signs.json missing or empty).")
    print("Visit the Traffic Signs tab on the web UI to select signs.")

# --- Main Loop ---
cycles = 0
w.feed()

while True:
    cycles += 1
    print(f"\n{'*' * 48}")
    print(f"Cycle #{cycles}  Free RAM: {gc.mem_free()} bytes")
    print(f"{'*' * 48}")
    w.feed()
    gc.collect()

    # Handle pending sign cache refresh (queued by web UI)
    if _refresh_cache_pending:
        _refresh_cache_pending = False
        if wifi.radio.connected and requests is not None:
            refresh_signs_cache_from_api()
        else:
            print("Cache refresh skipped — no network.")

    # Reconnect WiFi if dropped
    if not wifi.radio.connected:
        print("WiFi dropped. Reconnecting...")
        if not connect_wifi():
            print("Reconnect failed. Retrying in 10s...")
            safe_delay(10)
            continue

    # Fetch NY511 API data
    print("Fetching NY511 API data...")
    response = None
    json_data = None
    matched_signs = []  # Only store the signs we actually need to display
    try:
        poll_server()  # Service web requests before API fetch

        # Poll rapidly while waiting for API response — this is the longest
        # blocking call and most likely time for a browser to connect
        _fetch_done = [False]
        response = requests.get(NY511_URL, timeout=15)
        w.feed()

        # Drain any pending web requests immediately after fetch completes
        for _ in range(5):
            poll_server()
        gc.collect()

        if response.status_code == 200:
            print("API fetch successful. Parsing JSON...")
            json_data = response.json()
            w.feed()

            # Close response immediately after parsing to free socket buffer RAM
            try:
                response.close()
            except Exception:
                pass
            response = None

            poll_server()  # Service web requests after JSON parse

            if isinstance(json_data, list):
                sign_count = len(json_data)
                print(f"Loaded {sign_count} signs from API.")
                print("Extracting favourites...")

                # Extract only matching signs into a small list, then free the full dataset
                fav_set = set(favsign_list)
                for sign in json_data:
                    if "Name" in sign and "Messages" in sign and sign["Name"] in fav_set:
                        matched_signs.append({
                            "name": sign["Name"],
                            "msg": sign["Messages"]
                        })

                # Free the full 928-sign JSON list NOW before display loops
                json_data = None
                gc.collect()
                gc.collect()  # Double-collect — CircuitPython sometimes needs two passes
                print(f"Matched {len(matched_signs)} sign(s). Free RAM after GC: {gc.mem_free()}")

                poll_server()  # Service web requests before display loop

                for match in matched_signs:
                    w.feed()
                    print(f"\nMATCH: {match['name']}")

                    # Display sign name in blue
                    centered_name = center_multiline_string(
                        clean_string(match["name"]), characters_per_line)
                    print(f"Name display:\n{centered_name}")
                    matrixportal.set_text_color(0x0000FF, 0)
                    matrixportal.set_text(centered_name, 0)
                    safe_delay(name_disp_secs)

                    # Display sign message in road-sign yellow
                    centered_msg = center_multiline_string(
                        clean_string(match["msg"]).replace("\\n", "\n"),
                        characters_per_line)
                    print(f"Message display:\n{centered_msg}")
                    matrixportal.set_text_color(sign_text_color, 0)
                    matrixportal.set_text(centered_msg, 0)
                    safe_delay(msg_disp_secs)
            else:
                print(f"Unexpected API response type: {type(json_data)}")
        else:
            print(f"API returned HTTP {response.status_code}")

    except Exception as e:
        print(f"API/display error: {e}")
        log_exception(e)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        json_data = None
        matched_signs = None
        gc.collect()
        gc.collect()

    print(f"Cycle complete. Waiting {cycle_sleep_secs}s...")
    safe_delay(cycle_sleep_secs)

