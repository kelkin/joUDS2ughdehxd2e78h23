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
LOCAL_VERSION = "2.0.0"

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

# --- Configuration & Secrets ---
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

DATA_SOURCE_URL = secrets["url_prefix"] + secrets["ny511key"] + secrets["url_suffix"]
ENABLE_OTA      = secrets.get("enable_ota", False)
MANIFEST_URL    = secrets.get("github_version_url", "")

debug               = secrets.get("debug", 0)
width               = int(secrets.get("width", 64))
height              = int(secrets.get("height", 32))
bit_depth           = int(secrets.get("depth", 4))
matrix_debug        = bool(secrets.get("matrix_debug", False))
characters_per_line = int(secrets.get("characters_per_line", 10))
sign_text_color     = secrets.get("sign_text_color", 0xF7B500)  # Road sign yellow

# --- Load settings.json (user-configurable, survives OTA) ---
SETTINGS_FILE = "settings.json"
_default_settings = {"color_order": secrets.get("color_order", "RGB")}

def load_settings():
    """Load settings.json, returning defaults for any missing keys."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.loads(f.read())
        # Merge with defaults so new keys always exist
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

settings = load_settings()
color_order = settings.get("color_order", "RGB")
print(f"Settings loaded. color_order={color_order}")

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
    text_color=sign_text_color
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
            # Log poll errors so they appear in the rescue console if needed
            # but don't crash the main loop
            err_str = str(_poll_err)
            if err_str and "timed out" not in err_str and "ETIMEDOUT" not in err_str:
                print(f"server.poll() error: {err_str}")
    else:
        poll_rescue_server()

def safe_delay(seconds):
    """Sleeps for `seconds` while continuously feeding the watchdog and
    polling the web server so the log page stays responsive."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        w.feed()
        poll_server()
        time.sleep(0.02)

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
        "select{font-family:monospace;font-size:1em;padding:6px 10px;"
        "background:#222;color:#eee;border:1px solid #555;border-radius:4px;}"
        "label{display:block;margin-bottom:6px;color:#aaa;}"
        ".btn-red{background:#cc2222;color:#fff;}.btn-red:hover{background:#ff3333;}"
        ".btn-green{background:#226622;color:#fff;}.btn-green:hover{background:#33aa33;}"
        ".btn-blue{background:#224499;color:#fff;}.btn-blue:hover{background:#3366cc;}"
        ".btn-yellow{background:#886600;color:#fff;}.btn-yellow:hover{background:#bbaa00;}"
        ".btn-gray{background:#444;color:#fff;}.btn-gray:hover{background:#666;}"
        ".status-ok{color:#33ff33;}.status-err{color:#ff3333;}"
        "table.calib{border-collapse:collapse;width:100%;margin-top:10px;}"
        "table.calib td{padding:10px;text-align:center;border:1px solid #333;vertical-align:top;width:33%;}"
        "table.calib .swatch{width:60px;height:60px;border-radius:6px;margin:0 auto 10px auto;}"
        "table.calib .color-btn{display:block;width:100%;margin:3px 0;}"
        "</style></head>"
    )

def html_nav(active):
    log_cls = " class=\"active\"" if active == "log" else ""
    set_cls = " class=\"active\"" if active == "settings" else ""
    return (
        "<nav>"
        "<a href=\"/\"" + log_cls + ">&#x1F4CB; Log</a>"
        "<a href=\"/settings\"" + set_cls + ">&#x2699;&#xFE0F; Settings</a>"
        "</nav>"
    )

def html_meta():
    return (
        "<div class=\"meta\">Firmware: v" + LOCAL_VERSION +
        " &nbsp;|&nbsp; IP: " + str(wifi.radio.ipv4_address) +
        " &nbsp;|&nbsp; Free RAM: " + str(gc.mem_free()) + " bytes</div>"
    )

_calib_state = {"active": False}

# --- Start adafruit_httpserver if available ---
if HAS_HTTPSERVER and pool is not None:
    try:
        server = Server(pool)
        server.socket_timeout = 0.05

        # ── GET / — Log page ──────────────────────────────────────────────
        @server.route("/", GET)
        def route_index(request):
            logs_html = web_logger.get_logs().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            body = (
                html_head("Matrix Portal S3 - Log") +
                "<body>" + html_nav("log") +
                "<h1>&#x1F6A8; Matrix Portal S3</h1>" + html_meta() +
                "<div class=\"card\"><h2>Log Output:</h2>"
                "<pre>" + logs_html + "</pre></div>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", body=body)

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
            current = settings.get("color_order", "RGB")
            opts = ""
            for o in VALID_COLOR_ORDERS:
                sel = " selected" if o == current else ""
                opts += "<option value=\"" + o + "\"" + sel + ">" + o + "</option>"
            body = (
                html_head("Matrix Portal S3 - Settings") +
                "<body>" + html_nav("settings") +
                "<h1>&#x2699;&#xFE0F; Settings</h1>" + html_meta() +
                "<div class=\"card\"><h2>System</h2>"
                "<form method=\"POST\" action=\"/reboot\" style=\"display:inline\">"
                "<button class=\"btn-red\" type=\"submit\">&#x1F504; Reboot Board</button>"
                "</form></div>"
                "<div class=\"card\"><h2>Display Color Order</h2>"
                "<p style=\"color:#aaa\">Current: <strong style=\"color:#fff\">" + current + "</strong>"
                " &mdash; Change takes effect after reboot.</p>"
                "<form method=\"POST\" action=\"/save-settings\">"
                "<label for=\"co\">Color Order:</label>"
                "<select name=\"color_order\" id=\"co\">" + opts + "</select>"
                "&nbsp;<button class=\"btn-green\" type=\"submit\">&#x1F4BE; Save</button>"
                "</form></div>"
                "<div class=\"card\"><h2>&#x1F3A8; RGB Order Calibration Wizard</h2>"
                "<p style=\"color:#aaa\">Illuminates each panel a different primary color "
                "at 20% brightness so you can identify the correct order for your hardware.</p>"
                "<form method=\"POST\" action=\"/calibrate\">"
                "<button class=\"btn-yellow\" type=\"submit\">&#x25B6; Start Calibration</button>"
                "</form></div>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", body=body)

        # ── POST /save-settings ───────────────────────────────────────────
        @server.route("/save-settings", "POST")
        def route_save_settings(request):
            try:
                body_str = request.body.decode("utf-8") if request.body else ""
                new_order = "RGB"
                for part in body_str.split("&"):
                    if part.startswith("color_order="):
                        new_order = part.split("=", 1)[1].strip().upper()
                        break
                if new_order not in VALID_COLOR_ORDERS:
                    new_order = "RGB"
                settings["color_order"] = new_order
                ok = save_settings(settings)
                status = "Saved! Reboot to apply." if ok else "Save failed."
                cls = "status-ok" if ok else "status-err"
                print(f"color_order saved: {new_order} ok={ok}")
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
                "</form></div>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", body=body)

        # ── POST /calibrate — Light panels, show color picker ─────────────
        @server.route("/calibrate", "POST")
        def route_calibrate(request):
            global _calib_state
            _calib_state["active"] = True
            try:
                import displayio
                display = matrixportal.display
                bmp = displayio.Bitmap(width, height, 3)
                pal = displayio.Palette(3)
                pal[0] = 0x330000  # dim red   — left panel
                pal[1] = 0x003300  # dim green — mid panel
                pal[2] = 0x000033  # dim blue  — right panel
                panel_w = width // 3
                for y in range(height):
                    for x in range(width):
                        bmp[x, y] = 0 if x < panel_w else (1 if x < panel_w * 2 else 2)
                tg = displayio.TileGrid(bmp, pixel_shader=pal)
                splash = displayio.Group()
                splash.append(tg)
                display.root_group = splash
                w.feed()
            except Exception as disp_err:
                print(f"Calibration display error: {disp_err}")
                log_exception(disp_err)

            rows = ""
            panels = [("Left","R","330000"),("Middle","G","003300"),("Right","B","000033")]
            for name, sent, swatch in panels:
                btns = ""
                for c, lbl, cls in [("R","Red","btn-red"),("G","Green","btn-green"),("B","Blue","btn-blue")]:
                    btns += (
                        "<button class=\"" + cls + " color-btn\" type=\"button\" "
                        "onclick=\"pick('" + name + "','" + c + "',this)\">" + lbl + "</button>"
                    )
                rows += (
                    "<td><div class=\"swatch\" style=\"background:#" + swatch + "\"></div>"
                    "<strong>" + name + " Panel</strong><br>"
                    "<small style=\"color:#888\">Sent: " + sent + "</small><br><br>"
                    + btns +
                    "<div id=\"sel_" + name + "\" style=\"margin-top:6px;color:#ffaa00;\"></div></td>"
                )

            body = (
                html_head("RGB Calibration") +
                "<body>" + html_nav("settings") +
                "<h1>&#x1F3A8; RGB Calibration</h1>" + html_meta() +
                "<div class=\"card\">"
                "<p style=\"color:#aaa\">Look at your display. Each third should be lit a dim color. "
                "Click the color you <strong>actually see</strong> for each panel.</p>"
                "<form method=\"POST\" action=\"/calibrate-result\" id=\"calform\">"
                "<input type=\"hidden\" name=\"left\" id=\"v_Left\">"
                "<input type=\"hidden\" name=\"mid\" id=\"v_Middle\">"
                "<input type=\"hidden\" name=\"right\" id=\"v_Right\">"
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
                "if(picks['Left']&&picks['Middle']&&picks['Right']){"
                "document.getElementById('applybtn').disabled=false;}}"
                "</script>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", body=body)

        # ── POST /calibrate-result — Derive and save color order ──────────
        @server.route("/calibrate-result", "POST")
        def route_calibrate_result(request):
            global _calib_state
            _calib_state["active"] = False
            try:
                matrixportal.display.root_group = matrixportal.splash
            except Exception:
                pass
            try:
                body_str = request.body.decode("utf-8") if request.body else ""
                params = {}
                for part in body_str.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k.strip()] = v.strip().upper()
                left  = params.get("left",  "R")
                mid   = params.get("mid",   "G")
                right = params.get("right", "B")
                # Board sent Left=R, Mid=G, Right=B.
                # User reports what they see — that IS the color_order string.
                order_str = left + mid + right
                if order_str not in VALID_COLOR_ORDERS:
                    raise ValueError("Invalid order: " + order_str)
                settings["color_order"] = order_str
                ok = save_settings(settings)
                print(f"Calibration: L={left} M={mid} R={right} -> {order_str} saved={ok}")
                status = "Color order set to <strong>" + order_str + "</strong>. " + ("Saved! Reboot to apply." if ok else "Save failed.")
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
                "</form></div>"
                "</body></html>"
            )
            return Response(request, content_type="text/html", body=body)

        server.start("0.0.0.0", port=80)
        print(f"Web server active at http://{wifi.radio.ipv4_address}/")
    except Exception as e:
        print(f"Web server start failed: {e}")
        log_exception(e)
        HAS_HTTPSERVER = False

# --- Load Preferred Sign List ---
favsign_list = []
print("Attempting to load 'sign_list.txt'")
try:
    with open("sign_list.txt", "r") as f:
        for line in f:
            sys.stdout.write(".")
            cleaned = line.strip()
            if cleaned:
                favsign_list.append(cleaned)
    print(f"\nLoaded {len(favsign_list)} entries from sign_list.txt.")
except OSError as e:
    print(f"\nCould not load sign_list.txt: {e}")

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

        response = requests.get(DATA_SOURCE_URL, timeout=15)
        w.feed()

        poll_server()  # Service web requests after API fetch

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
                    safe_delay(3)

                    # Display sign message in road-sign yellow
                    centered_msg = center_multiline_string(
                        clean_string(match["msg"]).replace("\\n", "\n"),
                        characters_per_line)
                    print(f"Message display:\n{centered_msg}")
                    matrixportal.set_text_color(sign_text_color, 0)
                    matrixportal.set_text(centered_msg, 0)
                    safe_delay(10)
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

    print("Cycle complete. Waiting 30s...")
    safe_delay(30)

