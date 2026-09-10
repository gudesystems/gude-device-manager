import json
import os
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Optional, List, Dict
import logging

log = logging.getLogger("webui")

"""
Web UI server for GUDE Device Manager.

Enhancements:
- Robust asset discovery for both source and PyInstaller one-file builds.
- Optional auto-open of the default browser.
"""

# Ensure repository root is on sys.path so we can import the entry module when running this file directly
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upload import run_processing_from_options, DeviceResult, save_device_to_config, merge_ini_file, generate_ini_export, overwrite_ini_hosts
from upload import load_cached_fw_infos, refresh_fw_infos


class State:
    running: bool = False
    results: List[DeviceResult] = []
    progress: dict = {}


class UiSessionMonitor:
    HEARTBEAT_TIMEOUT_SECS = 90.0
    SHUTDOWN_GRACE_SECS = 2.0
    WATCHDOG_INTERVAL_SECS = 0.5

    enabled: bool = False
    ever_connected: bool = False
    sessions: Dict[str, float] = {}
    shutdown_deadline: Optional[float] = None
    server: Optional[ThreadingHTTPServer] = None
    watcher_thread: Optional[threading.Thread] = None
    stop_event = threading.Event()
    lock = threading.Lock()

    @classmethod
    def configure(cls, server: ThreadingHTTPServer, enabled: bool) -> None:
        cls.stop()
        cls.enabled = enabled
        cls.ever_connected = False
        cls.sessions = {}
        cls.shutdown_deadline = None
        cls.server = server
        if not enabled:
            return

        cls.stop_event = threading.Event()
        cls.watcher_thread = threading.Thread(
            target=cls._watchdog_loop,
            name="webui-session-watchdog",
            daemon=True,
        )
        cls.watcher_thread.start()

    @classmethod
    def stop(cls) -> None:
        cls.stop_event.set()
        cls.enabled = False
        cls.shutdown_deadline = None
        cls.sessions = {}
        cls.server = None
        cls.watcher_thread = None

    @classmethod
    def open_session(cls, session_id: str) -> int:
        return cls._record_activity(session_id)

    @classmethod
    def ping_session(cls, session_id: str) -> int:
        return cls._record_activity(session_id)

    @classmethod
    def close_session(cls, session_id: str) -> int:
        if not cls.enabled or not session_id:
            return 0

        now = time.monotonic()
        with cls.lock:
            cls.sessions.pop(session_id, None)
            active_sessions = len(cls.sessions)
            if cls.ever_connected and active_sessions == 0:
                cls.shutdown_deadline = now + cls.SHUTDOWN_GRACE_SECS
        return active_sessions

    @classmethod
    def _record_activity(cls, session_id: str) -> int:
        if not cls.enabled or not session_id:
            return 0

        now = time.monotonic()
        with cls.lock:
            cls.sessions[session_id] = now
            cls.shutdown_deadline = None
            cls.ever_connected = True
            return len(cls.sessions)

    @classmethod
    def _watchdog_loop(cls) -> None:
        while not cls.stop_event.wait(cls.WATCHDOG_INTERVAL_SECS):
            shutdown_server = None
            now = time.monotonic()
            with cls.lock:
                stale_sessions = [
                    session_id
                    for session_id, last_seen in cls.sessions.items()
                    if (now - last_seen) > cls.HEARTBEAT_TIMEOUT_SECS
                ]
                for session_id in stale_sessions:
                    cls.sessions.pop(session_id, None)

                if cls.ever_connected and not cls.sessions and cls.shutdown_deadline is None:
                    cls.shutdown_deadline = now + cls.SHUTDOWN_GRACE_SECS

                if (
                    cls.enabled
                    and cls.server is not None
                    and cls.ever_connected
                    and not cls.sessions
                    and cls.shutdown_deadline is not None
                    and now >= cls.shutdown_deadline
                    and not State.running
                ):
                    shutdown_server = cls.server
                    cls.shutdown_deadline = None
                    cls.stop_event.set()

            if shutdown_server is not None:
                log.info("No active Web UI session remains. Shutting down backend.")
                shutdown_server.shutdown()
                return

def _base_dir() -> Path:
    """Return directory containing index.html/assets, both in src and frozen builds."""
    if getattr(sys, 'frozen', False):
        # In PyInstaller onefile, data files are extracted under _MEIPASS; we keep them under 'webui'
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)).joinpath('webui')
        if base.is_dir():
            return base
        # Fallback to _MEIPASS root
        return Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return Path(__file__).parent


# Assets/index resolved from base dir
BASE_DIR = _base_dir()
ASSETS_DIR = str(BASE_DIR.joinpath('assets'))


def _json_default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)


def _run_gbl_query_async(enable_gbl: bool = True):
    try:
        State.running = True
        State.progress = {}
        try:
            State.results = run_processing_from_options(
                gbl=enable_gbl,
                status=True,
                onlineupdate=True,
                upload_ini="upload.ini",
                version_ini="version.ini"
            )
        except KeyError as e:
            # Occurs if gbl=False and upload.ini has no hosts.
            # This is expected behavior for correct startup state (empty list).
            upload_ini_path = Path("upload.ini")
            if not upload_ini_path.is_file():
                log.warning("No upload.ini configuration provided (upload.ini not found). Starting with empty list.")
                log.warning("Use 'Find Devices' in GUI, or create upload.ini with hosts and/or 'gbl=search'.")
            else:
                log.warning("No devices configured in upload.ini and GBL disabled. Starting with empty list.")
                log.warning("Use 'Find Devices' in GUI, or enable 'gbl=search' in upload.ini.")
            log.debug(f"Initial device discovery skipped: {e}")
            State.results = []
    finally:
        State.running = False



def _run_update_selected_async(
    hosts: list[str],
    firmware_overrides: Optional[dict] = None,
    config_overrides: Optional[dict] = None,
    ssl_overrides: Optional[dict] = None,
    forcefw: bool = False,
    custom_firmware: Optional[dict] = None,
    host_settings: Optional[dict] = None,
):
    try:
        State.running = True
        State.progress = {}
        
        def on_progress(evt):
            # evt: { "ip": ..., "type": "progress"/"device_done", ... }
            typ = evt.get("type")
            if typ in ("progress", "device_done"):
                ip = evt.get("ip")
                if ip:
                    if ip not in State.progress:
                        State.progress[ip] = {}
                    State.progress[ip].update(evt)
                    if typ == "device_done":
                        State.progress[ip]["progress"] = 100

        # Build devices mapping like: { 'hosts': { 'ip1': 'host:port', 'ip2': 'host:port', ... } }
        devices = {'hosts': {}}
        for idx, h in enumerate(hosts, start=1):
            devices['hosts'][f'ip{idx}'] = str(h)

        State.results = run_processing_from_options(
            upload_ini="upload.ini",
            replace_hosts=True,
            version_ini="no_version.ini",
            onlineupdate=True,
            refresh_online_info=False,
            devices=devices,
            forcefw=bool(forcefw),
            status=False,
            gbl=False,
            device_concurrency=2,
            progress_cb=on_progress,

            firmware_config=firmware_overrides,  # Pass overrides to upload logic
            custom_firmware=custom_firmware,     # Pass per-device firmware actions
            host_settings=host_settings,
            custom_config=config_overrides,      # Pass config overrides
            custom_ssl=ssl_overrides             # Pass SSL overrides
        )
    finally:
        State.running = False


def _run_status_selected_async(hosts: list[str], host_settings: Optional[dict] = None):
    try:
        State.running = True
        State.progress = {}
        # Build devices mapping like: { 'hosts': { 'ip1': 'host:port', 'ip2': 'host:port', ... } }
        devices = {'hosts': {}}
        for idx, h in enumerate(hosts, start=1):
            devices['hosts'][f'ip{idx}'] = str(h)

        State.results = run_processing_from_options(
            upload_ini="upload.ini",
            replace_hosts=True,
            version_ini="no_version.ini",
            onlineupdate=True,
            devices=devices,
            forcefw=False,
            status=True,
            gbl=False,
            device_concurrency=5,
            host_settings=host_settings,
        )
    finally:
        State.running = False


class Handler(BaseHTTPRequestHandler):
    def _api_refresh_firmware_info(self):
        try:
            cfg = refresh_fw_infos()
            payload = {'ok': True, 'last_update': cfg.get('url', 'last_update')}
            code = 200
        except Exception as exc:
            log.warning("Firmware information refresh failed: %s", exc)
            payload = {'ok': False, 'error': 'Could not download or save firmware information. '
                       'Check your Internet connection and write permissions. '
                       'Previously saved information is retained; device discovery is still available.'}
            code = 502
        self._send(code, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def _send(self, code=200, headers=None):
        self.send_response(code)
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
        except (ValueError, TypeError):
            return {}

        raw = self.rfile.read(length) if length > 0 else b'{}'
        if not raw:
            return {}

        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def log_message(self, format, *args):
        # Suppress /api/devices logs by moving them to DEBUG level
        if args and args[0].startswith("GET /api/devices"):
            log.debug("%s %s" % (self.address_string(), format % args))
            return
        
        # Use standard logging for other requests
        log.info("%s %s" % (self.address_string(), format % args))

    def _api_firmware(self):
        fw_dir = Path('fw')
        files = []
        if fw_dir.is_dir():
            for f in fw_dir.glob('*.bin'):
                files.append({'name': f.name, 'size': f.stat().st_size})
        
        cfg = load_cached_fw_infos()
        payload = {'files': files, 'last_update': cfg.get('url', 'last_update', fallback=None)}
        data = json.dumps(payload, default=_json_default).encode('utf-8')
        self._send(200, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(data)

    def _api_upload_firmware(self):
        filename = self.headers.get('X-Filename')
        if not filename:
             self._send(400)
             self.wfile.write(b"Bad Request: X-Filename header missing")
             return

        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
             self._send(400)
             self.wfile.write(b"Bad Request: Invalid Content-Length")
             return

        if length <= 0:
            self._send(400)
            self.wfile.write(b"Bad Request: Empty body")
            return
            
        fn = os.path.basename(filename)
        save_path = Path('fw') / fn
        
        # Ensure fw dir exists
        Path('fw').mkdir(exist_ok=True)
        
        # Read the entire body directly
        try:
            with open(save_path, 'wb') as f:
                # Read in chunks to avoid memory issues with large files, though firmware is usually small
                remaining = length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    chunk = self.rfile.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    
            self._send(200, {"Content-Type": "application/json"})
            self.wfile.write(json.dumps({"filename": fn, "success": True}).encode('utf-8'))
        except Exception as e:
            log.error(f"Error saving upload: {e}")
            self._send(500)
            self.wfile.write(b"Internal Server Error saving file")

    def _api_config(self):
        cfg_dir = ROOT / 'config'
        files = []
        if cfg_dir.is_dir():
            for f in cfg_dir.glob('*'):
                if f.is_file(): # List all files in config dir
                    files.append({'name': f.name, 'size': f.stat().st_size})
        
        payload = {'files': files}
        data = json.dumps(payload, default=_json_default).encode('utf-8')
        self._send(200, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(data)

    def _api_upload_config(self):
        filename = self.headers.get('X-Filename')
        if not filename:
             self._send(400)
             self.wfile.write(b"Bad Request: X-Filename header missing")
             return

        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
             self._send(400)
             self.wfile.write(b"Bad Request: Invalid Content-Length")
             return

        if length <= 0:
            self._send(400)
            self.wfile.write(b"Bad Request: Empty body")
            return
            
        fn = os.path.basename(filename)
        save_path = ROOT / 'config' / fn
        
        # Ensure config dir exists
        (ROOT / 'config').mkdir(exist_ok=True)
        
        # Read the entire body directly
        try:
            with open(save_path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    chunk = self.rfile.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    
            self._send(200, {"Content-Type": "application/json"})
            self.wfile.write(json.dumps({"filename": fn, "success": True}).encode('utf-8'))
        except Exception as e:
            log.error(f"Error saving config upload: {e}")
            self._send(500)
            self.wfile.write(b"Internal Server Error saving file")

    def _api_ssl(self):
        ssl_dir = ROOT / 'ssl'
        files = []
        if ssl_dir.is_dir():
            for f in ssl_dir.glob('*'):
                if f.is_file(): # List all files in ssl dir
                    files.append({'name': f.name, 'size': f.stat().st_size})
        
        payload = {'files': files}
        data = json.dumps(payload, default=_json_default).encode('utf-8')
        self._send(200, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(data)

    def _api_upload_ssl(self):
        filename = self.headers.get('X-Filename')
        if not filename:
             self._send(400)
             self.wfile.write(b"Bad Request: X-Filename header missing")
             return

        try:
            length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
             self._send(400)
             self.wfile.write(b"Bad Request: Invalid Content-Length")
             return

        if length <= 0:
            self._send(400)
            self.wfile.write(b"Bad Request: Empty body")
            return
            
        fn = os.path.basename(filename)
        save_path = ROOT / 'ssl' / fn
        
        # Ensure ssl dir exists
        (ROOT / 'ssl').mkdir(exist_ok=True)
        
        # Read the entire body directly
        try:
            with open(save_path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    chunk = self.rfile.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    
            self._send(200, {"Content-Type": "application/json"})
            self.wfile.write(json.dumps({"filename": fn, "success": True}).encode('utf-8'))
        except Exception as e:
            log.error(f"Error saving ssl upload: {e}")
            self._send(500)
            self.wfile.write(b"Internal Server Error saving file")

    def _api_add_device(self):
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length > 0 else b'{}'
            body = json.loads(raw.decode('utf-8'))
            
            ip = body.get('ip')
            if not ip:
                self._send(400)
                self.wfile.write(b"Missing IP")
                return
            
            # Construct settings dict
            settings = {
                'ssl': body.get('ssl', '0'),
                'auth': body.get('auth', '0'),
                'username': body.get('username', ''),
                'password': body.get('password', '')
            }
            if 'port' in body and body['port']:
                settings['port'] = body['port']
                # Create unique section key for same host with different ports
                # If ip doesn't already have the port and port is provided
                if ':' not in ip:
                    ip = f"{ip}:{body['port']}"
                
            save_device_to_config(ip, settings)
            
            self._send(200, {"Content-Type": "application/json"})
            self.wfile.write(json.dumps({"success": True, "message": "Device added"}).encode('utf-8'))
        except Exception as e:
            log.error(f"Error adding device: {e}")
            self._send(500)
            self.wfile.write(b"Internal Error")

    def _api_import_ini(self):
        filename = self.headers.get('X-Filename')
        if not filename:
             self._send(400)
             return

        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0:
                self._send(400)
                return
                
            content = self.rfile.read(length).decode('utf-8', errors='ignore')
            merge_ini_file(content)
            
            self._send(200, {"Content-Type": "application/json"})
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
        except Exception as e:
            log.error(f"Error importing INI: {e}")
            self._send(500)
            self.wfile.write(b"Internal Error")

    def _api_export_ini(self):
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length > 0 else b'{}'
            body = json.loads(raw.decode('utf-8'))
            
            hosts = body.get('hosts', [])
            export_all = body.get('export_all', False)
            
            ini_content = generate_ini_export(selected_keys=hosts, export_all=export_all)
            
            self._send(200, {
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="upload.ini"'
            })
            self.wfile.write(ini_content.encode('utf-8'))
        except Exception as e:
            log.error(f"Error exporting INI: {e}")
            self._send(500)
            self.wfile.write(b"Internal Error")

            self._send(500)
            self.wfile.write(b"Internal Error")

    def _api_save_ini_selection(self):
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length > 0 else b'{}'
            body = json.loads(raw.decode('utf-8'))
            
            hosts = body.get('hosts', [])
            host_settings = body.get('host_settings', {})
            # 'hosts' should be a list of strings
            
            overwrite_ini_hosts(hosts, host_settings=host_settings)
            
            self._send(200, {"Content-Type": "application/json"})
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
        except Exception as e:
            log.error(f"Error saving INI selection: {e}")
            self._send(500)
            self.wfile.write(b"Internal Error")

    def _api_session(self, action: str):
        body = self._read_json_body()
        session_id = str(body.get('session_id', '')).strip()
        if not session_id:
            self._send(400, {"Content-Type": "application/json; charset=utf-8"})
            self.wfile.write(json.dumps({"success": False, "error": "session_id is required"}).encode('utf-8'))
            return

        if action == 'open':
            active_sessions = UiSessionMonitor.open_session(session_id)
        elif action == 'ping':
            active_sessions = UiSessionMonitor.ping_session(session_id)
        elif action == 'close':
            active_sessions = UiSessionMonitor.close_session(session_id)
        else:
            self._send(400, {"Content-Type": "application/json; charset=utf-8"})
            self.wfile.write(json.dumps({"success": False, "error": "invalid session action"}).encode('utf-8'))
            return

        self._send(200, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(json.dumps({
            "success": True,
            "active_sessions": active_sessions,
            "auto_shutdown_enabled": UiSessionMonitor.enabled,
        }).encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/' or path == '/index.html':
            return self._serve_index()
        if path.startswith('/assets/'):
            return self._serve_asset(path[len('/assets/'):])
        if path == '/api/devices':
            return self._api_devices()
        if path == '/api/run':
            return self._api_run()
        if path == '/api/firmware':
            return self._api_firmware()
        elif self.path == '/api/config':
            return self._api_config()
        elif self.path == '/api/ssl':
            return self._api_ssl()
        else:
            self._send(404, {"Content-Type": "text/plain; charset=utf-8"})
            self.wfile.write(b'Not Found')

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/firmware/refresh':
            return self._api_refresh_firmware_info()
        if path == '/api/update':
            return self._api_update()
        if path == '/api/run':
            return self._api_run()
        if path == '/api/session/open':
            return self._api_session('open')
        if path == '/api/session/ping':
            return self._api_session('ping')
        if path == '/api/session/close':
            return self._api_session('close')
        if path == '/api/upload_firmware':
            return self._api_upload_firmware()
        elif self.path == '/api/upload_config':
            return self._api_upload_config()
        elif self.path == '/api/upload_ssl':
            return self._api_upload_ssl()
        elif self.path == '/api/add_device':
            return self._api_add_device()
        elif self.path == '/api/import_ini':
            return self._api_import_ini()
        elif self.path == '/api/export_ini':
            return self._api_export_ini()
        elif self.path == '/api/save_ini_selection':
            return self._api_save_ini_selection()
        else:
             self._send(404, {"Content-Type": "text/plain; charset=utf-8"})
             self.wfile.write(b'Not Found')

    def _serve_index(self):
        index_path = str(BASE_DIR.joinpath('index.html'))
        try:
            with open(index_path, 'rb') as f:
                data = f.read()
            self._send(200, {"Content-Type": "text/html; charset=utf-8"})
            self.wfile.write(data)
        except FileNotFoundError:
            self._send(500, {"Content-Type": "text/plain; charset=utf-8"})
            self.wfile.write(b'index.html not found')

    def _serve_asset(self, name: str):
        if not ASSETS_DIR:
            self._send(404, {"Content-Type": "text/plain; charset=utf-8"})
            self.wfile.write(b'Assets directory not found')
            return
        safe_name = os.path.basename(name)
        path = os.path.join(ASSETS_DIR, safe_name)
        # Allow serving of a project-root logo if requested
        if not os.path.isfile(path):
            if safe_name == 'gude_only-logo.svg':
                alt = os.path.join(str(ROOT), 'gude_only-logo.svg')
                if os.path.isfile(alt):
                    path = alt
                else:
                    self._send(404, {"Content-Type": "text/plain; charset=utf-8"})
                    self.wfile.write(b'Asset not found')
                    return
            else:
                self._send(404, {"Content-Type": "text/plain; charset=utf-8"})
                self.wfile.write(b'Asset not found')
                return
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.svg': 'image/svg+xml',
        }.get(ext, 'application/octet-stream')
        with open(path, 'rb') as f:
            data = f.read()
        self._send(200, {"Content-Type": f"{ctype}; charset=utf-8" if ctype.startswith(('text/', 'application/javascript')) else ctype})
        self.wfile.write(data)

    def _api_devices(self):
        payload = {
            'running': State.running,
            'running': State.running,
            'results': [asdict(r) for r in State.results],
            'progress': State.progress,
        }
        data = json.dumps(payload, default=_json_default).encode('utf-8')
        self._send(200, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(data)

    def _api_run(self):
        if State.running:
            # Already running; report accepted
            payload = {'running': True}
            data = json.dumps(payload).encode('utf-8')
            self._send(200, {"Content-Type": "application/json; charset=utf-8"})
            self.wfile.write(data)
            return

        # Check for POST data (selected IPs)
        enable_gbl = True
        if self.command == 'POST':
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length > 0 else b'{}'
            try:
                body = json.loads(raw.decode('utf-8')) if raw else {}
            except Exception:
                body = {}
            
            enable_gbl = body.get('gbl', True)
            hosts: list[str] = []
            if isinstance(body.get('hosts'), list):
                hosts = [str(x) for x in body['hosts']]
            
            if hosts:
                State.running = True
                t = threading.Thread(target=_run_status_selected_async,
                                     args=(hosts, body.get('host_settings')), daemon=True)
                t.start()
                payload = {'running': True}
                data = json.dumps(payload).encode('utf-8')
                self._send(202, {"Content-Type": "application/json; charset=utf-8"})
                self.wfile.write(data)
                return

        # Default GET/POST-no-hosts behavior: GBL query
        t = threading.Thread(target=_run_gbl_query_async, args=(enable_gbl,), daemon=True)
        t.start()
        payload = {'running': True}
        data = json.dumps(payload).encode('utf-8')
        self._send(202, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(data)

    def _api_update(self):
        if State.running:
            payload = {'running': True}
            data = json.dumps(payload).encode('utf-8')
            self._send(200, {"Content-Type": "application/json; charset=utf-8"})
            self.wfile.write(data)
            return
        # Read JSON body
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            body = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            body = {}
        hosts: list[str] = []
        # Accept either {"hosts": [..]} or a nested devices mapping
        if isinstance(body.get('hosts'), list):
            hosts = [str(x) for x in body['hosts']]
        elif isinstance(body.get('devices'), dict):
            dvc = body['devices'].get('hosts') or {}
            if isinstance(dvc, dict):
                # devices.hosts is mapping ip1->"host:port"
                hosts = [str(v) for _, v in sorted(dvc.items())]
            elif isinstance(dvc, list):
                hosts = [str(x) for x in dvc]
        
        firmware_overrides = body.get('firmware_overrides')  # Optional dict
        custom_firmware = body.get('custom_firmware')        # Optional dict (ip -> "__no_update__")
        config_overrides = body.get('config_overrides')      # Optional dict
        ssl_overrides = body.get('ssl_overrides')            # Optional dict
        forcefw = bool(body.get('forcefw', False))

        t = threading.Thread(
            target=_run_update_selected_async,
            args=(hosts, firmware_overrides, config_overrides, ssl_overrides, forcefw, custom_firmware,
                  body.get('host_settings')),
            daemon=True,
        )
        t.start()
        payload = {'running': True}
        data = json.dumps(payload).encode('utf-8')
        self._send(202, {"Content-Type": "application/json; charset=utf-8"})
        self.wfile.write(data)


def serve(host: str = '127.0.0.1', port: int = 8000, *, open_browser: bool = False):
    httpd = ThreadingHTTPServer((host, port), Handler)
    auto_shutdown_enabled = open_browser and host in ('127.0.0.1', 'localhost')
    UiSessionMonitor.configure(httpd, enabled=auto_shutdown_enabled)
    url = f"http://localhost:{port}"
    print(f"Web UI available at http://{host}:{port}")
    if auto_shutdown_enabled:
        print("Backend auto-exit is enabled when all Web UI tabs are closed.")
    # Open the default browser if requested
    if open_browser:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            # Non-fatal: continue serving even if browser could not be opened
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        UiSessionMonitor.stop()
        httpd.server_close()


if __name__ == '__main__':
    serve(open_browser=True)
