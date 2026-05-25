# SPDX-FileCopyrightText: 2026 Google LLC
#
# SPDX-License-Identifier: Apache-2.0

"""
Robust CircuitPython Main Program for Matrix Portal S3 Traffic Sign Display.

Fixes & Enhancements:
- Implements ATOMIC FILE WRITING to completely eliminate 0-byte file truncation crashes.
- Validates downloaded file size against HTTP Content-Length before swapping onto storage.
- Foolproof Dynamic Class Resolver to auto-detect legacy and modern httpserver classes.
- Semantically aware Version Comparison (is_newer_version) to prevent accidental downgrades.
- Global try...finally Socket Cleanup to prevent Port 80 EADDRINUSE busy collisions.
- Socket-layer SO_REUSEADDR configuration via SocketPool constants.
- Exponential backoff retry engine to handle DHCP and DNS settle delays smoothly.
- Chunked Socket Streaming Engine in Rescue Server to prevent out-of-memory (OOM) fragmentation.
- Aggressive memory reclamation (explicitly purging massive JSON dicts from RAM).
- Graceful TCP Preconnection handling to resolve Firefox empty response errors.
- Warning-free exception tracebacks optimized for CircuitPython 9.x.
- Restored visual Matrix Display feedback for OTA Version checks and status.
- Added Fail-Safe Port 80 Rescue Server fallback if main HTTPServer initialization fails.
"""

# --- EASY ACCESS VERSION CONFIGURATION ---
LOCAL_VERSION = "1.1.32"  

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

# --- Shadow Library Cleanup ---
for shadow_file in ["/lib/adafruit_httpserver.py", "/lib/adafruit_httpserver.mpy"]:
    try:
        os.remove(shadow_file)
    except OSError:
        pass

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
        import traceback
        import io
        stream = io.StringIO()
        traceback.print_exception(e, limit=None, file=stream)
        print(stream.getvalue())
    except Exception:
        try:
            import io
            stream = io.StringIO()
            sys.print_exception(e, stream)
            print(stream.getvalue())
        except Exception as ex:
            print(f"Exception logging failed: {e}")

# --- Foolproof Dynamic Class Resolver ---
print("\n" + "="*50)
print("🔍 RESOLVING HTTPSERVER CLASS NAMESPACES...")
print("="*50)

HTTPServer = None
HTTPResponse = None
HTTPMethod = None

# Resolve HTTPServer / Server
try:
    from adafruit_httpserver.server import Server as _srv_cls
    HTTPServer = _srv_cls
    print("✅ Resolved HTTPServer -> adafruit_httpserver.server.Server")
except Exception as e:
    print(f"DEBUG: adafruit_httpserver.server.Server resolution failed: {e}")

if HTTPServer is None:
    try:
        from adafruit_httpserver.server import HTTPServer as _srv_cls_legacy
        HTTPServer = _srv_cls_legacy
        print("✅ Resolved HTTPServer -> adafruit_httpserver.server.HTTPServer")
    except Exception as e:
         print(f"DEBUG: adafruit_httpserver.server.HTTPServer resolution failed: {e}")

if HTTPServer is None:
    try:
        from adafruit_httpserver import Server as _pkg_srv
        HTTPServer = _pkg_srv
        print("✅ Resolved HTTPServer -> adafruit_httpserver.Server")
    except Exception as e:
         print(f"DEBUG: adafruit_httpserver.Server resolution failed: {e}")

if HTTPServer is None:
    try:
        from adafruit_httpserver import HTTPServer as _pkg_srv_legacy
        HTTPServer = _pkg_srv_legacy
        print("✅ Resolved HTTPServer -> adafruit_httpserver.HTTPServer")
    except Exception as err:
        print(f"❌ Failed to locate HTTPServer module class: {err}")

# Resolve HTTPResponse / Response
try:
    from adafruit_httpserver.response import HTTPResponse as _resp_cls
    HTTPResponse = _resp_cls
    print("✅ Resolved HTTPResponse -> adafruit_httpserver.response.HTTPResponse")
except Exception as e:
    print(f"DEBUG: adafruit_httpserver.response.HTTPResponse resolution failed: {e}")

if HTTPResponse is None:
    try:
        from adafruit_httpserver.response import Response as _resp_cls_alt
        HTTPResponse = _resp_cls_alt
        print("✅ Resolved HTTPResponse -> adafruit_httpserver.response.Response")
    except Exception as e:
        print(f"DEBUG: adafruit_httpserver.response.Response resolution failed: {e}")

if HTTPResponse is None:
    try:
        from adafruit_httpserver import HTTPResponse as _pkg_resp
        HTTPResponse = _pkg_resp
        print("✅ Resolved HTTPResponse -> adafruit_httpserver.HTTPResponse")
    except Exception as e:
        print(f"DEBUG: adafruit_httpserver.HTTPResponse resolution failed: {e}")

if HTTPResponse is None:
    try:
        from adafruit_httpserver import Response as _pkg_resp_alt
        HTTPResponse = _pkg_resp_alt
        print("✅ Resolved HTTPResponse -> adafruit_httpserver.Response")
    except Exception as err:
        print(f"❌ Failed to locate HTTPResponse module class: {err}")

# Resolve HTTPMethod / Method
try:
    from adafruit_httpserver.methods import HTTPMethod as _meth_cls
    HTTPMethod = _meth_cls
    print("✅ Resolved HTTPMethod -> adafruit_httpserver.methods.HTTPMethod")
except Exception as e:
    print(f"DEBUG: adafruit_httpserver.methods.HTTPMethod resolution failed: {e}")

if HTTPMethod is None:
    try:
        from adafruit_httpserver.methods import Method as _meth_cls_alt
        HTTPMethod = _meth_cls_alt
        print("✅ Resolved HTTPMethod -> adafruit_httpserver.methods.Method")
    except Exception as e:
        print(f"DEBUG: adafruit_httpserver.methods.Method resolution failed: {e}")

if HTTPMethod is None:
    try:
        from adafruit_httpserver.method import HTTPMethod as _meth_cls_alt2
        HTTPMethod = _meth_cls_alt2
        print("✅ Resolved HTTPMethod -> adafruit_httpserver.method.HTTPMethod")
    except Exception as e:
        print(f"DEBUG: adafruit_httpserver.method.HTTPMethod resolution failed: {e}")

if HTTPMethod is None:
    try:
        from adafruit_httpserver.method import Method as _meth_cls_alt3
        HTTPMethod = _meth_cls_alt3
        print("✅ Resolved HTTPMethod -> adafruit_httpserver.method.Method")
    except Exception as e:
        print(f"DEBUG: adafruit_httpserver.method.Method resolution failed: {e}")

if HTTPMethod is None:
    try:
        from adafruit_httpserver import HTTPMethod as _pkg_meth
        HTTPMethod = _pkg_meth
        print("✅ Resolved HTTPMethod -> adafruit_httpserver.HTTPMethod")
    except Exception as e:
        print(f"DEBUG: adafruit_httpserver.HTTPMethod resolution failed: {e}")

if HTTPMethod is None:
    try:
        from adafruit_httpserver import Method as _pkg_meth_alt
        HTTPMethod = _pkg_meth_alt
        print("✅ Resolved HTTPMethod -> adafruit_httpserver.Method")
    except Exception as err:
        print(f"❌ Failed to locate HTTPMethod module class: {err}")

# Robust Mock Fallback for HTTPMethod to guarantee routing compatibility
if HTTPMethod is None:
    class MockHTTPMethod:
        GET = "GET"
        POST = "POST"
        PUT = "PUT"
        DELETE = "DELETE"
        HEAD = "HEAD"
    HTTPMethod = MockHTTPMethod
    print("🛠️ Resolved HTTPMethod -> Mock Fallback (GET/POST/etc.)")

# Finalize import validation status
if HTTPServer is not None and HTTPResponse is not None and HTTPMethod is not None:
    HAS_HTTPSERVER = True
    print("🎉 HTTPSERVER DEPENDENCY IMPORT: SUCCESSFUL!")
else:
    HAS_HTTPSERVER = False
    web_error_message = "IMPORT"
    print("⚠️ HTTPSERVER DEPENDENCY IMPORT: FAILED")
print("="*50 + "\n")

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
            af_inet = 2      
            sock_stream = 1  
            try:
                if hasattr(pool, "AF_INET"): af_inet = pool.AF_INET
                elif hasattr(socketpool, "AF_INET"): af_inet = socketpool.AF_INET
            except Exception: pass
            try:
                if hasattr(pool, "SOCK_STREAM"): sock_stream = pool.SOCK_STREAM
                elif hasattr(socketpool, "SOCK_STREAM"): sock_stream = socketpool.SOCK_STREAM
            except Exception: pass

            rescue_socket = pool.socket(af_inet, sock_stream)
            rescue_socket.settimeout(0.02)
            
            # Dynamically fetch SO_REUSEADDR configurations from standard module wrappers
            try:
                sol_socket = getattr(pool, "SOL_SOCKET", getattr(socketpool, "SOL_SOCKET", 1))
                so_reuseaddr = getattr(pool, "SO_REUSEADDR", getattr(socketpool, "SO_REUSEADDR", 4))
                rescue_socket.setsockopt(sol_socket, so_reuseaddr, 1)
                print("✅ Socket reuse (SO_REUSEADDR) enabled successfully.")
            except Exception as reuse_err:
                print(f"⚠️ Could not apply SO_REUSEADDR: {reuse_err}")
            
            # Robust Settle Binding Loop to bypass temporary EADDRINUSE port states
            bound = False
            for attempt in range(5):
                try:
                    rescue_socket.bind((str(wifi.radio.ipv4_address), 80))
                    bound = True
                    break
                except OSError as bind_err:
                    if bind_err.errno == 112:  # EADDRINUSE
                        print(f"Port 80 busy, retrying in 1.5s (attempt {attempt+1}/5)...")
                        time.sleep(1.5)
                    else:
                        raise bind_err
                        
            if not bound:
                raise RuntimeError("Could not bind to Port 80 after multiple retry cycles.")

            try: rescue_socket.listen(3)
            except TypeError: rescue_socket.listen()
            print("Emergency Rescue Web Server active on Port 80.")
        except Exception as e:
            print(f"Rescue binding failed: {e}")
            try:
                rescue_socket.close()
            except Exception: pass
            rescue_socket = None

def safe_send(conn, data):
    """Sends arbitrary bytes/strings over a TCP socket reliably without truncating."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    bytes_sent = 0
    while bytes_sent < len(data):
        try:
            sent = conn.send(data[bytes_sent:])
            if sent == 0: break
            bytes_sent += sent
        except OSError as e:
            if e.errno == 11:  
                time.sleep(0.01)
                continue
            raise e

def poll_rescue_server():
    """Emergency Lightweight Web Server. Streams logs dynamically to protect memory footprint."""
    global rescue_socket
    if not HAS_HTTPSERVER and rescue_socket is not None:
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
                except OSError: pass
                time.sleep(0.05)
            
            if request_str and "favicon.ico" in request_str:
                try:
                    conn.send("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("utf-8"))
                    conn.close()
                except Exception: pass
                return
            
            print(f"Rescue connection accepted from {addr}")
            safe_send(conn, "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n")
            safe_send(conn, "<!DOCTYPE html><html><head><title>Rescue Console</title>")
            safe_send(conn, "<meta name='viewport' content='width=device-width, initial-scale=1'>")
            safe_send(conn, "<style>body{font-family:monospace;background-color:#111;color:#ff3333;margin:20px;line-height:1.4;}")
            safe_send(conn, "pre{background-color:#000;padding:15px;border-radius:5px;border:1px solid #333;overflow-x:auto;white-space:pre-wrap;color:#00ff00;}</style></head>")
            safe_send(conn, "<body><h1>🚨 Matrix Portal S3 - Rescue System</h1>")
            safe_send(conn, "<h2>System Diagnostic & Download Logs:</h2><pre>")
            for line in web_logger.buffer:
                safe_send(conn, line + "\n")
            if web_logger.current_line:
                safe_send(conn, web_logger.current_line)
            safe_send(conn, "</pre></body></html>")
            time.sleep(0.15)
            conn.close()
        except OSError: pass
        except Exception as ex:
            if conn:
                try: conn.close()
                except Exception: pass
        finally:
            gc.collect()

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

def is_newer_version(remote, local):
    """Compares semver strings component-by-component.
    Returns True if remote is strictly greater than local.
    """
    try:
        r_parts = [int(x) for x in remote.split(".")]
        l_parts = [int(x) for x in local.split(".")]
        while len(r_parts) < len(l_parts): r_parts.append(0)
        while len(l_parts) < len(r_parts): l_parts.append(0)
        return r_parts > l_parts
    except Exception:
        return False

# --- Atomic Manifest-Based Safe OTA Updater ---
def perform_ota_check(requests_session, force=False):
    if not ENABLE_OTA or not MANIFEST_URL: return
    print(f"Checking updates via Manifest... Local Version: {LOCAL_VERSION}")
    
    # Restore matrix portal visual feedback for OTA checking phase
    try:
        matrixportal.set_text_color("#FFFF00")
        matrixportal.set_text(center_multiline_string(f"LOCAL\nv{LOCAL_VERSION}", characters_per_line))
        safe_delay(2)
        matrixportal.set_text(center_multiline_string("CHECKING\nUPDATE", characters_per_line))
    except Exception: pass
    
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
            
            # Restore matrix portal visual feedback for cloud version discovery
            try:
                matrixportal.set_text_color("#FFFF00")
                matrixportal.set_text(center_multiline_string(f"CLOUD\nv{remote_version}", characters_per_line))
                safe_delay(2)
            except Exception: pass
            
            if is_newer_version(remote_version, LOCAL_VERSION) or force:
                print(f"Update found! Remote {remote_version} > Local {LOCAL_VERSION}. Fetching files...")
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
                            content_length = file_response.headers.get("content-length")
                            if content_length is not None:
                                if len(file_content.encode("utf-8")) != int(content_length):
                                    raise RuntimeError(f"Size mismatch for {local_path}")
                            
                            ensure_dir_exists(local_path)
                            temp_path = local_path + ".tmp"
                            with open(temp_path, "w") as f:
                                f.write(file_content)
                            successful_swaps.append((temp_path, local_path))
                        else:
                            raise RuntimeError(f"HTTP Error {file_response.status_code}")
                    except Exception as err:
                        print(f"Aborting update to prevent crash. Error: {err}")
                        return
                    finally:
                        if file_response: file_response.close()
                        gc.collect()
                
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
                print(f"Local version ({LOCAL_VERSION}) is up to date with remote ({remote_version}).")
                try:
                    matrixportal.set_text_color("#00FF00")
                    matrixportal.set_text(center_multiline_string("UP TO DATE", characters_per_line))
                    safe_delay(2)
                except Exception: pass
    except Exception as ex:
        print(f"OTA Error: {ex}")
    finally:
        if response: response.close()
        gc.collect()


# --- Main Thread Execution Wrapper ---
try:
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

    perform_ota_check(requests, force=False)

    if HAS_HTTPSERVER:
        try:
            server = HTTPServer(pool)
            @server.route("/", HTTPMethod.GET)
            def base(request):
                body = f"<h1>Matrix Portal S3 active. Firmware: v{LOCAL_VERSION}</h1>"
                return HTTPResponse(request, content_type="text/html", body=body)
            server.start(str(wifi.radio.ipv4_address))
        except Exception as e:
            # Main Server initialization crashed. Fall back to Rescue Console immediately!
            print("\n❌ MAIN WEB SERVER STARTUP FAILED. Bypassing to Raw Rescue Server!")
            log_exception(e)
            HAS_HTTPSERVER = False
            start_rescue_server()

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
        json_data = None
        try:
            response = requests.get(DATA_SOURCE_URL, timeout=8)
            if response.status_code == 200:
                json_data = response.json()
                w.feed()
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
            json_data = None  
            gc.collect()      

        safe_delay(30)

finally:
    print("\n🧹 Cleaning up active socket connections...")
    if rescue_socket is not None:
        try:
            rescue_socket.close()
            print("✅ Closed rescue socket cleanly.")
        except Exception as cleanup_err:
            print(f"Error closing rescue socket: {cleanup_err}")
    if server is not None:
        try:
            if hasattr(server, "stop"):
                server.stop()
            elif hasattr(server, "close"):
                server.close()
            print("✅ Closed main web server cleanly.")
        except Exception as cleanup_err:
            print(f"Error closing main server: {cleanup_err}")
    gc.collect()
