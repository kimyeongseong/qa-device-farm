from fastapi import FastAPI, WebSocket, Request, File, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from adbutils import adb
import os
import asyncio
import json
import shutil
import time
from pydantic import BaseModel

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
    allow_origins=["*"],  # Allow port 8000
    # Credentials must stay off while origins is "*": browsers reject that pair,
    # and the farm has no per-user session to carry anyway.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

def get_adb_path():
    # Check local scrcpy_bin folder first
    local_adb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrcpy_bin", "adb.exe")
    if os.path.exists(local_adb):
        return local_adb
    return "C:\\platform-tools\\adb.exe" # Fallback

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

device_leases = {}  # { serial: {"owner": str, "expires_at": float} }

def get_lease(serial: str):
    """Return the live lease for a device, dropping it if it has expired."""
    lease = device_leases.get(serial)
    if lease and lease["expires_at"] <= time.time():
        del device_leases[serial]
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
                from PIL import Image, UnidentifiedImageError
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

@app.websocket("/ws/video/{serial}")
async def websocket_video_endpoint(websocket: WebSocket, serial: str):
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

@app.get("/api/devices")
async def get_devices():
    """
    Returns a list of connected devices with details AND resolution.
    """
    try:
        devices = []
        print(f"DEBUG: Searching devices...", flush=True)
        adbd = adb.device_list()
        print(f"DEBUG: Found {len(adbd)} devices in adbutils", flush=True)
        
        for d in adbd:
            try:
                print(f"DEBUG: Processing {d.serial}", flush=True)
                model = d.prop.get("ro.product.model", "Unknown")
                version = d.prop.get("ro.build.version.release", "?")
                
                # Resolution
                width, height = 0, 0
                try:
                    res = d.shell("wm size") # Physical size: 1080x2400
                    if res and "Physical size:" in res:
                        parts = res.split(":")[-1].strip().split("x")
                        width, height = int(parts[0]), int(parts[1])
                except: pass

                # IP
                ip = d.get_serial_no() if "." in d.serial else "USB" 
                # (Simple heuristic, real IP needs shell ip addr)
                if ip == "USB":
                     try:
                         # Try to find WLAN ip
                         ips = d.shell("ip addr show wlan0 | grep 'inet '")
                         if ips:
                             ip = ips.split()[1].split("/")[0]
                     except: pass
                
                # SDK
                sdk = d.prop.get("ro.build.version.sdk", "?")

                # Battery
                try:
                    bat_output = d.shell("dumpsys battery | grep level")
                    # Take first line only and clean it
                    if bat_output:
                        level_str = bat_output.split("\n")[0].split(":")[1].strip()
                        battery = f"{int(level_str)}%"
                    else:
                        battery = "Unknown"
                except:
                    battery = "Unknown"
                
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
                    "occupied_by": lease["owner"] if lease else None,
                    "occupied_until": lease["expires_at"] if lease else None
                })
                print(f"DEBUG: Added {d.serial}", flush=True)
            except Exception as e:
                print(f"Error reading device {d.serial}: {e}", flush=True)
        
        return {"devices": devices}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"devices": [], "error": str(e)}

class AliasRequest(BaseModel):
    serial: str
    alias: str

@app.post("/api/aliases")
async def update_alias(req: AliasRequest):
    device_aliases[req.serial] = req.alias
    save_aliases()
    return {"status": "ok", "alias": req.alias}

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
    try:
        d = adb.device(serial=serial)
        props = d.prop
        
        # Get IP Address (Safe method)
        ip = "Unknown"
        try:
            # parsing 'ip route' -> '... src 192.168.x.x ...'
            ip_output = d.shell("ip route")
            import re
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
        
        info = {
            "model": props.get("ro.product.model"),
            "manufacturer": props.get("ro.product.manufacturer"),
            "android_version": props.get("ro.build.version.release"),
            "sdk": props.get("ro.build.version.sdk"),
            "serial": serial,
            "ip": ip,
            "current_app": current_app,
            "cpu": props.get("ro.board.platform", "Unknown"),
            "memory": "Checking..." 
        }
        return JSONResponse(content={"status": "success", "info": info})
    except Exception as e:
        print(f"Info Error: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/packages/{serial}")
async def get_packages(serial: str):
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

@app.post("/api/wireless/{serial}")
async def enable_wireless(serial: str):
    try:
        d = adb.device(serial=serial)
        
        # Get IP
        ip_output = d.shell("ip route")
        import re
        match = re.search(r"src (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", ip_output)
        if not match:
             return JSONResponse(content={"status": "error", "message": "Device has no IP. Connect to WiFi first."})
        
        ip = match.group(1)

        # Switch to TCPIP
        d.tcpip(5555)
        
        # Connect
        # Running in background/async might be better but let's try direct
        adb.connect(f"{ip}:5555")
        
        return JSONResponse(content={"status": "success", "message": f"Connected wirelessly to {ip}:5555"})
    except Exception as e:
        print(f"Wireless Error: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

# Screenshot endpoint consolidated above

# --- Lease API ---

class OccupyRequest(BaseModel):
    owner: str
    ttl_seconds: int = DEFAULT_LEASE_SECONDS

class ReleaseRequest(BaseModel):
    owner: str

def grant_lease(serial: str, req: OccupyRequest):
    lease = {"owner": req.owner, "expires_at": time.time() + max(1, req.ttl_seconds)}
    device_leases[serial] = lease
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
        serials = [d.serial for d in adb.device_list()]
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
    device_leases.pop(serial, None)
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
    try:
        await dispatch_input(get_adb_path(), serial, req.model_dump(exclude_none=True))
        return {"status": "success"}
    except ValueError as bad_event:
        return JSONResponse({"status": "error", "message": str(bad_event)}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/health")
async def health():
    """Liveness probe: is the server up, and can it still talk to adb?"""
    try:
        serials = [d.serial for d in adb.device_list()]
        leased = sum(1 for s in serials if get_lease(s))
        return {
            "status": "ok",
            "adb": "ok",
            "devices_total": len(serials),
            "devices_free": len(serials) - leased,
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

@app.post("/api/macros/start_record/{serial}")
async def start_record(serial: str):
    active_recordings[serial] = []
    print(f"[{serial}] Recording Started")
    return {"status": "success"}

@app.post("/api/macros/stop_record/{serial}")
async def stop_record(serial: str, request: Request):
    try:
        body = await request.json()
        name = body.get("name", f"macro_{int(time.time())}")
        
        if serial not in active_recordings:
            return JSONResponse({"status": "error", "message": "Not recording"}, status_code=400)
            
        events = active_recordings.pop(serial)
        
        # Save to file
        filename = os.path.join(MACROS_DIR, f"{name}.json")
        with open(filename, "w") as f:
            json.dump(events, f)
            
        print(f"[{serial}] Recording Saved: {name}")
        return {"status": "success", "count": len(events)}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/macros")
async def list_macros():
    files = glob.glob(os.path.join(MACROS_DIR, "*.json"))
    macros = [os.path.basename(f).replace(".json", "") for f in files]
    return {"macros": macros}

@app.post("/api/macros/play/{serial}")
async def play_macro(serial: str, request: Request):
    body = await request.json()
    name = body.get("name")
    count = int(body.get("count", 1)) # Default 1 loop
    
    filename = os.path.join(MACROS_DIR, f"{name}.json")
    
    if not os.path.exists(filename):
        return JSONResponse({"status": "error", "message": "Macro not found"}, status_code=404)
        
    # Start background task
    with open(filename, "r") as f:
        events = json.load(f)
        
    asyncio.create_task(run_macro_bg(serial, events, count))
    return {"status": "success", "message": f"Playback started ({count} loops)"}

@app.get("/api/macros/{name}")
async def get_macro_content(name: str):
    filename = os.path.join(MACROS_DIR, f"{name}.json")
    if not os.path.exists(filename):
        return JSONResponse({"status": "error", "message": "Macro not found"}, status_code=404)
    with open(filename, "r") as f:
        data = json.load(f)
    return {"status": "success", "data": data}

async def run_macro_bg(serial, events, count=1):
    print(f"[{serial}] Replay Started ({count} loops)")
    adb_path = get_adb_path()
    if not events: return
    
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
                await dispatch_input(adb_path, serial, event)
            except (ValueError, RuntimeError) as step_err:
                print(f"[{serial}] Replay step failed: {step_err}")

        # Optional delay between loops
        await asyncio.sleep(1)
            
    print(f"[{serial}] Replay Finished")


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

        scrcpy_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrcpy_bin", "scrcpy.exe")
        if not os.path.exists(scrcpy_bin):
             return JSONResponse({"status": "error", "message": "Scrcpy binary not found"}, status_code=500)

        # Launch with a visible title so user knows what it is
        cmd = [scrcpy_bin, "-s", serial, "--no-video", "--no-control", "--window-title", f"🔊 AUDIO - {serial}"]
        
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

if __name__ == "__main__":
    import uvicorn
    # Run high performance server on 8001 (Hybird Mode with ws-scrcpy on 8000)
    uvicorn.run(app, host="0.0.0.0", port=8001)
