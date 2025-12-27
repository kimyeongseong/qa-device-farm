from fastapi import FastAPI, WebSocket, Request, File, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from adbutils import adb
import os
import sys
import asyncio
import json
import shutil
import time
import re
import hmac
from pydantic import BaseModel

# This server echoes device output — logcat lines, app names, adb errors — and
# those routinely carry characters the console codepage cannot encode (emoji on
# a cp949 Windows console). An unencodable print raises UnicodeEncodeError from
# whatever request or background task happened to log it, which took down the
# audio endpoint and would take down the logcat pump. Make the streams lossy.
#
# line_buffering matters just as much: redirected to a file or a service log,
# stdout is block-buffered, so progress lines from long-running work (macro
# replay, logcat) only surface once the buffer fills. Diagnosing a farm from its
# log is the normal case here, so flush per line.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass  # Not a reconfigurable stream (redirected/wrapped); nothing to do.

app = FastAPI(
    title="QA Device Farm",
    description="Self-hosted Android device farm: shared real devices over one HTTP/WebSocket API.",
    version="1.1.0",
)

# --- Alias Storage ---
ALIAS_FILE = "device_aliases.json"
device_aliases = {}

def load_aliases():
    global device_aliases
    if os.path.exists(ALIAS_FILE):
        try:
            with open(ALIAS_FILE, "r", encoding="utf-8") as f:
                device_aliases = json.load(f)
        except:
            device_aliases = {}

def save_aliases():
    try:
        with open(ALIAS_FILE, "w", encoding="utf-8") as f:
            json.dump(device_aliases, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save aliases: {e}")

load_aliases()
# ---------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow the stream server's origin
    # Credentials must stay off while origins is "*": browsers reject that pair,
    # and the farm carries its token in a header, not a cookie.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Access token ---
# Anyone who can reach this port can drive the devices and install APKs on them.
# On a trusted lab network that is the point; the moment the farm is tunnelled
# out, it is not. Setting DEVICE_FARM_TOKEN turns on a shared-secret check.
#
# Off by default so a local checkout still just runs. When it is on, the server
# says so at startup rather than leaving it to be discovered.

FARM_TOKEN = os.environ.get("DEVICE_FARM_TOKEN", "").strip()

# Reachable without a token: the dashboard itself and its assets (the page then
# asks for the token), and the health probe, so monitoring does not need the
# secret to see that the farm is alive.
#
# Exact matches and prefixes are kept apart on purpose. "/" as a prefix matches
# every path there is, so folding it in with the rest quietly made the whole API
# public while still looking like it was guarded.
PUBLIC_EXACT = frozenset({"/", "/control", "/favicon.ico",
                          "/docs", "/redoc", "/openapi.json", "/api/health"})
PUBLIC_PREFIXES = ("/static/",)

def is_public_path(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)

def token_ok(request: Request) -> bool:
    supplied = (request.headers.get("x-farm-token")
                or request.query_params.get("token")
                or "")
    # Compared with hmac.compare_digest so a wrong token cannot be recovered by
    # timing the response.
    return hmac.compare_digest(supplied, FARM_TOKEN)

@app.middleware("http")
async def require_token(request: Request, call_next):
    if not FARM_TOKEN or request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if is_public_path(path):
        return await call_next(request)
    if not token_ok(request):
        return JSONResponse(
            {"status": "error",
             "message": "이 팜은 토큰이 필요합니다. X-Farm-Token 헤더에 토큰을 넣으세요."},
            status_code=401,
        )
    return await call_next(request)

# Mount Static Files (Frontend)
# Ensure 'static' directory exists
if not os.path.exists("static"):
    os.makedirs("static")
    
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_root():
    return FileResponse("static/index.html")

@app.get("/control")
async def control_page():
    return FileResponse("static/control.html")

# --- Stream server location ---
# The dashboard and the stream server (ws-scrcpy) are separate processes on
# separate ports, so the browser has to be told where the second one is. Both
# sides read the same file so the port is defined exactly once.

STREAM_CONFIG_FILE = "ws-scrcpy.config.json"
FALLBACK_STREAM_PORT = 8010

def get_stream_port() -> int:
    try:
        with open(STREAM_CONFIG_FILE, "r", encoding="utf-8") as f:
            for item in json.load(f).get("server", []):
                if not item.get("secure") and item.get("port"):
                    return int(item["port"])
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Could not read {STREAM_CONFIG_FILE}: {e}")
    return FALLBACK_STREAM_PORT

@app.get("/api/config")
async def get_config():
    """What the dashboard cannot work out from its own URL."""
    return {"stream_port": get_stream_port()}

def get_adb_path():
    """Locate the adb binary.

    A bundled copy wins so a host can pin a known version, then PATH -- which is
    what the setup instructions tell people to do, and what adbutils itself
    uses, so ignoring it made the dashboard look alive while every control
    action failed.
    """
    exe = "adb.exe" if os.name == "nt" else "adb"
    local_adb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrcpy_bin", exe)
    if os.path.exists(local_adb):
        return local_adb

    on_path = shutil.which("adb")
    if on_path:
        return on_path

    # Last resort: the default Windows install location. It may well not exist --
    # adb_binary_info() is what says so out loud.
    return "C:\\platform-tools\\adb.exe" if os.name == "nt" else "adb"

def adb_binary_info():
    """Whether the resolved adb binary is actually there.

    adbutils reaches the adb *server* over TCP 5037, so the device list, device
    details and screenshots all work with no adb binary on the machine at all.
    Everything that shells out -- input, app control, install, logcat, wireless,
    the screenrecord fallback -- fails with a bare "file not found". Reporting
    only the path let the farm look healthy while half the API was dead.
    """
    path = get_adb_path()
    found = shutil.which(path) or (os.path.exists(path) and path)
    info = {"path": path, "ok": bool(found)}
    if not found:
        info["note"] = ("adb binary not found. Device list and screenshots still work "
                        "(adbutils talks to the adb server directly), but input, app "
                        "control, install, logcat, wireless and the screenrecord "
                        "fallback all fail. Install Android platform-tools and put adb "
                        "on PATH, or drop a copy in scrcpy_bin/.")
    return info

import subprocess
from fastapi.responses import Response

# --- Concurrency Control ---
device_locks = {}

def get_device_lock(serial: str):
    if serial not in device_locks:
        device_locks[serial] = asyncio.Lock()
    return device_locks[serial]
# ---------------------------

# --- Device Leases ---
# The farm is shared, so callers announce which device they are driving before
# they drive it. Leases expire on their own: a CI job that dies mid-run must not
# park a device forever. Leases are advisory for the browser UI (which drives
# devices over the WebSocket) and enforced on the HTTP input API used by CI.

DEFAULT_LEASE_SECONDS = 600
LEASE_FILE = "device_leases.json"

device_leases = {}  # { serial: {"owner": str, "expires_at": float} }

# Android 11+ wireless debugging advertises the device over mDNS, so adb lists
# one phone twice: by its serial, and as
# "adb-<serial>-<suffix>._adb-tls-connect._tcp". Both transports work and both
# report the same ro.serialno and boot_id.
#
# Counting them as two devices broke the only guarantee this farm makes.
# Measured on a real tablet: ci-A claimed HA2F2NVC, then ci-B asked for "any
# free device" and was handed adb-HA2F2NVC-...._tcp -- two jobs driving one
# screen, which is what leases exist to prevent. Batch runs hit it twice and the
# device count was inflated too.
MDNS_ALIAS = re.compile(r"^adb-(?P<serial>.+)-[^-]+\._adb-tls-connect\._tcp$")

def mdns_base(serial: str):
    """The plain serial behind an mDNS transport name, or None."""
    m = MDNS_ALIAS.match(serial)
    return m.group("serial") if m else None

def lease_key(serial: str) -> str:
    """Leases belong to a physical device, not to an adb transport name.

    Commands still go out on whichever transport the caller named -- only the
    bookkeeping is collapsed, so claiming a device by either name locks both.
    """
    return mdns_base(serial) or serial

def drop_duplicate_transports(serials):
    """Hide an mDNS alias when its plain serial is present in the same listing.

    Kept when it is the only way to reach the device, which is the whole point
    of wireless debugging once the cable is out.
    """
    # mdns_base() is None for a plain serial, and None is never in `direct`.
    direct = {s for s in serials if not mdns_base(s)}
    return [s for s in serials if mdns_base(s) not in direct]

def save_leases():
    """Persist leases so a server restart does not silently free every device.

    Restarting the farm used to drop the whole table, which is exactly when it
    hurts: the CI job that holds a device is still running and has no idea the
    farm now considers that device free for someone else to grab.
    """
    try:
        with open(LEASE_FILE, "w", encoding="utf-8") as f:
            json.dump(device_leases, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save leases: {e}")

def load_leases():
    global device_leases
    try:
        with open(LEASE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"Could not read {LEASE_FILE}: {e}")
        return
    now = time.time()
    # Leases that expired while the server was down are simply gone; restoring
    # them would park devices nobody is using.
    device_leases = {
        serial: entry for serial, entry in saved.items()
        if isinstance(entry, dict) and entry.get("expires_at", 0) > now
    }
    dropped = len(saved) - len(device_leases)
    if device_leases or dropped:
        print(f"Restored {len(device_leases)} lease(s), dropped {dropped} expired")

load_leases()

def get_lease(serial: str):
    """Return the live lease for a device, dropping it if it has expired."""
    key = lease_key(serial)
    lease = device_leases.get(key)
    if lease and lease["expires_at"] <= time.time():
        del device_leases[key]
        save_leases()
        return None
    return lease

def lease_conflict(serial: str, owner):
    """Return a 409 response if someone else currently holds the device."""
    lease = get_lease(serial)
    if lease and lease["owner"] != owner:
        return JSONResponse(
            {
                "status": "error",
                "message": f"Device is held by '{lease['owner']}'",
                "owner": lease["owner"],
                "expires_at": lease["expires_at"],
            },
            status_code=409,
        )
    return None
# ---------------------

# --- Input Injection ---
# Coordinates arrive from the browser and from CI, so they are parsed as integers
# and handed to adb as separate argv entries. Never build a shell string here.

async def adb_exec(adb_path: str, serial: str, *args: str):
    proc = await asyncio.create_subprocess_exec(
        adb_path, "-s", serial, *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or f"adb exited {proc.returncode}")

def as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be an integer, got {value!r}")

async def dispatch_input(adb_path: str, serial: str, event: dict):
    """Turn one control event into a single `adb shell input` call."""
    etype = event.get("type")

    if etype == "tap":
        x, y = as_int(event.get("x"), "x"), as_int(event.get("y"), "y")
        await adb_exec(adb_path, serial, "shell", "input", "tap", str(x), str(y))

    elif etype == "swipe":
        x1, y1 = as_int(event.get("x1"), "x1"), as_int(event.get("y1"), "y1")
        x2, y2 = as_int(event.get("x2"), "x2"), as_int(event.get("y2"), "y2")
        duration = as_int(event.get("duration", 300), "duration")
        await adb_exec(adb_path, serial, "shell", "input", "swipe",
                       str(x1), str(y1), str(x2), str(y2), str(duration))

    elif etype == "key":
        keycode = as_int(event.get("keycode"), "keycode")
        await adb_exec(adb_path, serial, "shell", "input", "keyevent", str(keycode))

    elif etype == "text":
        # `input text` only carries ASCII. Korean and emoji need an IME on the
        # device; this endpoint does not pretend otherwise.
        text = str(event.get("text", ""))
        if not text.isascii():
            raise ValueError("'text' supports ASCII only; install an IME for other scripts")
        await adb_exec(adb_path, serial, "shell", "input", "text", text)

    else:
        raise ValueError(f"Unknown event type: {etype!r}")
# -----------------------

@app.get("/api/device/{serial}/screenshot")
async def get_device_screenshot(serial: str):
    try:
        lock = get_device_lock(serial)
        
        # Acquire lock to ensure only one screenshot per device at a time
        async with lock:
            loop = asyncio.get_event_loop()
            
            def capture_task():
                import io
                
                d = adb.device(serial=serial)
                
                # Retry logic for unstable connections
                img = None
                for attempt in range(2):
                    try:
                        img = d.screenshot()
                        break
                    except Exception as attempt_msg:
                         print(f"[{serial}] Screenshot attempt {attempt+1} failed: {attempt_msg}")
                         if attempt == 1: 
                             # If failure persists, return empty or error image instead of crashing server
                             return None
                
                if not img: return None

                # Resize optimization
                img.thumbnail((300, 600))
                
                # Convert to JPEG
                buf = io.BytesIO()
                try:
                    img.save(buf, format="JPEG", quality=60)
                except Exception as save_err:
                    print(f"[{serial}] Image Save Error: {save_err}")
                    return None
                    
                buf.seek(0)
                return buf.getvalue()

            # Run blocking ADB/Image code in thread pool
            img_bytes = await loop.run_in_executor(None, capture_task)
            
            if img_bytes is None:
                # Return a placeholder or 503 Service Unavailable (but 200 with error msg might be safer for frontend polling)
                # Let's return 404 or 503 so frontend knows to retry silently
                return Response(status_code=503)
            
            return Response(content=img_bytes, media_type="image/jpeg")

    except Exception as e:
        print(f"[{serial}] Screenshot Error: {e}")
        return Response(content=str(e), status_code=500)

def ws_token_ok(websocket: WebSocket) -> bool:
    """Token check for WebSockets.

    HTTP middleware never sees a websocket scope, so the video and control
    sockets have to check for themselves -- otherwise turning the token on
    would lock the REST API while leaving live device control wide open.
    Browsers cannot set headers on a WebSocket, so the query string is the
    supported route here.
    """
    if not FARM_TOKEN:
        return True
    supplied = (websocket.query_params.get("token")
                or websocket.headers.get("x-farm-token")
                or "")
    return hmac.compare_digest(supplied, FARM_TOKEN)

@app.websocket("/ws/video/{serial}")
async def websocket_video_endpoint(websocket: WebSocket, serial: str):
    if not ws_token_ok(websocket):
        await websocket.close(code=1008, reason="token required")
        return
    await websocket.accept()
    print(f"[{serial}] Video WS Connected")
    
    adb_path = get_adb_path()
    
    # Start adb screenrecord
    # output-format=h264 is simpler to stream, though raw bitstream requires parsing.
    # JMuxer expects raw H.264 NAL units.
    # Note: 'screenrecord' on some devices puts header info that might need stripping,
    # but let's try raw stream first.
    
    # Using exec-out for raw binary stream (Critical for Windows)
    # Added --bit-rate 4M to prevent bandwidth overload
    cmd = [adb_path, "-s", serial, "exec-out", "screenrecord", "--bit-rate", "4M", "--output-format=h264", "-"]
    print(f"Executing: {' '.join(cmd)}")
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    try:
        # Read chunks and send
        while True:
            data = await process.stdout.read(4096) # Read 4KB chunks
            if not data:
                break
            await websocket.send_bytes(data)
    except Exception as e:
        print(f"[{serial}] Stream Error: {e}")
    finally:
        try:
            process.terminate()
        except: pass
        print(f"[{serial}] Stream Closed")

@app.websocket("/ws/control/{serial}")
async def websocket_control_endpoint(websocket: WebSocket, serial: str):
    if not ws_token_ok(websocket):
        await websocket.close(code=1008, reason="token required")
        return
    await websocket.accept()
    print(f"[{serial}] Control WS Connected")
    adb_path = get_adb_path()
    
    try:
        while True:
            data = await websocket.receive_text()
            event = json.loads(data)

            # Simple ADB Input Injection
            # For high performance, we should keep a shell open or use 'minitouch',
            # but for MVP 'adb shell input' is sufficient.

            # --- Recording Logic ---
            if serial in active_recordings:
                event["timestamp"] = time.time()
                active_recordings[serial].append(event)
            # -----------------------

            try:
                await dispatch_input(adb_path, serial, event)
            except ValueError as bad_event:
                # A malformed event should not tear down the whole session.
                print(f"[{serial}] Ignored event: {bad_event}")

    except Exception as e:
        print(f"[{serial}] Control Error: {e}")
    finally:
        print(f"[{serial}] Control WS Closed")
        # Auto-stop audio if web page is closed
        if serial in active_audio_procs:
            print(f"[{serial}] Stopping Audio due to WS disconnect")
            proc = active_audio_procs[serial]
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except:
                proc.kill()
            del active_audio_procs[serial]

# A phone that is plugged in but not usable ('unauthorized' because nobody
# accepted the RSA prompt, 'offline' after a flaky cable) is invisible to
# adbutils' device_list(). Silently dropping it is the worst answer: the owner
# sees an empty slot and no reason why. adb.list() keeps the state.
STATE_LABELS = {
    "unauthorized": "USB 디버깅 승인 필요 (기기 화면의 팝업을 확인하세요)",
    "offline": "연결 불안정 (케이블/포트를 확인하세요)",
    "no permissions": "adb 권한 없음 (udev 규칙을 확인하세요)",
}

def list_device_states():
    """Every serial adb knows about, mapped to its state."""
    try:
        states = {info.serial: info.state for info in adb.list()}
    except Exception as e:
        print(f"adb.list() failed: {e}")
        return {}
    keep = set(drop_duplicate_transports(list(states)))
    return {s: st for s, st in states.items() if s in keep}

# --- Device detail cache ---
# The dashboard polls /api/devices every two seconds, and interrogating one
# device costs several adb round trips (props, `wm size`, `ip addr`,
# `dumpsys battery`). Measured on real hardware that is 0.8-2.6s for a *single*
# device -- already slower than the poll interval, and it grows with every
# device added, which is the wrong direction for a farm.
#
# Model, OS version and screen size cannot change while a device stays
# attached, so they are read once. Battery and IP change slowly enough to serve
# from a short-lived cache.

BATTERY_TTL = 20.0
IP_TTL = 60.0

device_cache = {}  # serial -> {"static": {...}, "battery": (value, at), "ip": (value, at)}

def read_static_detail(d):
    model = d.prop.get("ro.product.model", "Unknown")
    width, height = 0, 0
    try:
        res = d.shell("wm size")  # Physical size: 1080x2400
        if res and "Physical size:" in res:
            w, h = res.split(":")[-1].strip().split("x")
            width, height = int(w), int(h)
    except Exception:
        pass
    return {
        "model": model,
        "version": d.prop.get("ro.build.version.release", "?"),
        "sdk": d.prop.get("ro.build.version.sdk", "?"),
        "width": width,
        "height": height,
    }

def read_battery(d):
    try:
        out = d.shell("dumpsys battery | grep level")
        if out:
            return f"{int(out.split(chr(10))[0].split(':')[1].strip())}%"
    except Exception:
        pass
    return "Unknown"

def read_ip(d):
    """Address of a wireless device.

    An `adb connect` serial carries the address ('192.168.0.5:5555'); mDNS
    pairing gives a name instead ('adb-XXXX._adb-tls-connect._tcp'), so that
    case has to ask the device. The original code branched on "." appearing in
    the serial and called a method adbutils does not have, so every wireless
    device threw and was dropped from the list -- including any device this
    farm's own /api/wireless had just switched over.
    """
    host = d.serial.split(":")[0]
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        return host
    try:
        ips = d.shell("ip addr show wlan0 | grep 'inet '")
        if ips:
            return ips.split()[1].split("/")[0]
    except Exception:
        pass
    return "USB"

def device_detail(d, refresh=False):
    """Dashboard fields for one device, re-reading only what can have changed."""
    entry = device_cache.setdefault(d.serial, {})
    now = time.time()

    if refresh or "static" not in entry:
        entry["static"] = read_static_detail(d)

    battery, at = entry.get("battery", (None, 0.0))
    if refresh or battery is None or now - at > BATTERY_TTL:
        battery = read_battery(d)
        entry["battery"] = (battery, now)

    ip, at = entry.get("ip", (None, 0.0))
    if refresh or ip is None or now - at > IP_TTL:
        ip = read_ip(d)
        entry["ip"] = (ip, now)

    return {**entry["static"], "battery": battery, "ip": ip}

def forget_absent_devices(present):
    """Drop cache for devices that are no longer attached.

    Also stops the cache growing without bound as devices come and go, and
    guarantees a device that is replugged is re-read rather than showing the
    values it had last time.
    """
    for serial in [s for s in device_cache if s not in present]:
        del device_cache[serial]

@app.get("/api/devices")
async def get_devices(refresh: bool = False):
    """
    Returns a list of connected devices with details AND resolution.
    Devices that are attached but unusable are included with their state.

    Details come from a per-device cache so the dashboard's two-second poll
    does not re-interrogate every device over adb; pass `?refresh=1` to force
    a re-read.

    The adb work runs in a thread. adbutils is synchronous, and calling it
    straight from an async handler blocks the whole event loop -- measured:
    /api/config, which only reads a local file, went from 7ms to 750ms while one
    device was being interrogated. On a shared farm that means one person opening
    a panel freezes everybody else's dashboard.
    """
    return await asyncio.to_thread(collect_devices, refresh)

def collect_devices(refresh: bool):
    try:
        devices = []
        states = list_device_states()
        attached = adb.device_list()
        keep = set(drop_duplicate_transports([d.serial for d in attached]))
        adbd = [d for d in attached if d.serial in keep]
        online = {d.serial for d in adbd}
        forget_absent_devices(set(states) | online)

        for d in adbd:
            try:
                detail = device_detail(d, refresh=refresh)
                model = detail["model"]
                version, sdk = detail["version"], detail["sdk"]
                width, height = detail["width"], detail["height"]
                ip, battery = detail["ip"], detail["battery"]

                # Alias Lookup
                alias = device_aliases.get(d.serial, model)

                # Lease Lookup
                lease = get_lease(d.serial)

                devices.append({
                    "serial": d.serial,
                    "model": model,
                    "version": version,
                    "width": width,
                    "height": height,
                    "ip": ip,
                    "sdk": sdk,
                    "battery": battery,
                    "alias": alias,
                    "state": "device",
                    "state_hint": None,
                    "occupied_by": lease["owner"] if lease else None,
                    "occupied_until": lease["expires_at"] if lease else None
                })
            except Exception as e:
                # Reading details failed, but adb says the device is there. Show
                # it with the reason rather than dropping it -- a device that
                # vanishes with only a line in the server log is the hardest
                # kind of fault to chase, which is exactly how the wireless bug
                # above stayed hidden.
                print(f"Error reading device {d.serial}: {e}", flush=True)
                devices.append({
                    "serial": d.serial,
                    "model": device_aliases.get(d.serial, d.serial),
                    "version": "?", "width": 0, "height": 0,
                    "ip": "?", "sdk": "?", "battery": "?",
                    "alias": device_aliases.get(d.serial, d.serial),
                    "state": "error",
                    "state_hint": f"기기 정보를 읽지 못했습니다: {e}",
                    "occupied_by": None, "occupied_until": None
                })

        # Attached but unusable: report them so the slot is not just empty.
        for serial, state in states.items():
            if serial in online:
                continue
            devices.append({
                "serial": serial,
                "model": device_aliases.get(serial, serial),
                "version": "?", "width": 0, "height": 0,
                "ip": "?", "sdk": "?", "battery": "?",
                "alias": device_aliases.get(serial, serial),
                "state": state,
                "state_hint": STATE_LABELS.get(state, f"사용 불가 상태: {state}"),
                "occupied_by": None, "occupied_until": None
            })

        return {"devices": devices}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"devices": [], "error": str(e)}

@app.post("/api/install/{serial}")
async def install_apk(serial: str, file: UploadFile = File(...)):
    # The client controls this filename, so keep only the basename and drop it
    # into the working directory rather than wherever the name points.
    safe_name = os.path.basename(file.filename or "upload.apk").replace("\\", "_")
    temp_file = f"temp_{safe_name}"
    try:
        # Save uploaded file
        with open(temp_file, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        print(f"[{serial}] Installing {safe_name}...")
        adb_path = get_adb_path()

        # Run adb install (argv form: the filename never reaches a shell)
        proc = await asyncio.create_subprocess_exec(
            adb_path, "-s", serial, "install", "-r", temp_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            print(f"[{serial}] Install Success")
            return JSONResponse(content={"status": "success", "message": "Installed successfully"})
        else:
            print(f"[{serial}] Install Failed: {stderr.decode()}")
            return JSONResponse(content={"status": "error", "message": stderr.decode()}, status_code=500)
            
    except Exception as e:
        print(f"Install Exception: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)



# --- Management APIs ---

@app.get("/api/info/{serial}")
async def get_device_info(serial: str):
    # Several adb round trips; see collect_devices() on why this cannot run on
    # the event loop.
    return await asyncio.to_thread(read_device_info, serial)

def read_device_info(serial: str):
    try:
        d = adb.device(serial=serial)
        props = d.prop
        
        # Get IP Address (Safe method)
        ip = "Unknown"
        try:
            # parsing 'ip route' -> '... src 192.168.x.x ...'
            ip_output = d.shell("ip route")
            match = re.search(r"src (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", ip_output)
            if match:
                ip = match.group(1)
        except:
            pass
            
        # Fetch Current App
        current_app = "Unknown"
        try:
            cur = d.app_current()
            if cur:
                 current_app = cur.package
        except:
            pass
        
        # Screen size belongs on a device-farm detail panel -- it is the first
        # thing you check when a layout bug only shows up on one tablet.
        # get_device_resolution() answers None for a device that will not say.
        size = get_device_resolution(serial)
        width, height = size if size else (0, 0)

        info = {
            "model": props.get("ro.product.model"),
            "manufacturer": props.get("ro.product.manufacturer"),
            "android_version": props.get("ro.build.version.release"),
            "sdk": props.get("ro.build.version.sdk"),
            "serial": serial,
            "ip": ip,
            "current_app": current_app,
            "cpu": props.get("ro.board.platform", "Unknown"),
            "memory": get_total_memory(d),
            "abi": props.get("ro.product.cpu.abi", "Unknown"),
            "build_id": props.get("ro.build.display.id") or props.get("ro.build.id", "Unknown"),
            "width": width,
            "height": height,
        }
        return JSONResponse(content={"status": "success", "info": info})
    except Exception as e:
        print(f"Info Error: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/packages/{serial}")
async def get_packages(serial: str):
    return await asyncio.to_thread(read_packages, serial)

def read_packages(serial: str):
    try:
        d = adb.device(serial=serial)
        # List 3rd party packages
        output = d.shell("pm list packages -3")
        packages = [line.replace("package:", "").strip() for line in output.split("\n") if line.strip()]
        return JSONResponse(content={"status": "success", "packages": packages})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/uninstall/{serial}")
async def uninstall_package(serial: str, request: Request):
    try:
        body = await request.json()
        package = body.get("package")
        d = adb.device(serial=serial)
        
        # Uninstall
        # output is usually 'Success' or 'Failure'
        output = d.shell(f"pm uninstall {package}")
        
        if "Success" in output:
            return JSONResponse(content={"status": "success"})
        else:
            return JSONResponse(content={"status": "error", "message": output})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

WIRELESS_PORT = 5555

@app.post("/api/wireless/{serial}")
async def enable_wireless(serial: str):
    try:
        d = adb.device(serial=serial)
        
        # Get IP
        ip_output = d.shell("ip route")
        match = re.search(r"src (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", ip_output)
        if not match:
             return JSONResponse(content={"status": "error", "message": "Device has no IP. Connect to WiFi first."})
        
        ip = match.group(1)

        # Switch to TCPIP. This restarts adbd on the device, which tears down
        # the very connection the command was issued over -- the client then
        # sees the socket drop and raises. That is what success looks like, so
        # swallowing it here is the point: letting it propagate aborted the
        # reconnect below and reported failure for a switch that had worked.
        try:
            d.tcpip(WIRELESS_PORT)
        except Exception as drop:
            print(f"[{serial}] tcpip() closed the link as expected: {drop}")

        # adbd takes a moment to come back listening on the new port, and how
        # long varies. A single attempt after a fixed wait reported success
        # while the device was not actually reachable yet, so retry and then
        # confirm against the device list before claiming anything.
        target = f"{ip}:{WIRELESS_PORT}"
        result = None
        for attempt in range(6):
            await asyncio.sleep(1.5)
            try:
                result = adb.connect(target, timeout=5)
            except Exception as e:
                result = str(e)
            if target in {info.serial for info in adb.list()}:
                break
            print(f"[{serial}] Wireless attempt {attempt + 1}: {result}")
        else:
            return JSONResponse(
                {"status": "error",
                 "message": f"Switched to TCP/IP, but {target} never appeared in the device "
                            f"list (last response: {result}). Check that the PC and the device "
                            f"are on the same network and that no firewall blocks "
                            f"{WIRELESS_PORT}.",
                 "address": target},
                status_code=502,
            )

        print(f"[{serial}] Wireless: {result}")
        return JSONResponse(content={
            "status": "success",
            "message": f"Connected wirelessly to {target}. 케이블을 뽑아도 유지됩니다.",
            "address": target,
        })
    except Exception as e:
        print(f"Wireless Error: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/usb/{serial}")
async def disable_wireless(serial: str):
    """Send a device back to USB-only adb -- the counterpart to /api/wireless.

    Without this the switch was one-way from the farm's side: you could put a
    device on the network but had to reach for a terminal to undo it.
    """
    try:
        # Same story as tcpip(): restarting adbd drops the connection the
        # command arrived on, so the error is the success case.
        try:
            await adb_exec(get_adb_path(), serial, "usb")
        except Exception as drop:
            print(f"[{serial}] usb() closed the link as expected: {drop}")

        await asyncio.sleep(2)
        # Drop the TCP entry so the device does not linger in the list as an
        # unreachable ghost after the switch.
        host = serial.split(":")[0]
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
            try:
                adb.disconnect(serial)
            except Exception:
                pass
        device_cache.pop(serial, None)
        return {"status": "success", "message": "USB 모드로 되돌렸습니다."}
    except Exception as e:
        print(f"USB Error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# --- Stream server cleanup ---
# ws-scrcpy pushes a server process onto the device and does not always take it
# down: restart ws-scrcpy, or replug the device, and the old one keeps holding
# the port so the next connection produces a black screen. Diagnosing that cost
# real time during development, and the manual fix -- ps, grep, kill -- is easy
# to get wrong, because the process shows up as a generic `app_process`. Match
# the scrcpy class instead of the interpreter so nothing else can be hit.

SCRCPY_SERVER_MARKER = "com.genymobile.scrcpy.Server"

def kill_stale_stream_servers(serial: str):
    d = adb.device(serial=serial)
    killed = []
    for line in d.shell("ps -A -o PID,ARGS").splitlines():
        if SCRCPY_SERVER_MARKER not in line:
            continue
        pid = line.split()[0] if line.split() else ""
        if not pid.isdigit():
            continue
        d.shell(f"kill {pid}")
        killed.append(int(pid))
    return killed

@app.post("/api/device/{serial}/reset-stream")
async def reset_stream(serial: str):
    """Kill any scrcpy server left behind on the device.

    Use when the mirror is black or the stream log says the service could not
    start. Safe while nothing is streaming; it only ends processes the mirror
    would have replaced anyway.
    """
    try:
        killed = kill_stale_stream_servers(serial)
        print(f"[{serial}] reset-stream killed {killed or 'nothing'}")
        return {
            "status": "success",
            "killed": killed,
            "message": (f"{len(killed)}개의 잔존 스트림 프로세스를 정리했습니다."
                        if killed else "정리할 잔존 프로세스가 없습니다."),
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# Screenshot endpoint consolidated above

# --- Lease API ---

class OccupyRequest(BaseModel):
    owner: str
    ttl_seconds: int = DEFAULT_LEASE_SECONDS

class ReleaseRequest(BaseModel):
    owner: str

def grant_lease(serial: str, req: OccupyRequest):
    lease = {"owner": req.owner, "expires_at": time.time() + max(1, req.ttl_seconds)}
    device_leases[lease_key(serial)] = lease
    save_leases()
    return {"status": "success", "serial": serial, **lease}

@app.post("/api/device/{serial}/occupy")
async def occupy_device(serial: str, req: OccupyRequest):
    """Claim one named device. 409 if somebody else holds it."""
    conflict = lease_conflict(serial, req.owner)
    if conflict:
        return conflict
    return grant_lease(serial, req)

@app.post("/api/devices/occupy")
async def occupy_any_device(req: OccupyRequest):
    """Claim any free device. This is what a CI job calls when it just needs 'an Android'."""
    try:
        serials = drop_duplicate_transports([d.serial for d in adb.device_list()])
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    for serial in serials:
        if get_lease(serial) is None:
            return grant_lease(serial, req)

    return JSONResponse(
        {"status": "error", "message": "No free device", "total": len(serials)},
        status_code=409,
    )

@app.post("/api/device/{serial}/release")
async def release_device(serial: str, req: ReleaseRequest):
    conflict = lease_conflict(serial, req.owner)
    if conflict:
        return conflict
    device_leases.pop(lease_key(serial), None)
    save_leases()
    return {"status": "success", "serial": serial}

@app.get("/api/leases")
async def list_leases():
    return {"leases": {s: get_lease(s) for s in list(device_leases) if get_lease(s)}}

# --- Stateless Input API (CLI / CI) ---
# The browser drives devices over the WebSocket above. Scripts get the same
# actions as plain HTTP calls, so there is no session to open and close.

class InputRequest(BaseModel):
    type: str
    owner: str = None
    x: int = None
    y: int = None
    x1: int = None
    y1: int = None
    x2: int = None
    y2: int = None
    duration: int = 300
    keycode: int = None
    text: str = None

@app.post("/api/device/{serial}/input")
async def send_input(serial: str, req: InputRequest):
    conflict = lease_conflict(serial, req.owner)
    if conflict:
        return conflict
    event = req.model_dump(exclude_none=True)
    try:
        await dispatch_input(get_adb_path(), serial, event)
    except ValueError as bad_event:
        return JSONResponse({"status": "error", "message": str(bad_event)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # A recording has to capture whatever drove the device, not just the
    # browser's WebSocket. Otherwise scripting a session over HTTP while
    # recording saves an empty macro and says nothing about it.
    if serial in active_recordings:
        step = dict(event)
        step.pop("owner", None)         # a lease holder is not part of the action
        step["timestamp"] = time.time()
        active_recordings[serial].append(step)

    return {"status": "success"}

def adb_server_info():
    """Which adb server is answering on port 5037, and does it look stale.

    The number is the diagnosis: a client only ever talks to whatever server
    already holds the port, and a mismatched one gets killed and replaced on
    every command. Anything below the current 41 means some other adb on the
    machine is fighting for the port.
    """
    try:
        version = adb.server_version()
    except Exception as e:
        return {"version": None, "note": f"could not query adb server: {e}"}

    info = {"version": version}
    if isinstance(version, int) and version < 41:
        info["note"] = ("An older adb server holds port 5037. Another adb on this "
                        "machine is restarting it, and every `adb connect` wireless "
                        "device is dropped when that happens.")
    return info

@app.get("/api/health")
async def health():
    """Liveness probe: is the server up, and can it still talk to adb?"""
    return await asyncio.to_thread(check_health)

def check_health():
    try:
        serials = drop_duplicate_transports([d.serial for d in adb.device_list()])
        leased = sum(1 for s in serials if get_lease(s))
        # Attached-but-unusable devices are surfaced here too, so a monitor can
        # alert on "three phones went unauthorized" instead of "count dropped".
        unusable = {s: st for s, st in list_device_states().items() if s not in set(serials)}
        binary = adb_binary_info()
        return {
            # A reachable adb server is not the same thing as a working farm. If
            # the binary is missing, say degraded rather than ok -- that is the
            # whole point of a health endpoint.
            "status": "ok" if binary["ok"] else "degraded",
            "adb": "ok",
            "devices_total": len(serials),
            "devices_free": len(serials) - leased,
            "devices_unusable": len(unusable),
            "unusable": unusable,
            "adb_path": binary["path"],
            "adb_binary": binary,
            # The *server* version matters, not just which binary this process
            # would launch. A second adb of a different version on the machine
            # (bundled with a screen-mirroring tool, an IDE, a vendor utility)
            # keeps killing and restarting the server, and every wireless
            # connection dies with it. That failure is invisible from the client
            # path alone, so surface what is actually answering on port 5037.
            "adb_server": adb_server_info(),
        }
    except Exception as e:
        return JSONResponse(
            {"status": "degraded", "adb": "unreachable", "message": str(e)},
            status_code=503,
        )

# --- Macro System ---

MACROS_DIR = "macros"
if not os.path.exists(MACROS_DIR):
    os.makedirs(MACROS_DIR)

active_recordings = {} # { serial: [ {event...}, ... ] }

import glob

def macro_path(name: str) -> str:
    """Map a macro name to a file inside MACROS_DIR.

    The name arrives from the client, so anything that could climb out of the
    directory ('../secrets', an absolute path, a leading dot) is refused rather
    than normalised -- a caller asking for that is not asking for a macro.
    """
    if not name or os.path.basename(name) != name or name.startswith("."):
        raise ValueError(f"Invalid macro name: {name!r}")
    return os.path.join(MACROS_DIR, f"{name}.json")

def get_total_memory(device):
    """Installed RAM as a human string, e.g. '7.6 GB'."""
    try:
        match = re.search(r"MemTotal:\s+(\d+)\s*kB", device.shell("cat /proc/meminfo"))
        if match:
            return f"{int(match.group(1)) / 1024 / 1024:.1f} GB"
    except Exception:
        pass
    return "Unknown"

def get_device_resolution(serial: str):
    """Physical screen size as (width, height), or None if the device won't say."""
    try:
        res = adb.device(serial=serial).shell("wm size")
        if res and "Physical size:" in res:
            w, h = res.split(":")[-1].strip().split("x")
            return int(w), int(h)
    except Exception:
        pass
    return None

def scale_event(event: dict, sx: float, sy: float) -> dict:
    """Rescale tap/swipe coordinates for a screen of a different size."""
    if sx == 1.0 and sy == 1.0:
        return event
    out = dict(event)
    for key, factor in (("x", sx), ("x1", sx), ("x2", sx),
                        ("y", sy), ("y1", sy), ("y2", sy)):
        if out.get(key) is not None:
            out[key] = round(out[key] * factor)
    return out

def read_macro(name: str):
    """Load a macro file, accepting both the v1 (bare list) and v2 layouts.

    v1 recordings carry no resolution, so they replay unscaled -- the same
    behaviour they had before scaling existed.
    """
    with open(macro_path(name), "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"version": 1, "recorded_on": None, "events": data}
    return data

@app.post("/api/macros/start_record/{serial}")
async def start_record(serial: str):
    active_recordings[serial] = []
    print(f"[{serial}] Recording Started")
    return {"status": "success"}

@app.post("/api/macros/stop_record/{serial}")
async def stop_record(serial: str, request: Request):
    try:
        body = await request.json()
        name = body.get("name") or f"macro_{int(time.time())}"

        if serial not in active_recordings:
            return JSONResponse({"status": "error", "message": "Not recording"}, status_code=400)

        events = active_recordings.pop(serial)

        # Record the screen size alongside the events, so the same macro can be
        # replayed on a device with a different resolution.
        resolution = get_device_resolution(serial)
        payload = {
            "version": 2,
            "recorded_on": {
                "serial": serial,
                "width": resolution[0] if resolution else None,
                "height": resolution[1] if resolution else None,
            },
            "events": events,
        }

        with open(macro_path(name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        print(f"[{serial}] Recording Saved: {name} ({len(events)} events)")
        return {"status": "success", "count": len(events), "resolution": resolution}
    except ValueError as bad_name:
        return JSONResponse({"status": "error", "message": str(bad_name)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/macros")
async def list_macros():
    macros = []
    for path in sorted(glob.glob(os.path.join(MACROS_DIR, "*.json"))):
        name = os.path.basename(path)[:-len(".json")]
        entry = {"name": name, "events": None, "width": None, "height": None}
        try:
            data = read_macro(name)
            entry["events"] = len(data.get("events", []))
            rec = data.get("recorded_on") or {}
            entry["width"], entry["height"] = rec.get("width"), rec.get("height")
        except Exception:
            pass  # An unreadable file still gets listed, just without detail.
        macros.append(entry)
    # 'names' keeps the old flat shape working for existing callers.
    return {"macros": [m["name"] for m in macros], "details": macros}

@app.post("/api/macros/play/{serial}")
async def play_macro(serial: str, request: Request):
    body = await request.json()
    name = body.get("name")
    count = int(body.get("count", 1)) # Default 1 loop

    try:
        macro = read_macro(name)
    except ValueError as bad_name:
        return JSONResponse({"status": "error", "message": str(bad_name)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"status": "error", "message": "Macro not found"}, status_code=404)

    asyncio.create_task(run_macro_bg(serial, macro, count))
    return {"status": "success", "message": f"Playback started ({count} loops)"}

@app.delete("/api/macros/{name}")
async def delete_macro(name: str):
    try:
        path = macro_path(name)
    except ValueError as bad_name:
        return JSONResponse({"status": "error", "message": str(bad_name)}, status_code=400)
    if not os.path.exists(path):
        return JSONResponse({"status": "error", "message": "Macro not found"}, status_code=404)
    os.remove(path)
    print(f"Macro deleted: {name}")
    return {"status": "success", "deleted": name}

@app.get("/api/macros/{name}")
async def get_macro_content(name: str):
    try:
        return {"status": "success", "data": read_macro(name)}
    except ValueError as bad_name:
        return JSONResponse({"status": "error", "message": str(bad_name)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"status": "error", "message": "Macro not found"}, status_code=404)

async def run_macro_bg(serial, macro, count=1):
    events = macro.get("events") if isinstance(macro, dict) else macro
    if not events:
        return

    adb_path = get_adb_path()

    # Work out the coordinate scale for this target device.
    sx = sy = 1.0
    rec = (macro.get("recorded_on") or {}) if isinstance(macro, dict) else {}
    if rec.get("width") and rec.get("height"):
        target = get_device_resolution(serial)
        if target:
            sx, sy = target[0] / rec["width"], target[1] / rec["height"]
            if (sx, sy) != (1.0, 1.0):
                print(f"[{serial}] Scaling macro {rec['width']}x{rec['height']} "
                      f"-> {target[0]}x{target[1]}")

    print(f"[{serial}] Replay Started ({count} loops)")

    for i in range(count):
        print(f"[{serial}] Loop {i+1}/{count}")

        start_time = events[0].get("timestamp", 0)
        prev_time = start_time

        for event in events:
            curr_time = event.get("timestamp", 0)
            delay = curr_time - prev_time
            if delay > 0:
                await asyncio.sleep(delay)
            prev_time = curr_time

            # Execute Command
            try:
                await dispatch_input(adb_path, serial, scale_event(event, sx, sy))
            except (ValueError, RuntimeError) as step_err:
                print(f"[{serial}] Replay step failed: {step_err}")

        # Optional delay between loops
        await asyncio.sleep(1)

    print(f"[{serial}] Replay Finished")


# --- App Control ---
# Launch / stop / wipe an app. These are the three things you do between test
# runs, and doing them by hand over adb is most of the friction in a manual pass.

PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

def check_package(package: str) -> str:
    if not package or not PACKAGE_RE.match(package):
        raise ValueError(f"Invalid package name: {package!r}")
    return package

class AppRequest(BaseModel):
    package: str
    owner: str = None

APP_ACTIONS = {
    # monkey resolves the launcher activity for us, so the caller only needs the package.
    "launch": lambda pkg: ("shell", "monkey", "-p", pkg, "-c",
                           "android.intent.category.LAUNCHER", "1"),
    "stop":   lambda pkg: ("shell", "am", "force-stop", pkg),
    "clear":  lambda pkg: ("shell", "pm", "clear", pkg),
}

async def run_app_action(serial: str, action: str, package: str):
    check_package(package)
    if action not in APP_ACTIONS:
        raise ValueError(f"Unknown action: {action!r} (expected one of {list(APP_ACTIONS)})")
    await adb_exec(get_adb_path(), serial, *APP_ACTIONS[action](package))

@app.post("/api/app/{serial}/{action}")
async def app_control(serial: str, action: str, req: AppRequest):
    """action is one of launch / stop / clear."""
    conflict = lease_conflict(serial, req.owner)
    if conflict:
        return conflict
    try:
        await run_app_action(serial, action, req.package)
        return {"status": "success", "action": action, "package": req.package}
    except ValueError as bad:
        return JSONResponse({"status": "error", "message": str(bad)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# --- Logcat Capture ---
# QA work lives or dies on the log. Pulling logcat by hand per device does not
# scale past a couple of phones, so the server tails it into a bounded buffer
# and flags the lines that mean "this run failed".

from collections import deque

LOGCAT_MAX_LINES = 20000

# A crash is worth surfacing on its own rather than making someone scroll.
CRASH_PATTERNS = [
    ("java",   re.compile(r"FATAL EXCEPTION")),
    ("native", re.compile(r"F/libc|Fatal signal \d+ \(SIG")),
    ("anr",    re.compile(r"ANR in ")),
]

logcat_sessions = {}  # { serial: {"proc":, "task":, "lines": deque, "crashes": [], "started": float} }

def find_crash(line: str):
    for kind, pattern in CRASH_PATTERNS:
        if pattern.search(line):
            return kind
    return None

async def pump_logcat(serial: str, session: dict):
    """Read logcat lines until the process ends or the session is stopped."""
    try:
        stream = session["proc"].stdout
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            session["lines"].append(line)
            if session.get("file"):
                try:
                    session["file"].write(line + "\n")
                except Exception:
                    # Disk full or the path went away: drop the file and keep
                    # capturing to memory rather than killing the pump.
                    session["file"] = None
            kind = find_crash(line)
            if kind:
                session["crashes"].append({"kind": kind, "line": line, "at": time.time()})
                print(f"[{serial}] CRASH ({kind}): {line[:160]}")
    finally:
        # adb can go away on its own -- cable pulled, device rebooted, adb server
        # restarted. Without this the session stayed in the table looking alive,
        # so /start answered "Already capturing" and capture could never be
        # resumed short of restarting the server.
        session["ended"] = True
        print(f"[{serial}] Logcat pump ended ({len(session['lines'])} lines buffered)")

async def reap_logcat(serial: str, session: dict):
    """Stop a capture's pump and adb process, and let asyncio close its pipes.

    Skipping the wait() leaves the stdout pipe transport open, so a farm that
    cycles capture all day slowly accumulates them.
    """
    task = session.get("task")
    if task is not None:
        task.cancel()

    handle = session.get("file")
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
        session["file"] = None

    proc = session.get("proc")
    if proc is None:
        return
    try:
        if proc.returncode is None:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except Exception as e:
        print(f"[{serial}] Logcat reap: {e}")

LOGCAT_DIR = "logs"

class LogcatStartRequest(BaseModel):
    clear: bool = True          # drop whatever is already buffered on the device
    level: str = "V"            # V/D/I/W/E/F -- minimum priority to keep
    owner: str = None
    # The in-memory buffer is a fixed 20k lines, so an overnight run silently
    # loses its beginning -- and the beginning is usually where the fault is.
    # Writing to disk as well keeps the whole run without unbounded memory.
    to_file: bool = False

@app.post("/api/logcat/{serial}/start")
async def start_logcat(serial: str, req: LogcatStartRequest = LogcatStartRequest()):
    conflict = lease_conflict(serial, req.owner)
    if conflict:
        return conflict

    existing = logcat_sessions.get(serial)
    if existing and not existing.get("ended"):
        return {"status": "success", "message": "Already capturing"}
    if existing:
        # Previous capture died; drop it and start clean. Its buffer is lost,
        # which is why the dashboard nudges you to download before restarting.
        await reap_logcat(serial, logcat_sessions.pop(serial))

    level = (req.level or "V").upper()
    if level not in ("V", "D", "I", "W", "E", "F"):
        return JSONResponse({"status": "error", "message": f"Invalid level: {req.level!r}"},
                            status_code=400)

    adb_path = get_adb_path()
    try:
        if req.clear:
            await adb_exec(adb_path, serial, "logcat", "-c")

        proc = await asyncio.create_subprocess_exec(
            adb_path, "-s", serial, "logcat", "-v", "time", f"*:{level}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    logfile = logpath = None
    if req.to_file:
        try:
            os.makedirs(LOGCAT_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_serial = re.sub(r"[^A-Za-z0-9._-]", "_", serial)
            logpath = os.path.join(LOGCAT_DIR, f"logcat_{safe_serial}_{stamp}.txt")
            logfile = open(logpath, "w", encoding="utf-8", errors="replace")
        except Exception as e:
            # Losing the file is not a reason to lose the capture.
            print(f"[{serial}] Could not open log file: {e}")
            logfile = logpath = None

    session = {
        "proc": proc,
        "lines": deque(maxlen=LOGCAT_MAX_LINES),
        "crashes": [],
        "started": time.time(),
        "level": level,
        "ended": False,
        "file": logfile,
        "path": logpath,
    }
    session["task"] = asyncio.create_task(pump_logcat(serial, session))
    logcat_sessions[serial] = session

    print(f"[{serial}] Logcat capture started (level {level})"
          + (f", writing to {logpath}" if logpath else ""))
    return {"status": "success", "level": level, "file": logpath}

@app.post("/api/logcat/{serial}/stop")
async def stop_logcat(serial: str):
    # Kept in the table rather than popped: stopping a capture and *then*
    # collecting the log is the obvious order, and dropping the session here made
    # both the buffer and the download 404 the moment you stopped -- so a run
    # without to_file lost everything it had just captured. The next /start
    # reaps the ended session and begins clean.
    # "stopped" is not "ended": a pump whose adb already died is ended but still
    # holds a process and pipes, and that is precisely the case that needs
    # reaping. Only an already-reaped session is a no-op.
    session = logcat_sessions.get(serial)
    if not session or session.get("stopped"):
        return {"status": "success", "message": "Not capturing"}

    await reap_logcat(serial, session)
    session["ended"] = True
    session["stopped"] = True
    print(f"[{serial}] Logcat capture stopped ({len(session['lines'])} lines)")
    return {"status": "success", "lines": len(session["lines"]),
            "crashes": len(session["crashes"])}

@app.get("/api/logcat/{serial}")
async def get_logcat(serial: str, tail: int = 500, contains: str = None):
    """Buffered lines, newest last. `tail` caps the response, `contains` filters."""
    session = logcat_sessions.get(serial)
    if not session:
        return JSONResponse({"status": "error", "message": "Not capturing"}, status_code=404)

    lines = list(session["lines"])
    if contains:
        needle = contains.lower()
        lines = [ln for ln in lines if needle in ln.lower()]

    return {
        "status": "success",
        # False once adb has gone away; the buffer is still readable.
        "capturing": not session.get("ended", False),
        "level": session["level"],
        "started": session["started"],
        "total": len(session["lines"]),
        "matched": len(lines),
        "file": session.get("path"),
        "crashes": session["crashes"],
        "lines": lines[-max(1, tail):],
    }

@app.get("/api/logcat/{serial}/download")
async def download_logcat(serial: str):
    session = logcat_sessions.get(serial)
    if not session:
        return JSONResponse({"status": "error", "message": "Not capturing"}, status_code=404)
    body = "\n".join(session["lines"]) + "\n"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="logcat_{serial}.txt"'},
    )

@app.get("/api/logcat")
async def logcat_status():
    """Which devices are being captured, and has anything crashed."""
    return {
        "sessions": {
            serial: {
                "lines": len(s["lines"]),
                "crashes": len(s["crashes"]),
                "level": s["level"],
                "started": s["started"],
                "capturing": not s.get("ended", False),
            }
            for serial, s in logcat_sessions.items()
        }
    }


# --- Batch Operations ---
# One action across many devices at once. This is the whole point of a farm:
# install this build everywhere, replay this scenario everywhere, then tell me
# which devices failed.

def lease_block_reason(serial: str, owner):
    """Why this caller may not touch the device right now, or None if it may."""
    lease = get_lease(serial)
    if lease and lease["owner"] != owner:
        return f"held by '{lease['owner']}'"
    return None

async def gather_per_device(serials, coro_factory, owner=None):
    """Run one coroutine per device concurrently and report each result separately.

    A device that fails is reported as a failure, not raised -- the caller wants
    the other nine results. Devices somebody else has leased come back as
    'skipped' rather than 'error': nothing went wrong, the farm just declined.
    """
    if not serials:
        raise ValueError("No serials given")

    async def one(serial):
        blocked = lease_block_reason(serial, owner)
        if blocked:
            return {"serial": serial, "status": "skipped", "message": blocked}
        try:
            detail = await coro_factory(serial)
            return {"serial": serial, "status": "success", "message": detail or "ok"}
        except Exception as e:
            return {"serial": serial, "status": "error", "message": str(e)}

    results = await asyncio.gather(*(one(s) for s in serials))
    failed = [r for r in results if r["status"] == "error"]
    skipped = [r for r in results if r["status"] == "skipped"]
    return {
        "status": "success" if not (failed or skipped) else "partial",
        "total": len(results),
        "succeeded": len(results) - len(failed) - len(skipped),
        "failed": len(failed),
        "skipped": len(skipped),
        "results": results,
    }

class BatchInputRequest(BaseModel):
    serials: list
    event: dict
    owner: str = None

@app.post("/api/batch/input")
async def batch_input(req: BatchInputRequest):
    adb_path = get_adb_path()

    async def act(serial):
        await dispatch_input(adb_path, serial, req.event)

    try:
        return await gather_per_device(req.serials, act, req.owner)
    except ValueError as bad:
        return JSONResponse({"status": "error", "message": str(bad)}, status_code=400)

class BatchAppRequest(BaseModel):
    serials: list
    action: str
    package: str
    owner: str = None

@app.post("/api/batch/app")
async def batch_app(req: BatchAppRequest):
    try:
        check_package(req.package)
        return await gather_per_device(
            req.serials, lambda s: run_app_action(s, req.action, req.package), req.owner
        )
    except ValueError as bad:
        return JSONResponse({"status": "error", "message": str(bad)}, status_code=400)

class BatchMacroRequest(BaseModel):
    serials: list
    name: str
    count: int = 1
    owner: str = None

@app.post("/api/batch/macro")
async def batch_macro(req: BatchMacroRequest):
    """Replay one macro on several devices at once.

    Playback is a background task per device, so this returns as soon as every
    device has started -- coordinates are rescaled per device on the way in.
    """
    try:
        macro = read_macro(req.name)
    except ValueError as bad:
        return JSONResponse({"status": "error", "message": str(bad)}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"status": "error", "message": "Macro not found"}, status_code=404)

    async def start(serial):
        asyncio.create_task(run_macro_bg(serial, macro, req.count))
        return f"playback started ({req.count} loops)"

    try:
        return await gather_per_device(req.serials, start, req.owner)
    except ValueError as bad:
        return JSONResponse({"status": "error", "message": str(bad)}, status_code=400)

@app.post("/api/batch/install")
async def batch_install(serials: str = Form(...), owner: str = Form(None),
                        file: UploadFile = File(...)):
    """Install one APK on several devices. `serials` is a comma-separated list."""
    targets = [s.strip() for s in serials.split(",") if s.strip()]
    safe_name = os.path.basename(file.filename or "upload.apk").replace("\\", "_")
    temp_file = f"temp_batch_{safe_name}"

    try:
        with open(temp_file, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        adb_path = get_adb_path()

        async def install(serial):
            await adb_exec(adb_path, serial, "install", "-r", temp_file)
            return "installed"

        return await gather_per_device(targets, install, owner)
    except ValueError as bad:
        return JSONResponse({"status": "error", "message": str(bad)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)


active_audio_procs = {} # { serial: subprocess_obj }

@app.post("/api/audio/start/{serial}")
async def start_audio(serial: str):
    """
    Starts Audio forwarding on the Host PC using local Scrcpy 2.7 binary.
    """
    try:
        if serial in active_audio_procs:
            if active_audio_procs[serial].poll() is None:
                return {"status": "success", "message": "Audio already running"}
            else:
                del active_audio_procs[serial] # Cleanup dead process

        exe = "scrcpy.exe" if os.name == "nt" else "scrcpy"
        scrcpy_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrcpy_bin", exe)
        if not os.path.exists(scrcpy_bin):
            found = shutil.which("scrcpy")
            if not found:
                # The binary is not redistributed with this repo, so say where
                # to put it rather than just reporting a missing file.
                return JSONResponse({
                    "status": "error",
                    "message": ("scrcpy를 찾을 수 없습니다. scrcpy 2.7 이상을 "
                                "scrcpy_bin/ 에 두거나 PATH에 추가하세요. "
                                "https://github.com/Genymobile/scrcpy/releases"),
                }, status_code=503)
            scrcpy_bin = found

        # Launch with a visible title so user knows what it is. Keep it ASCII:
        # the title is also logged, and an emoji here is what made this endpoint
        # fail outright on a cp949 console.
        cmd = [scrcpy_bin, "-s", serial, "--no-video", "--no-control",
               "--window-title", f"AUDIO - {serial}"]
        
        print(f"[{serial}] Starting Audio: {' '.join(cmd)}")
        # Store process to kill later
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(scrcpy_bin))
        active_audio_procs[serial] = proc
        
        return {"status": "success", "message": "Audio started"}
    
    except Exception as e:
        print(f"Audio Error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/alias/{serial}")
async def set_alias(serial: str, request: Request):
    """
    Sets a custom alias for a device. Passing empty name resets to model.
    """
    try:
        data = await request.json()
        name = data.get("name", "").strip()
        
        if not name:
            if serial in device_aliases:
                del device_aliases[serial]
        else:
            device_aliases[serial] = name
            
        save_aliases()
        return {"status": "success", "alias": name}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/audio/stop/{serial}")
async def stop_audio(serial: str):
    try:
        if serial in active_audio_procs:
            proc = active_audio_procs[serial]
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            del active_audio_procs[serial]
            return {"status": "success", "message": "Audio stopped"}
        else:
            return {"status": "success", "message": "No audio running"}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

def port_owner(host: str, port: int):
    """Who already holds this port, if anyone.

    Starting on a taken port otherwise ends in a raw OSError traceback, and on
    Windows a process bound to 127.0.0.1 quietly wins over one bound to :: --
    which is how an unrelated program can end up answering for the farm. Saying
    it plainly up front is worth the few lines.
    """
    import socket
    probe = socket.socket()
    probe.settimeout(0.4)
    try:
        probe.connect(("127.0.0.1" if host in ("0.0.0.0", "::") else host, port))
        return True
    except Exception:
        return False
    finally:
        probe.close()


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("DEVICE_FARM_HOST", "0.0.0.0")
    port = int(os.environ.get("DEVICE_FARM_PORT", "8001"))
    stream_port = get_stream_port()

    if port_owner(host, port):
        print(f"[ERROR] Port {port} is already in use, so the dashboard cannot start.")
        print("        Something else is holding it. Find it with:")
        print(f'          Windows  Get-NetTCPConnection -LocalPort {port} -State Listen')
        print(f"          Linux    lsof -iTCP:{port} -sTCP:LISTEN")
        print("        Then stop that program, or pick another port:")
        print("          set DEVICE_FARM_PORT=8002   (Windows)")
        print("          export DEVICE_FARM_PORT=8002")
        sys.exit(1)

    print("=" * 62)
    print("  QA Device Farm")
    print(f"  Dashboard   http://localhost:{port}/")
    print(f"  API docs    http://localhost:{port}/docs")
    print(f"  Stream      port {stream_port} (ws-scrcpy, started separately)")
    adb_bin = adb_binary_info()
    print(f"  adb         {adb_bin['path']}")
    if not adb_bin["ok"]:
        print("              NOT FOUND - input, app control, install, logcat and")
        print("              wireless will fail. The device list and screenshots")
        print("              still work because adbutils uses the adb server.")
        print("              Install platform-tools on PATH, or copy adb into")
        print("              scrcpy_bin/.")
    if FARM_TOKEN:
        print("  Access      token required (DEVICE_FARM_TOKEN is set)")
    else:
        print("  Access      OPEN - anyone who can reach this port can drive the")
        print("              devices. Set DEVICE_FARM_TOKEN before exposing the farm.")
    print("=" * 62)

    uvicorn.run(app, host=host, port=port)
