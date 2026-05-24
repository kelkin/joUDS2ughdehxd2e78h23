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
- Prints exact tracebacks on import errors to help debug missing library dependencies.
- Writes startup tracebacks to boot_error.txt to allow offline USB debugging.
- Integrated Local Web Configuration Server (runs if libraries are present).
- Integrated an Emergency Raw Socket Rescue Server (runs if adafruit_httpserver fails)
  to serve boot_error.txt and live logs on Port 80 over Wi-Fi. Discards favicon requests.
- Uses a non-blocking fast-polling safe_delay function to ensure Port 80 is responsive.
- Displays visual Wi-Fi status, full IP address on line 1, connected SSID on line 2, and
  versions along with Web Server status on the matrix screen during bootup.
- Captures print statements into a sliding RAM buffer and serves a live console web page on `/logs`.
- Overrides the built-in print() function globally to avoid write-protected sys.stdout limits.
"""

# --- EASY ACCESS VERSION CONFIGURATION ---
# Put this at the very top of the file so you can easily update it when pushing new code to GitHub!
LOCAL_VERSION = "1.1.10"  

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
# Captures prints into a rolling text buffer in memory.
class WebLogger:
    def __init__(self, max_lines=60):
        self.buffer = []
        self.max_lines = max_lines
        self.current_line = ""

    def write(self, message):
        # Safe conversion of bytes/other types to clean string
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8")
            except Exception:
                message = str(message)
        elif not isinstance(message, str):
            message = str(message)
            
        # Parse the output into clean lines for our web stream
        parts = message.split("\n")
        if len(parts) == 1:
            self.current_line += parts[0]
        else:
            self.current_line += parts[0]
            self.buffer.append(self.current_line)
            for part in parts[1:-1]:
                self.buffer.append(part)
            self.current_line = parts[-1]
            
            # Prune old logs to keep RAM usage small
            while len(self.buffer) > self.max_lines:
                self.buffer.pop(0)

    def get_logs(self):
        all_lines = list(self.buffer)
        if self.current_line:
            all_lines.append(self.current_line)
        return "\n".join(all_lines)

# Instantiate the web logger
web_logger = WebLogger()

# --- Overriding Built-In Print globally for RAM Logging ---
# We preserve a reference to the native print function so we can still output to Thonny/serial
_original_print = print

def print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = sep.join(str(arg) for arg in args)
    
    # Save a copy inside our web logging buffer
    web_logger.write(message + end)
    
    # Send it out to the physical serial terminal (Thonny)
    _original_print(*args, **kwargs)

# --- Exception Logging Helper ---
# Formats traceback errors as a string so they can go through our custom print wrapper
def log_exception(e):
    try:
        import io
        stream = io.StringIO()
        sys.print_exception(e, stream)
        print(stream.getvalue())
    except Exception:
        print(f"Exception logging failed. Raw exception: {e}")

# --- Deep Library File Verification Inspector ---
# Scans files inside /lib/adafruit_httpserver to verify they aren't missing or empty (0 bytes)
def check_httpserver_files():
    expected_files = [
        "__init__.py", "authentication.py", "exceptions.py", "headers.py",
        "httpserver.py", "interfaces.py", "methods.py", "mime_types.py",
        "request.py", "response.py", "route.py", "server.py", "status.py"
    ]
    file_status = []
    folder_exists = False
    try:
        os.stat("/lib/adafruit_httpserver")
        folder_exists = True
        file_status.append("📂 /lib/adafruit_httpserver: Directory exists")
    except OSError:
        file_status.append("❌ /lib/adafruit_httpserver: Directory MISSING!")
        
    if folder_exists:
        for filename in expected_files:
            filepath = f"/lib/adafruit_httpserver/{filename}"
            try:
                stats = os.stat(filepath)
                size = stats[6]  # File size in bytes
                if size == 0:
                    file_status.append(f"⚠️  {filename}: EMPTY (0 bytes)")
                else:
                    file_status.append(f"✅  {filename}: OK ({size} bytes)")
            except OSError:
                file_status.append(f"❌  {filename}: MISSING")
                
    # Check general core library dependencies
    for dep in ["adafruit_logging.py", "adafruit_ticks.py"]:
        try:
            stats = os.stat(f"/lib/{dep}")
            file_status.append(f"✅  {dep}: OK ({stats[6]} bytes)")
        except OSError:
            file_status.append(f"❌  {dep}: MISSING")
            
    return file_status

print("Wireless stdout logging activated.")

# --- Defensive Imports for Web Server Bootstrap ---
web_error_message = ""
try:
    from adafruit_httpserver import HTTPServer, HTTPResponse, HTTPMethod
    HAS_HTTPSERVER = True
    print("Web server libraries loaded successfully.")
except Exception as e:
    HAS_HTTPSERVER = False
    print("\n" + "="*60)
    print("WARNING: adafruit_httpserver library failed to load.")
    print(f"Error detail: {e}")
    print("Please inspect the traceback below for details:")
    print("="*60)
    log_exception(e)
    print("="*60 + "\n")
    
    # 1. Write traceback & file checklist to boot_error.txt for offline USB debugging
    try:
        with open("boot_error.txt", "w") as f:
            import io
            sys.print_exception(e, f)
            f.write("\n" + "="*50 + "\n")
            f.write("Local Library Verification Checklist:\n")
            f.write("="*50 + "\n")
            for status in check_httpserver_files():
                f.write(status + "\n")
    except Exception as write_err:
        print(f"Failed to write boot_error.txt: {write_err}")
    
    # 2. Extract clean error message to show on Matrix display
    err_str = str(e)
    if "no module named" in err_str.lower():
        parts = err_str.split("'")
        missing_module = parts[1] if len(parts) > 1 else "library"
        missing_module = missing_module.replace("adafruit_", "")
        web_error_message = missing_module.upper()
    else:
        web_error_message = "ERROR"

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

# Globals for server instances
server = None
rescue_socket = None

def start_rescue_server():
    """Initializes the raw socket Emergency Rescue Server on Port 80."""
    global rescue_socket, pool
    if rescue_socket is None:
        try:
            rescue_socket = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
            rescue_socket.settimeout(0.05)  # Short non-blocking timeout
            
            # Request socket port reuse (helps prevent "Address already in use" errors)
            try:
                import socket
                rescue_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass
                
            rescue_socket.bind((str(wifi.radio.ipv4_address), 80))
            rescue_socket.listen(3)  # Increase backlog to handle parallel browser threads
            print("Emergency Rescue Web Server initialized on Port 80.")
        except Exception as e:
            print(f"Could not bind Emergency Rescue Server: {e}")

def poll_rescue_server():
    """Emergency Lightweight Web Server.
    Served via raw sockets so it still runs even if adafruit_httpserver dependencies are broken!
    Optimized to handle multiple browser connections and reject favicon probes cleanly.
    """
    global rescue_socket
    if not HAS_HTTPSERVER and rescue_socket is not None:
        conn = None
        try:
            conn, addr = rescue_socket.accept()
            conn.settimeout(0.5)
            
            # Read incoming request headers safely
            request_str = ""
            try:
                request = conn.recv(512)
                if request:
                    request_str = request.decode("utf-8")
            except Exception:
                pass
            
            # Fast-path rejection of favicon.ico or non-root paths to prevent port congestion
            if "favicon.ico" in request_str or (request_str and "GET / " not in request_str and "GET /logs" not in request_str):
                try:
                    conn.send("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("utf-8"))
                    conn.close()
                except Exception:
                    pass
                return
            
            print(f"Rescue connection accepted from {addr}")
            
            # Retrieve traceback and file verification checklist from file
            try:
                with open("boot_error.txt", "r") as f:
                    err_content = f.read()
            except Exception:
                err_content = "No boot_error.txt found on flash memory."
                
            log_content = web_logger.get_logs()
            
            # Construct a highly optimized diagnostic response
            body = f"""<!DOCTYPE html>
            <html>
            <head>
                <title>Emergency Rescue Console</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: monospace; background-color: #111; color: #ff3333; margin: 20px; line-height: 1.4; font-size: 13px; }}
                    h1 {{ color: #ffcc00; font-family: Arial, sans-serif; font-size: 20px; border-bottom: 2px solid #ffcc00; padding-bottom: 5px; }}
                    pre {{ background-color: #000; padding: 15px; border-radius: 5px; border: 1px solid #333; overflow-x: auto; white-space: pre-wrap; color: #ff5555; }}
                    .btn {{ display: inline-block; background-color: #00FF00; color: #000; font-weight: bold; padding: 10px 15px; border-radius: 5px; text-decoration: none; font-family: Arial, sans-serif; }}
                    .logs {{ color: #00ff00; border-color: #005500; }}
                    .tag {{ background-color: #ff3333; color: white; padding: 2px 6px; border-radius: 3px; font-weight: bold; }}
                </style>
            </head>
            <body>
                <h1>🚨 S3 Matrix Portal - Emergency Rescue Console</h1>
                <p style="color:#aaa;">The main web server library failed to load, so the system is running in <span class="tag">Rescue Mode</span>.</p>
                
                <h2>1. Startup Diagnostic Checklist & Error Traceback:</h2>
                <pre>{err_content}</pre>
                
                <h2>2. Diagnostic Console & Download Logs:</h2>
                <pre class="logs">{log_content}</pre>
                
                <p><a href="#" onclick="window.location.reload();" class="btn">Refresh Diagnostics</a></p>
            </body>
            </html>
            """
            
            # Send standard HTTP response headers with accurate Content-Length
            response_bytes = body.encode("utf-8")
            headers = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(response_bytes)}\r\nConnection: close\r\n\r\n"
            
            try:
                conn.send(headers.encode("utf-8"))
                conn.send(response_bytes)
            except Exception as send_err:
                print(f"Error sending payload: {send_err}")
                
            conn.close()
        except OSError:
            pass
        except Exception as ex:
            print(f"Error handling rescue server: {ex}")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

def safe_delay(seconds):
    """Delays execution for specified seconds without blocking.
    Feeds the watchdog and polls either the main web server or the emergency rescue server rapidly.
    """
    start_time = time.monotonic()
    while time.monotonic() - start_time < seconds:
        w.feed()
        if HAS_HTTPSERVER and server is not None:
            try:
                server.poll()
            except Exception:
                pass
        else:
            poll_rescue_server()
        time.sleep(0.02)  # Prevent CPU stalling, poll every 20ms

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

# --- Initial Network Connection & System Initialization ---
gc.collect()
if connect_wifi():
    # Setup sockets immediately after connecting to ensure Emergency services work
    pool = socketpool.SocketPool(wifi.radio)
    ssl_context = ssl.create_default_context()
    requests = adafruit_requests.Session(pool, ssl_context)

    # Initialize Emergency Rescue Server if the standard libraries are missing/broken
    if not HAS_HTTPSERVER:
        start_rescue_server()

    # Format and show the IP Address on Line 1, and the SSID on Line 2
    try:
        ip_str = str(wifi.radio.ipv4_address)
        ssid_str = str(secrets.get("ssid", "WIFI"))
        ip_display = f"{ip_str}\n{ssid_str}"
        
        matrixportal.set_text_color("#00FF00")
        matrixportal.set_text(center_multiline_string(ip_display, characters_per_line))
        safe_delay(5)
    except Exception as display_err:
        print(f"Error displaying IP: {display_err}")

# Show web server import status on matrix
try:
    if HAS_HTTPSERVER:
        matrixportal.set_text_color("#00FF00")  # Green
        matrixportal.set_text(center_multiline_string("WEB OK\nPORT 80", characters_per_line))
    else:
        matrixportal.set_text_color("#FF0000")  # Red
        matrixportal.set_text(center_multiline_string(f"WEB ERR\n{web_error_message}", characters_per_line))
    safe_delay(3)
except Exception:
    pass

# --- GitHub Manifest-Based Multi-File OTA function ---
def perform_ota_check(requests_session, force=False):
    if not ENABLE_OTA or not MANIFEST_URL:
        print("OTA Updates are disabled or Manifest URL is not configured.")
        return

    print(f"Checking updates via Manifest... Local Version: {LOCAL_VERSION}")
    
    # 1. Visually display local version on the matrix screen
    matrixportal.set_text_color("#FFFF00")  # Yellow
    matrixportal.set_text(center_multiline_string(f"LOCAL\nv{LOCAL_VERSION}", characters_per_line))
    safe_delay(2)

    # 2. Show the standard checking updates text
    matrixportal.set_text_color("#FFFF00")  # Yellow
    matrixportal.set_text(center_multiline_string("CHECKING\nUPDATE", characters_per_line))
    
    response = None
    try:
        w.feed()
        # Non-blocking optimization: Reduce timeout to 5 seconds so the browser never hangs!
        response = requests_session.get(MANIFEST_URL, timeout=5)
        if response.status_code == 200:
            manifest_data = response.json()
            w.feed()
            
            remote_version = manifest_data.get("version", "0.0.0")
            files_to_download = manifest_data.get("files", {})
            
            print(f"Remote version found on GitHub: '{remote_version}'")
            
            # 3. Visually display remote version on the matrix screen
            matrixportal.set_text_color("#FFFF00")  # Yellow
            matrixportal.set_text(center_multiline_string(f"CLOUD\nv{remote_version}", characters_per_line))
            safe_delay(2)
            
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
                        # Non-blocking optimization: Timeout reduced to 5 seconds per file download
                        file_response = requests_session.get(remote_url, timeout=5)
                        if file_response.status_code == 200:
                            file_content = file_response.text
                            w.feed()
                            
                            # Content-length validation to protect against file truncation
                            content_length = file_response.headers.get("content-length")
                            if content_length is not None:
                                try:
                                    expected_size = int(content_length)
                                    actual_size = len(file_content.encode("utf-8"))
                                    if actual_size != expected_size:
                                        print(f"Warning: Size mismatch for {local_path}. Expected {expected_size} bytes, got {actual_size}")
                                except Exception as len_err:
                                    print(f"Content-Length check failed: {len_err}")
                            
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
                # 4. Visual confirmation that everything is matched and current (MODIFIED TO SINGLE LINE)
                matrixportal.set_text_color("#00FF00")  # Green
                matrixportal.set_text(center_multiline_string("UP TO DATE", characters_per_line))
                safe_delay(2)
        else:
            print(f"Failed to fetch remote manifest: HTTP {response.status_code}")
            try:
                matrixportal.set_text_color("#FF0000")  # Red
                matrixportal.set_text(center_multiline_string(f"HTTP ERR\n{response.status_code}", characters_per_line))
                safe_delay(3)
            except Exception:
                pass
    except Exception as ex:
        print(f"Error during Manifest OTA check: {ex}")
        log_exception(ex)
        
        # Write traceback and deep folder verification to boot_error.txt
        try:
            with open("boot_error.txt", "w") as f:
                import io
                sys.print_exception(ex, f)
                f.write("\n" + "="*50 + "\n")
                f.write("Local Library Verification Checklist:\n")
                f.write("="*50 + "\n")
                for status in check_httpserver_files():
                    f.write(status + "\n")
        except Exception as write_err:
            print(f"Failed to write boot_error.txt: {write_err}")
            
        try:
            # Safely extract exception name without type(ex).__name__ which fails in CircuitPython
            ex_str = repr(ex)
            err_name = ex_str.split('(')[0] if '(' in ex_str else ex_str.split(' ')[0]
            err_name = err_name.replace("<class '", "").replace("'>", "").replace("Error", "")
            if not err_name:
                err_name = "ERR"
            
            matrixportal.set_text_color("#FF0000")  # Red
            matrixportal.set_text(center_multiline_string(f"OTA ERR\n{err_name[:10]}", characters_per_line))
            safe_delay(3)
        except Exception as display_err:
            print(f"Failed to show OTA error on screen: {display_err}")
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        gc.collect()

# Run the update check at bootup
perform_ota_check(requests, force=False)

# --- Initialize Web Server only if library is present ---
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
                .btn {{ display: block; width: 100%; padding: 12px; margin: 15px 0; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; text-decoration: none; font-size: 16px; box-sizing: border-box; }}
                .btn-update {{ background-color: #00FF00; color: #000; }}
                .btn-update:hover {{ background-color: #00CC00; }}
                .btn-logs {{ background-color: #008CBA; color: #fff; }}
                .btn-logs:hover {{ background-color: #007399; }}
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
                <a href="/logs" class="btn btn-logs">View System Logs</a>
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

        @server.route("/logs", HTTPMethod.GET)
        def logs(request):
            """Serves the live system print/traceback logs."""
            log_content = web_logger.get_logs()
            
            # Formulating an auto-scrolling terminal-style viewport for log diagnostic
            html_logs = """<!DOCTYPE html>
            <html>
            <head>
                <title>Matrix Portal Logs</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body { font-family: monospace; background-color: #111; color: #0f0; margin: 20px; font-size: 13px; line-height: 1.4; }
                    h1 { color: #F7B500; font-family: Arial, sans-serif; font-size: 18px; margin-bottom: 10px; }
                    .nav { margin-bottom: 20px; }
                    .nav a { color: #00FF00; text-decoration: none; font-family: Arial, sans-serif; font-size: 14px; font-weight: bold; border: 1px solid #00FF00; padding: 5px 10px; border-radius: 4px; }
                    .nav a:hover { background-color: rgba(0, 255, 0, 0.2); }
                    pre { background-color: #000; padding: 15px; border-radius: 5px; border: 1px solid #333; overflow-x: auto; white-space: pre-wrap; max-height: 70vh; }
                </style>
                <script>
                    window.onload = function() {
                        var pre = document.getElementById("log-box");
                        pre.scrollTop = pre.scrollHeight;
                        setTimeout(function() {
                            window.location.reload();
                        }, 5000);
                    };
                </script>
            </head>
            <body>
                <div class="nav">
                    <a href="/">← Back to Dashboard</a>
                </div>
                <h1>System Diagnostic Logs</h1>
                <pre id="log-box">""" + log_content + """</pre>
                <p style="color: #666; font-family: Arial, sans-serif; font-size: 11px;">Page auto-refreshes every 5 seconds. Current free RAM: """ + str(gc.mem_free()) + """ bytes</p>
            </body>
            </html>
            """
            return HTTPResponse(request, content_type="text/html", body=html_logs)

        print("Starting local configuration server...")
        server.start(str(wifi.radio.ipv4_address))
    except Exception as server_init_err:
        print(f"Failed to start local server: {server_init_err}")
        HAS_HTTPSERVER = False
        start_rescue_server()  # Fallback: instantly bind Emergency socket if start fails!

w.feed()

# --- Load Preferred Sign List (ONCE at Startup) ---
filename = "sign_list.txt"
favsign_list = []
print(f"Attempting to load '{filename}'")

try:
    with open(filename, "r") as f:
        for line in f:
            print('.', end='')
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
            safe_delay(10)
            continue

    # 2. Check if a web client pressed the force update button
    if force_ota_triggered:
        force_ota_triggered = False
        perform_ota_check(requests, force=True)

    # 3. Fetch data from 511NY API
    print(f"Fetching data from API...")
    response = None
    try:
        # Non-blocking optimization: Timeout reduced to 5 seconds so the browser connection doesn't drop!
        response = requests.get(DATA_SOURCE_URL, timeout=5)
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
                    
                    for sign_data in api_signs:
                        if fav_name == sign_data['name']:
                            print(f"\nMATCH FOUND: {fav_name}")
                            
                            raw_name = sign_data['name']
                            clean_name = clean_string(raw_name)
                            centered_name = center_multiline_string(clean_name, characters_per_line)
                            
                            print(f"Displaying Name:\n{centered_name}")
                            matrixportal.set_text_color("#0000FF")
                            matrixportal.set_text(centered_name)
                            
                            # Hold name on screen (feeds WDT and processes web requests in the background)
                            safe_delay(3)

                            raw_msg = sign_data['messages']
                            clean_msg = clean_string(raw_msg)
                            clean_msg = clean_msg.replace('\\n', '\n')
                            centered_msg = center_multiline_string(clean_msg, characters_per_line)
                            
                            print(f"Displaying Message:\n{centered_msg}")
                            matrixportal.set_text_color(sign_text_color)
                            matrixportal.set_text(centered_msg)
                            
                            # Hold message on screen for 10 seconds
                            safe_delay(10)

            else:
                print(f"Error: API returned unexpected structure: {type(json_data)}")
        else:
            print(f"API Error: Status code {response.status_code}")

    except Exception as e:
        print(f"An error occurred during API fetch or display cycle: {e}")
        log_exception(e)

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

    # 5. Non-blocking sleep interval while polling web server rapidly
    print("Cycle completed. Sleeping and listening for web connections...")
    safe_delay(30)
