"""Exercise the new macro / app-control / logcat / batch features."""
import sys, os, json, time, asyncio, shutil, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix="farm_test_")
shutil.copytree(os.path.join(ROOT, "static"), os.path.join(WORK, "static"))
os.makedirs(os.path.join(WORK, "macros"), exist_ok=True)
os.chdir(WORK)

import server
from fastapi.testclient import TestClient

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# ---- fakes -------------------------------------------------------------
class FakeDev:
    def __init__(self, serial): self.serial = serial
server.adb.device_list = lambda: [FakeDev("DEV_A"), FakeDev("DEV_B"), FakeDev("DEV_C")]

RESOLUTIONS = {"DEV_A": (1080, 2400), "DEV_B": (540, 1200), "DEV_C": (1440, 3200)}
server.get_device_resolution = lambda s: RESOLUTIONS.get(s)

calls = []          # every adb argv the server tried to run
FAILING = set()     # devices whose adb calls blow up
async def fake_exec(adb_path, serial, *args):
    if serial in FAILING:
        raise RuntimeError(f"device {serial} offline")
    calls.append((serial,) + args)
server.adb_exec = fake_exec

c = TestClient(server.app)

print("=== macro: record, resolution metadata, list ===")
server.active_recordings["DEV_A"] = [
    {"type": "tap", "x": 540, "y": 1200, "timestamp": 1000.0},
    {"type": "swipe", "x1": 100, "y1": 200, "x2": 900, "y2": 2000, "duration": 250, "timestamp": 1000.5},
]
r = c.post("/api/macros/stop_record/DEV_A", json={"name": "login_flow"})
check("stop_record 200", r.status_code == 200, r.text)
check("stop_record reports resolution", r.json().get("resolution") == [1080, 2400], r.text)

saved = json.load(open(os.path.join(WORK, "macros", "login_flow.json"), encoding="utf-8"))
check("saved as v2", saved.get("version") == 2, str(saved)[:200])
check("saved resolution", saved["recorded_on"]["width"] == 1080 and saved["recorded_on"]["height"] == 2400, str(saved))

r = c.get("/api/macros")
d = r.json()
check("list keeps flat names", d["macros"] == ["login_flow"], r.text)
check("list adds details", d["details"][0]["events"] == 2 and d["details"][0]["width"] == 1080, r.text)

print()
print("=== macro: coordinate scaling across resolutions ===")
ev = {"type": "tap", "x": 540, "y": 1200}
check("no scale when same size", server.scale_event(ev, 1.0, 1.0) == ev)
check("halved device scales down",
      server.scale_event(ev, 0.5, 0.5) == {"type": "tap", "x": 270, "y": 600},
      str(server.scale_event(ev, 0.5, 0.5)))
sw = {"type": "swipe", "x1": 100, "y1": 200, "x2": 900, "y2": 2000}
check("swipe scales all four coords",
      server.scale_event(sw, 2.0, 2.0) == {"type": "swipe", "x1": 200, "y1": 400, "x2": 1800, "y2": 4000},
      str(server.scale_event(sw, 2.0, 2.0)))
check("non-coordinate keys survive",
      server.scale_event({"type": "key", "keycode": 4}, 0.5, 0.5) == {"type": "key", "keycode": 4})

# replay recorded on DEV_A (1080x2400) onto DEV_B (540x1200) -> halved
calls.clear()
macro = server.read_macro("login_flow")
asyncio.run(server.run_macro_bg("DEV_B", macro, 1))
taps = [x for x in calls if "tap" in x]
check("replay on smaller device halves the tap",
      taps and taps[0] == ("DEV_B", "shell", "input", "tap", "270", "600"), str(taps))

# replay onto the same size -> untouched
calls.clear()
asyncio.run(server.run_macro_bg("DEV_A", macro, 1))
taps = [x for x in calls if "tap" in x]
check("replay on same-size device is unscaled",
      taps and taps[0] == ("DEV_A", "shell", "input", "tap", "540", "1200"), str(taps))

print()
print("=== macro: v1 backward compatibility ===")
json.dump([{"type": "tap", "x": 10, "y": 20, "timestamp": 0}],
          open(os.path.join(WORK, "macros", "legacy.json"), "w"))
old = server.read_macro("legacy")
check("v1 bare list is upgraded on read", old["version"] == 1 and len(old["events"]) == 1, str(old))
calls.clear()
asyncio.run(server.run_macro_bg("DEV_C", old, 1))
check("v1 replays unscaled (no resolution recorded)",
      calls == [("DEV_C", "shell", "input", "tap", "10", "20")], str(calls))

print()
print("=== macro: delete + path traversal ===")
r = c.delete("/api/macros/legacy")
check("delete 200", r.status_code == 200, r.text)
check("file actually gone", not os.path.exists(os.path.join(WORK, "macros", "legacy.json")))
check("delete missing -> 404", c.delete("/api/macros/nope").status_code == 404)

before = sorted(os.listdir(os.path.join(WORK, "macros")))
for evil in ["..%2f..%2fserver", "....//server"]:
    r = c.delete(f"/api/macros/{evil}")
    check(f"traversal delete rejected ({evil})", r.status_code in (400, 404), f"{r.status_code} {r.text}")
r = c.get("/api/macros/..%2f..%2fserver")
check("traversal read rejected", r.status_code in (400, 404), f"{r.status_code} {r.text}")
check("macros dir untouched by traversal attempts",
      sorted(os.listdir(os.path.join(WORK, "macros"))) == before)
check("server.py still there", os.path.exists(os.path.join(ROOT, "server.py")))

print()
print("=== app control ===")
calls.clear()
r = c.post("/api/app/DEV_A/launch", json={"package": "com.example.app"})
check("launch 200", r.status_code == 200, r.text)
check("launch uses monkey argv",
      calls == [("DEV_A", "shell", "monkey", "-p", "com.example.app", "-c",
                 "android.intent.category.LAUNCHER", "1")], str(calls))
calls.clear()
c.post("/api/app/DEV_A/stop", json={"package": "com.example.app"})
check("force-stop argv", calls == [("DEV_A", "shell", "am", "force-stop", "com.example.app")], str(calls))
calls.clear()
c.post("/api/app/DEV_A/clear", json={"package": "com.example.app"})
check("pm clear argv", calls == [("DEV_A", "shell", "pm", "clear", "com.example.app")], str(calls))

calls.clear()
r = c.post("/api/app/DEV_A/launch", json={"package": "com.example.app; rm -rf /"})
check("package with shell metachars -> 400", r.status_code == 400, r.text)
check("bad package never reached adb", calls == [], str(calls))
r = c.post("/api/app/DEV_A/nuke", json={"package": "com.example.app"})
check("unknown action -> 400", r.status_code == 400, r.text)

print()
print("=== logcat: crash detection ===")
check("java crash detected", server.find_crash(
    "01-01 00:00:00.000 E/AndroidRuntime(123): FATAL EXCEPTION: main") == "java")
check("native crash detected", server.find_crash(
    "F/libc (17157): Fatal signal 11 (SIGSEGV), code 1") == "native")
check("ANR detected", server.find_crash(
    "E/ActivityManager: ANR in com.example.app (com.example.app/.MainActivity)") == "anr")
check("ordinary line is not a crash", server.find_crash(
    "I/Choreographer: Skipped 30 frames!") is None)

class FakeStream:
    def __init__(self, lines): self.lines = list(lines)
    async def readline(self):
        return self.lines.pop(0).encode() if self.lines else b""
class FakeProc:
    """Mimics asyncio.subprocess.Process closely enough for reap_logcat."""
    def __init__(self, lines):
        self.stdout = FakeStream(lines); self.killed = False; self.returncode = None
    def terminate(self): self.killed = True; self.returncode = -15
    def kill(self): self.killed = True; self.returncode = -9
    async def wait(self): return self.returncode

LINES = [
    "01-01 00:00:01.000 I/Init: boot\n",
    "01-01 00:00:02.000 E/AndroidRuntime(99): FATAL EXCEPTION: main\n",
    "01-01 00:00:03.000 I/Choreographer: Skipped 30 frames!\n",
    "01-01 00:00:04.000 E/ActivityManager: ANR in com.example.app\n",
]
from collections import deque
proc = FakeProc(LINES)
sess = {"proc": proc, "lines": deque(maxlen=100), "crashes": [], "started": time.time(), "level": "V"}
asyncio.run(server.pump_logcat("DEV_A", sess))
server.logcat_sessions["DEV_A"] = sess
class FakeTask:
    def __init__(self): self.cancelled = False
    def cancel(self): self.cancelled = True
    def done(self): return self.cancelled
sess["task"] = FakeTask()

check("all lines buffered", len(sess["lines"]) == 4, str(len(sess["lines"])))
check("two crashes flagged", len(sess["crashes"]) == 2, str(sess["crashes"]))
check("crash kinds correct", [x["kind"] for x in sess["crashes"]] == ["java", "anr"], str(sess["crashes"]))

r = c.get("/api/logcat/DEV_A")
check("logcat read 200", r.status_code == 200, r.text)
check("logcat returns crashes", len(r.json()["crashes"]) == 2, r.text)
r = c.get("/api/logcat/DEV_A?contains=ANR")
check("contains filter works", r.json()["matched"] == 1, r.text)
r = c.get("/api/logcat/DEV_A?tail=2")
check("tail caps output", len(r.json()["lines"]) == 2, r.text)
# The dashboard warns "버퍼 가득" by comparing total against this, so a read that
# omits it would silently drop the warning on an all-day capture.
check("read reports the buffer capacity",
      r.json()["capacity"] == server.LOGCAT_MAX_LINES, r.text)
r = c.get("/api/logcat/DEV_A/download")
check("download is a text attachment",
      r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""),
      f"{r.status_code} {r.headers}")
check("download body has all lines", r.text.count("\n") == 4, repr(r.text[:120]))
r = c.get("/api/logcat")
check("status lists the session", r.json()["sessions"]["DEV_A"]["crashes"] == 2, r.text)
r = c.get("/api/logcat/DEV_B")
check("reading a non-capturing device -> 404", r.status_code == 404, r.text)
r = c.post("/api/logcat/DEV_A/stop")
check("stop reports counts", r.json().get("lines") == 4 and r.json().get("crashes") == 2, r.text)
check("stop terminated the process", proc.killed)

# Stop, then collect, is the obvious order -- and popping the session on stop
# made the buffer and the download 404 the instant you stopped, so a run without
# to_file threw away everything it had just captured.
r = c.get("/api/logcat/DEV_A")
check("buffer survives stop", r.status_code == 200 and len(r.json()["lines"]) == 4, r.text)
check("stopped session reports capturing false", r.json()["capturing"] is False, r.text)
check("crashes survive stop", len(r.json()["crashes"]) == 2, r.text)
r = c.get("/api/logcat/DEV_A/download")
check("download survives stop", r.status_code == 200 and r.text.count("\n") == 4,
      f"{r.status_code} {r.text[:80]}")
r = c.get("/api/logcat")
check("status marks it not capturing",
      c.get("/api/logcat").json()["sessions"]["DEV_A"]["capturing"] is False, r.text)

r = c.post("/api/logcat/DEV_A/stop")
check("stopping twice is harmless", r.status_code == 200, r.text)
check("stopping twice says not capturing",
      r.json().get("message") == "Not capturing", r.text)

r = c.post("/api/logcat/DEV_A/start", json={"level": "Z"})
check("invalid level -> 400", r.status_code == 400, r.text)

print()
print("=== batch ===")
calls.clear()
r = c.post("/api/batch/input", json={"serials": ["DEV_A", "DEV_B", "DEV_C"],
                                     "event": {"type": "tap", "x": 1, "y": 2}})
b = r.json()
check("batch input all succeeded", b["status"] == "success" and b["succeeded"] == 3, r.text)
check("batch hit every device", sorted(x[0] for x in calls) == ["DEV_A", "DEV_B", "DEV_C"], str(calls))

FAILING.add("DEV_B")
calls.clear()
r = c.post("/api/batch/input", json={"serials": ["DEV_A", "DEV_B", "DEV_C"],
                                     "event": {"type": "tap", "x": 1, "y": 2}})
b = r.json()
check("one bad device -> partial", b["status"] == "partial", r.text)
check("partial counts right", b["succeeded"] == 2 and b["failed"] == 1, r.text)
bad = [x for x in b["results"] if x["status"] == "error"]
check("failure names the device", bad and bad[0]["serial"] == "DEV_B", str(bad))
check("healthy devices still ran", sorted(x[0] for x in calls) == ["DEV_A", "DEV_C"], str(calls))
FAILING.clear()

calls.clear()
r = c.post("/api/batch/app", json={"serials": ["DEV_A", "DEV_C"],
                                   "action": "clear", "package": "com.example.app"})
check("batch app 200", r.json()["succeeded"] == 2, r.text)
check("batch app argv", sorted(calls) == [
    ("DEV_A", "shell", "pm", "clear", "com.example.app"),
    ("DEV_C", "shell", "pm", "clear", "com.example.app")], str(calls))

r = c.post("/api/batch/app", json={"serials": ["DEV_A"], "action": "clear", "package": "bad pkg;x"})
check("batch app validates package", r.status_code == 400, r.text)

r = c.post("/api/batch/input", json={"serials": [], "event": {"type": "tap", "x": 1, "y": 2}})
check("empty serials -> 400", r.status_code == 400, r.text)

calls.clear()
r = c.post("/api/batch/macro", json={"serials": ["DEV_B", "DEV_C"], "name": "login_flow", "count": 1})
check("batch macro started on both", r.json()["succeeded"] == 2, r.text)
r = c.post("/api/batch/macro", json={"serials": ["DEV_A"], "name": "nope"})
check("batch macro missing -> 404", r.status_code == 404, r.text)
r = c.post("/api/batch/macro", json={"serials": ["DEV_A"], "name": "../../server"})
check("batch macro traversal -> 400", r.status_code == 400, r.text)

import io
calls.clear()
r = c.post("/api/batch/install",
           data={"serials": "DEV_A,DEV_C"},
           files={"file": ("app.apk", io.BytesIO(b"fake apk"), "application/vnd.android.package-archive")})
b = r.json()
check("batch install 2 devices", b.get("succeeded") == 2, r.text)
check("install argv per device",
      sorted(x[:5] for x in calls) == [("DEV_A", "install", "-r", "temp_batch_app.apk"),
                                       ("DEV_C", "install", "-r", "temp_batch_app.apk")], str(calls))
check("temp apk cleaned up", not any(f.startswith("temp_batch") for f in os.listdir(WORK)), str(os.listdir(WORK)))

print()
print("=== batch respects device leases ===")
server.device_leases.clear()
c.post("/api/device/DEV_B/occupy", json={"owner": "ci-smoke", "ttl_seconds": 300})

calls.clear()
r = c.post("/api/batch/app", json={"serials": ["DEV_A", "DEV_B", "DEV_C"],
                                   "action": "stop", "package": "com.example.app"})
b = r.json()
check("leased device makes batch partial", b["status"] == "partial", r.text)
check("leased device is skipped, not failed",
      b["skipped"] == 1 and b["failed"] == 0 and b["succeeded"] == 2, r.text)
sk = [x for x in b["results"] if x["status"] == "skipped"]
check("skip names the holder", sk and sk[0]["serial"] == "DEV_B" and "ci-smoke" in sk[0]["message"], str(sk))
check("held device was never touched", sorted(x[0] for x in calls) == ["DEV_A", "DEV_C"], str(calls))

calls.clear()
r = c.post("/api/batch/app", json={"serials": ["DEV_A", "DEV_B"], "action": "stop",
                                   "package": "com.example.app", "owner": "ci-smoke"})
b = r.json()
check("lease holder may drive its own device", b["status"] == "success" and b["succeeded"] == 2, r.text)
check("holder's batch reached both", sorted(x[0] for x in calls) == ["DEV_A", "DEV_B"], str(calls))

calls.clear()
r = c.post("/api/batch/input", json={"serials": ["DEV_B"], "event": {"type": "tap", "x": 1, "y": 2}})
check("batch input honours the lease too", r.json()["skipped"] == 1, r.text)
check("no input reached the held device", calls == [], str(calls))

r = c.post("/api/batch/macro", json={"serials": ["DEV_B"], "name": "login_flow"})
check("batch macro honours the lease", r.json()["skipped"] == 1, r.text)

r = c.post("/api/batch/install",
           data={"serials": "DEV_B"},
           files={"file": ("app.apk", io.BytesIO(b"x"), "application/octet-stream")})
check("batch install honours the lease", r.json()["skipped"] == 1, r.text)

# Single-device app control already enforced it; confirm the shape the UI reads.
r = c.post("/api/app/DEV_B/stop", json={"package": "com.example.app"})
check("single app action on held device -> 409", r.status_code == 409, r.text)
check("409 body carries owner for the UI message", r.json().get("owner") == "ci-smoke", r.text)
server.device_leases.clear()

print()
print("=== adb discovery ===")
import shutil as _sh
real_which = _sh.which
real_exists = server.os.path.exists

def bundled(answer):
    """Answer for scrcpy_bin/ only; everything else keeps the real filesystem.

    Whether the checkout running these tests happens to contain a bundled adb is
    not this test's subject, and reading the real directory made the no-bundled
    case pass or fail depending on the developer's working tree.
    """
    def exists(path):
        if os.path.basename(os.path.dirname(path)) == "scrcpy_bin":
            return answer
        return real_exists(path)
    return exists

server.os.path.exists = bundled(False)
server.shutil.which = lambda n: "/usr/local/bin/adb" if n == "adb" else None
check("falls back to PATH when no bundled adb",
      server.get_adb_path() in ("/usr/local/bin/adb",), server.get_adb_path())
server.shutil.which = lambda n: None
# adbutils carries an adb of its own on some platforms, and that is tried before
# giving up. It is a real answer, so accept it here; the no-adb-anywhere case is
# the check below it.
p = server.get_adb_path()
check("falls back to the adb adbutils ships, when it has one",
      p == "adb" or p.endswith("adb.exe") or "adbutils" in p, p)

# Nothing bundled, nothing on PATH, and adbutils has no copy either: the answer
# has to stay a plain platform-appropriate guess rather than a broken path, so
# adb_binary_info() can report it as missing.
import adbutils as _adbutils
_real_adb_path = _adbutils.adb_path
_adbutils.adb_path = lambda: "/nonexistent/adb"
p = server.get_adb_path()
check("last-resort path is platform-appropriate",
      p.endswith("adb.exe") if os.name == "nt" else p == "adb", p)
_adbutils.adb_path = _real_adb_path

# A bundled copy wins so a host can pin a known adb version.
server.os.path.exists = bundled(True)
server.shutil.which = lambda n: "/usr/local/bin/adb"
check("bundled adb beats PATH",
      os.path.basename(os.path.dirname(server.get_adb_path())) == "scrcpy_bin",
      server.get_adb_path())

server.os.path.exists = real_exists
server.shutil.which = real_which

print()
print("=== unusable devices are surfaced, not dropped ===")
class Info:
    def __init__(self, serial, state): self.serial, self.state = serial, state
# DEV_A online; DEV_X waiting for the RSA prompt; DEV_Y dropped off the bus.
server.adb.list = lambda: [Info("DEV_A", "device"), Info("DEV_X", "unauthorized"),
                           Info("DEV_Y", "offline")]
class Props(dict):
    def get(self, k, d=None): return dict.get(self, k, d) or d
def fake_device(serial=None):
    d = FakeDev(serial)
    d.prop = Props({"ro.product.model": "Pixel", "ro.build.version.release": "15",
                    "ro.build.version.sdk": "35"})
    d.shell = lambda cmd: ("Physical size: 1080x2400" if "wm size" in cmd else "")
    d.get_serial_no = lambda: serial
    return d
server.adb.device_list = lambda: [fake_device("DEV_A")]
server.adb.device = fake_device

r = c.get("/api/devices")
devs = {d["serial"]: d for d in r.json()["devices"]}
check("online device still listed", "DEV_A" in devs and devs["DEV_A"]["state"] == "device", r.text)
check("unauthorized device is listed", "DEV_X" in devs, list(devs))
check("unauthorized carries its state", devs.get("DEV_X", {}).get("state") == "unauthorized", r.text)
check("unauthorized carries a human hint",
      "승인" in (devs.get("DEV_X", {}).get("state_hint") or ""), str(devs.get("DEV_X")))
check("offline device is listed with hint",
      devs.get("DEV_Y", {}).get("state") == "offline" and devs["DEV_Y"]["state_hint"], r.text)

r = c.get("/api/health")
h = r.json()
check("health counts usable separately", h["devices_total"] == 1, r.text)
check("health reports unusable count", h["devices_unusable"] == 2, r.text)
check("health names the unusable states",
      h["unusable"] == {"DEV_X": "unauthorized", "DEV_Y": "offline"}, r.text)
check("health exposes resolved adb path", bool(h.get("adb_path")), r.text)

print()
print("=== device info reports real memory ===")
def mem_device(serial=None):
    d = fake_device(serial)
    d.shell = lambda cmd: ("MemTotal:        7929876 kB\nMemFree: 100 kB"
                           if "meminfo" in cmd else "")
    d.app_current = lambda: None
    return d
server.adb.device = mem_device
r = c.get("/api/info/DEV_A")
mem = r.json()["info"]["memory"]
check("memory is a real number, not a placeholder", mem == "7.6 GB", mem)
def nomem_device(serial=None):
    d = fake_device(serial)
    d.shell = lambda cmd: ""
    d.app_current = lambda: None
    return d
server.adb.device = nomem_device
check("unreadable memory says Unknown, not Checking...",
      c.get("/api/info/DEV_A").json()["info"]["memory"] == "Unknown")

print()
print("=== removed duplicate alias endpoint ===")
check("dead /api/aliases is gone", c.post("/api/aliases", json={"serial": "DEV_A", "alias": "x"}).status_code == 404)
server.adb.device = fake_device
r = c.post("/api/alias/DEV_A", json={"name": "메인폰"})
check("surviving alias endpoint works", r.status_code == 200 and r.json()["alias"] == "메인폰", r.text)

print()
print("=== logcat can also stream to disk for long runs ===")
# The memory buffer is a fixed 20k lines, so an overnight capture loses its
# beginning -- usually where the fault is.
sess = {"proc": None, "lines": deque(maxlen=100), "crashes": [], "started": time.time(),
        "level": "V", "task": None, "ended": False,
        "file": None, "path": None}
logpath = os.path.join(WORK, "capture.txt")
sess["file"] = open(logpath, "w", encoding="utf-8")
sess["proc"] = FakeProc(["01-01 00:00:01 I/A: one\n",
                         "01-01 00:00:02 E/AndroidRuntime: FATAL EXCEPTION: main\n",
                         "01-01 00:00:03 I/B: three\n"])
asyncio.run(server.pump_logcat("FILE_1", sess))
sess["file"].close()
written = open(logpath, encoding="utf-8").read().splitlines()
check("every line reached the file", len(written) == 3, str(written))
check("file content matches the buffer", written == list(sess["lines"]), str(written))
check("crash still detected while writing", len(sess["crashes"]) == 1, str(sess["crashes"]))

# A failing file handle must not take the capture down with it.
class BrokenFile:
    closed = False
    def write(self, _): raise OSError("disk full")
    def close(self): self.closed = True
sess2 = {"proc": FakeProc(["01-01 00:00:01 I/A: still captured\n"]),
         "lines": deque(maxlen=100), "crashes": [], "started": time.time(),
         "level": "V", "task": None, "ended": False, "file": BrokenFile(), "path": "x"}
asyncio.run(server.pump_logcat("FILE_2", sess2))
check("a failing file does not stop the capture", list(sess2["lines"]) == ["01-01 00:00:01 I/A: still captured"],
      str(list(sess2["lines"])))
check("the broken handle is dropped", sess2["file"] is None)

# Default stays off: no file unless asked for.
check("to_file defaults to off", server.LogcatStartRequest().to_file is False)
check("log directory name is defined", server.LOGCAT_DIR == "logs")

print()
print("=== access token gates the API when one is configured ===")
# Off by default so a local checkout still just runs.
check("no token configured means open", server.FARM_TOKEN == "" and c.get("/api/devices").status_code == 200)

server.FARM_TOKEN = "s3cret"
try:
    check("API rejects a request with no token", c.get("/api/devices").status_code == 401)
    check("API rejects a wrong token",
          c.get("/api/devices", headers={"X-Farm-Token": "nope"}).status_code == 401)
    check("header token is accepted",
          c.get("/api/devices", headers={"X-Farm-Token": "s3cret"}).status_code == 200)
    check("query token is accepted", c.get("/api/devices?token=s3cret").status_code == 200)
    check("401 body explains what to do",
          "X-Farm-Token" in c.get("/api/devices").json()["message"], c.get("/api/devices").text)

    # The dashboard has to load before it can ask for a token, and monitoring
    # should not need the secret just to see the farm is alive.
    check("dashboard page stays reachable", c.get("/").status_code == 200)
    check("health stays reachable", c.get("/api/health").status_code == 200)
    check("static assets stay reachable", c.get("/static/jmuxer.min.js").status_code == 200)

    # Writes must be gated too, not just reads.
    check("state-changing call is rejected without a token",
          c.post("/api/device/DEV_A/occupy", json={"owner": "x"}).status_code == 401)
    check("state-changing call works with a token",
          c.post("/api/device/DEV_A/occupy", json={"owner": "x"},
                 headers={"X-Farm-Token": "s3cret"}).status_code == 200)
    c.post("/api/device/DEV_A/release", json={"owner": "x"}, headers={"X-Farm-Token": "s3cret"})

    # A near-miss must not be accepted; compare_digest also keeps it constant-time.
    check("prefix of the token is rejected",
          c.get("/api/devices", headers={"X-Farm-Token": "s3cre"}).status_code == 401)
    check("token with trailing space is rejected",
          c.get("/api/devices", headers={"X-Farm-Token": "s3cret "}).status_code == 401)

    # HTTP middleware never sees a websocket scope; the sockets check themselves.
    class FakeWS:
        def __init__(self, qs=None, hdr=None):
            self.query_params = qs or {}
            self.headers = hdr or {}
    check("websocket without a token is refused", not server.ws_token_ok(FakeWS()))
    check("websocket with the query token is allowed",
          server.ws_token_ok(FakeWS(qs={"token": "s3cret"})))
    check("websocket with a wrong token is refused",
          not server.ws_token_ok(FakeWS(qs={"token": "bad"})))
finally:
    server.FARM_TOKEN = ""
check("websockets are open again once no token is set", server.ws_token_ok(object()))

print()
print("=== leases survive a restart ===")
# Restarting the farm used to free every device, which is worst exactly when it
# matters: the CI job holding one is still running.
server.device_leases.clear()
c.post("/api/device/DEV_A/occupy", json={"owner": "ci-long", "ttl_seconds": 300})
check("lease file written", os.path.exists(server.LEASE_FILE))
saved = json.load(open(server.LEASE_FILE, encoding="utf-8"))
check("file holds the owner", saved["DEV_A"]["owner"] == "ci-long", str(saved))

server.device_leases.clear()                 # simulate a process restart
server.load_leases()
check("lease restored after restart", server.get_lease("DEV_A") is not None)
check("owner survived", server.device_leases["DEV_A"]["owner"] == "ci-long")
r = c.post("/api/device/DEV_A/occupy", json={"owner": "someone-else"})
check("restored lease still blocks others", r.status_code == 409, r.text)

# A lease that ran out while the server was down must not park the device.
json.dump({"GHOST": {"owner": "gone", "expires_at": time.time() - 60},
           "LIVE": {"owner": "here", "expires_at": time.time() + 300}},
          open(server.LEASE_FILE, "w", encoding="utf-8"))
server.device_leases.clear()
server.load_leases()
check("expired lease is not restored", "GHOST" not in server.device_leases, str(server.device_leases))
check("live lease is restored", "LIVE" in server.device_leases, str(server.device_leases))

json.dump({"junk": "not a dict"}, open(server.LEASE_FILE, "w", encoding="utf-8"))
server.device_leases.clear(); server.load_leases()
check("malformed entries are skipped, not fatal", server.device_leases == {}, str(server.device_leases))
open(server.LEASE_FILE, "w").write("{ broken")
server.device_leases.clear(); server.load_leases()
check("corrupt lease file does not stop startup", server.device_leases == {})
c.post("/api/device/DEV_A/release", json={"owner": "ci-long"})
server.device_leases.clear(); server.save_leases()

print()
print("=== stale stream server cleanup targets only scrcpy ===")
# The device-side scrcpy server shows up as a generic `app_process`; killing by
# that name would take unrelated processes with it.
PS_OUTPUT = "\n".join([
    "  PID ARGS",
    " 1001 app_process / com.android.somethingelse",
    " 1002 sh -c CLASSPATH=/data/local/tmp/scrcpy-server.jar nohup app_process / "
    "com.genymobile.scrcpy.Server 1.19-ws6 web ERROR 8886 true",
    " 1003 app_process / com.genymobile.scrcpy.Server 1.19-ws6 web ERROR 8886 true",
    " 1004 /system/bin/surfaceflinger",
])
class PsDev:
    serial = "PS_1"
    def __init__(self): self.killed = []
    def shell(self, cmd):
        if cmd.startswith("ps "): return PS_OUTPUT
        if cmd.startswith("kill "): self.killed.append(cmd.split()[1]); return ""
        return ""
psdev = PsDev()
server.adb.device = lambda serial=None: psdev
r = c.post("/api/device/PS_1/reset-stream")
check("reset-stream 200", r.status_code == 200, r.text)
check("killed exactly the two scrcpy pids", sorted(psdev.killed) == ["1002", "1003"], str(psdev.killed))
check("unrelated app_process untouched", "1001" not in psdev.killed, str(psdev.killed))
check("system process untouched", "1004" not in psdev.killed, str(psdev.killed))
check("response reports what it killed", r.json()["killed"] == [1002, 1003], r.text)

class EmptyPsDev(PsDev):
    def shell(self, cmd):
        return "  PID ARGS\n 1004 /system/bin/surfaceflinger" if cmd.startswith("ps ") else ""
server.adb.device = lambda serial=None: EmptyPsDev()
r = c.post("/api/device/PS_1/reset-stream")
check("nothing to kill is a clean success", r.status_code == 200 and r.json()["killed"] == [], r.text)
server.adb.device = fake_device

print()
print("=== device detail is cached so the 2s poll stays cheap ===")
# Measured on real hardware, interrogating one device over adb costs 0.8-2.6s;
# the dashboard polls every 2s, so uncached this is slower than the poll itself
# and grows with device count.
class CountingDev:
    def __init__(self, serial):
        self.serial = serial
        self.shell_calls = 0
        self._props = Props({"ro.product.model": "CacheTest",
                             "ro.build.version.release": "14",
                             "ro.build.version.sdk": "34"})
    @property
    def prop(self):
        self.shell_calls += 1          # adbutils issues getprop over adb
        return self._props
    def shell(self, cmd):
        self.shell_calls += 1
        if "wm size" in cmd: return "Physical size: 1080x2400"
        if "battery" in cmd: return "  level: 55"
        if "ip addr" in cmd: return "    inet 10.0.0.9/24 brd 10.0.0.255 scope global wlan0"
        return ""

server.device_cache.clear()
cd = CountingDev("CACHE_1")
server.adb.list = lambda: [Info("CACHE_1", "device")]
server.adb.device_list = lambda: [cd]

first = c.get("/api/devices").json()["devices"][0]
after_first = cd.shell_calls
check("first poll reads the device", after_first > 0, str(after_first))
check("first poll returns real values",
      first["model"] == "CacheTest" and first["width"] == 1080 and first["battery"] == "55%"
      and first["ip"] == "10.0.0.9", str(first))

for _ in range(10):
    c.get("/api/devices")
check("ten more polls cost no extra adb calls", cd.shell_calls == after_first,
      f"{after_first} -> {cd.shell_calls}")
again = c.get("/api/devices").json()["devices"][0]
check("cached values match the first read",
      (again["model"], again["width"], again["battery"], again["ip"])
      == (first["model"], first["width"], first["battery"], first["ip"]), str(again))

before_refresh = cd.shell_calls
c.get("/api/devices?refresh=1")
check("?refresh=1 forces a re-read", cd.shell_calls > before_refresh,
      f"{before_refresh} -> {cd.shell_calls}")

# Battery has a short TTL; the immutable fields must not be re-read with it.
server.device_cache["CACHE_1"]["battery"] = ("55%", 0.0)   # expire it
static_before = server.device_cache["CACHE_1"]["static"]
c.get("/api/devices")
check("expired battery does not invalidate static fields",
      server.device_cache["CACHE_1"]["static"] is static_before)

# An unplugged device must not leave stale values behind for its serial.
server.adb.list = lambda: []
server.adb.device_list = lambda: []
c.get("/api/devices")
check("cache is dropped when the device goes away", "CACHE_1" not in server.device_cache,
      str(list(server.device_cache)))

print()
print("=== health reports which adb server is answering ===")
server.adb.list = lambda: [Info("DEV_A", "device")]
server.adb.device_list = lambda: [fake_device("DEV_A")]
server.adb.server_version = lambda: 41
h = c.get("/api/health").json()
check("modern server version reported", h["adb_server"]["version"] == 41, str(h.get("adb_server")))
check("no warning for a current server", "note" not in h["adb_server"], str(h["adb_server"]))
server.adb.server_version = lambda: 40
h = c.get("/api/health").json()
check("old server version reported", h["adb_server"]["version"] == 40, str(h["adb_server"]))
check("old server carries an explanation", "note" in h["adb_server"], str(h["adb_server"]))
def boom(): raise RuntimeError("no server")
server.adb.server_version = boom
h = c.get("/api/health").json()
check("unreachable adb server does not break health",
      h["adb_server"]["version"] is None and "note" in h["adb_server"], str(h["adb_server"]))
server.adb.server_version = lambda: 41

print()
print("=== health notices when the adb binary is missing ===")
# adbutils reaches the adb server over TCP, so the device list and screenshots
# work with no adb binary on the machine at all -- while input, app control,
# install, logcat and wireless every one fail with a bare "file not found".
# Reporting only the path let the farm answer "ok" in exactly that state.
real_get_adb_path = server.get_adb_path
server.get_adb_path = lambda: os.path.join(WORK, "nowhere", "adb.exe")
h = c.get("/api/health").json()
check("missing binary is flagged", h["adb_binary"]["ok"] is False, str(h.get("adb_binary")))
check("missing binary degrades status", h["status"] == "degraded", str(h["status"]))
check("missing binary is still adb-server-reachable", h["adb"] == "ok", str(h["adb"]))
check("missing binary explains the blast radius",
      "input" in h["adb_binary"].get("note", ""), str(h["adb_binary"]))
check("missing binary still reports the path it tried",
      h["adb_path"] == h["adb_binary"]["path"], str(h["adb_binary"]))

present = os.path.join(WORK, "adb.exe")
open(present, "wb").close()
server.get_adb_path = lambda: present
h = c.get("/api/health").json()
check("present binary is ok", h["adb_binary"]["ok"] is True, str(h["adb_binary"]))
check("present binary keeps status ok", h["status"] == "ok", str(h["status"]))
check("present binary carries no note", "note" not in h["adb_binary"], str(h["adb_binary"]))
server.get_adb_path = real_get_adb_path

print()
print("=== wirelessly attached devices stay in the list ===")
# A wireless serial contains dots. The old IP heuristic branched on that and
# called a method adbutils does not have, so the exception dropped the device
# from /api/devices entirely -- including devices this farm's own /api/wireless
# had just switched over.
class WirelessDev:
    """Deliberately lacks get_serial_no(), like the real adbutils AdbDevice."""
    def __init__(self, serial, wlan_ip=None):
        self.serial = serial
        self._wlan = wlan_ip
        self.prop = Props({"ro.product.model": "TabX", "ro.build.version.release": "14",
                           "ro.build.version.sdk": "34"})
    def shell(self, cmd):
        if "wm size" in cmd: return "Physical size: 1600x2560"
        if "ip addr" in cmd:
            if not self._wlan: raise RuntimeError("no wlan0")
            return f"    inet {self._wlan}/24 brd 192.168.0.255 scope global wlan0"
        if "battery" in cmd: return "  level: 77"
        return ""

for serial, wlan, expect_ip, label in [
    ("192.168.0.5:5555", None, "192.168.0.5", "adb connect 시리얼"),
    ("adb-HA2F2NVC-xYKOdg._adb-tls-connect._tcp", "192.168.0.170", "192.168.0.170", "mDNS 시리얼"),
    ("R3CN30ABCDE", "192.168.0.20", "192.168.0.20", "USB + wlan0 있음"),
    ("R3CN30ABCDE", None, "USB", "USB + wlan0 없음"),
]:
    # Each row stands for a different device; two of them deliberately reuse a
    # serial to vary only the wlan0 answer, so clear the detail cache between
    # them rather than letting one case serve the previous one's IP.
    server.device_cache.clear()
    server.adb.list = lambda s=serial: [Info(s, "device")]
    server.adb.device_list = lambda s=serial, w=wlan: [WirelessDev(s, w)]
    body = c.get("/api/devices").json()["devices"]
    found = [d for d in body if d["serial"] == serial]
    check(f"{label}: 목록에 남아있음", len(found) == 1, str(body))
    if found:
        check(f"{label}: ip={expect_ip}", found[0]["ip"] == expect_ip, str(found[0]["ip"]))
        check(f"{label}: 해상도 읽힘", found[0]["width"] == 1600 or serial.startswith("R3"), str(found[0]))

print()
print("=== one phone on two adb transports is one device ===")
# Android 11+ wireless debugging advertises the device over mDNS, so adb lists
# the same phone twice. Measured on a real tablet: ci-A claimed HA2F2NVC, then
# ci-B asked for "any free device" and was handed adb-HA2F2NVC-...._tcp -- two
# jobs driving one screen, which is the one thing leases exist to prevent.
USB = "HA2F2NVC"
MDNS = "adb-HA2F2NVC-xYKOdg._adb-tls-connect._tcp"
server.device_leases.clear()
server.device_cache.clear()
server.adb.list = lambda: [Info(USB, "device"), Info(MDNS, "device")]
server.adb.device_list = lambda: [WirelessDev(USB, "192.168.0.170"),
                                  WirelessDev(MDNS, "192.168.0.170")]

body = c.get("/api/devices").json()["devices"]
check("두 transport가 카드 하나로", len(body) == 1, str([d["serial"] for d in body]))
check("USB 시리얼 쪽이 남음", body and body[0]["serial"] == USB, str(body))
h = c.get("/api/health").json()
check("기기 수도 1대", h["devices_total"] == 1, str(h))

r = c.post(f"/api/device/{USB}/occupy", json={"owner": "ci-A", "ttl_seconds": 60})
check("USB 시리얼로 점유 성공", r.json()["status"] == "success", r.text)
r = c.post("/api/devices/occupy", json={"owner": "ci-B", "ttl_seconds": 60})
check("mDNS 쌍둥이를 유휴로 내주지 않음", r.status_code == 409, r.text)

# And the alias must not be a way around the lease when named outright.
r = c.post(f"/api/device/{MDNS}/occupy", json={"owner": "ci-B", "ttl_seconds": 60})
check("mDNS 이름으로도 우회 불가", r.status_code == 409, r.text)
check("409가 실제 점유자를 알려줌", r.json().get("owner") == "ci-A", r.text)
r = c.post(f"/api/device/{MDNS}/input",
           json={"type": "tap", "x": 1, "y": 2, "owner": "ci-B"})
check("mDNS 이름으로 입력도 막힘", r.status_code == 409, r.text)

# Claiming by either name locks both, and releasing by either frees both.
check("점유 항목이 하나로 기록됨", len(c.get("/api/leases").json()["leases"]) == 1,
      c.get("/api/leases").text)
r = c.post(f"/api/device/{MDNS}/release", json={"owner": "ci-A"})
check("mDNS 이름으로 반납 가능", r.json()["status"] == "success", r.text)
check("반납 후 비어 있음", c.get("/api/leases").json()["leases"] == {},
      c.get("/api/leases").text)

# Cable out: the mDNS name is now the only way in, so it must survive.
server.device_cache.clear()
server.adb.list = lambda: [Info(MDNS, "device")]
server.adb.device_list = lambda: [WirelessDev(MDNS, "192.168.0.170")]
body = c.get("/api/devices").json()["devices"]
check("USB가 빠지면 mDNS 기기가 남음",
      len(body) == 1 and body[0]["serial"] == MDNS, str(body))
server.device_leases.clear()

print()
print("=== a device whose details fail is shown, not dropped ===")
class BrokenDev:
    serial = "BROKEN_1"
    @property
    def prop(self): raise RuntimeError("보드가 응답하지 않음")
server.adb.list = lambda: [Info("BROKEN_1", "device")]
server.adb.device_list = lambda: [BrokenDev()]
body = c.get("/api/devices").json()["devices"]
broken = [d for d in body if d["serial"] == "BROKEN_1"]
check("읽기 실패한 기기도 목록에 나타남", len(broken) == 1, str(body))
check("state=error 로 표시", broken and broken[0]["state"] == "error", str(broken))
check("실패 이유가 담김", broken and "응답하지" in (broken[0]["state_hint"] or ""), str(broken))

# restore the standard fakes for the rest of the file
server.adb.list = lambda: [Info("DEV_A", "device")]
server.adb.device_list = lambda: [fake_device("DEV_A")]

print()
print("=== stream port comes from one file, not a hardcoded page ===")
cfgpath = os.path.join(WORK, "ws-scrcpy.config.json")

# No config file -> documented fallback, and no crash.
if os.path.exists(cfgpath):
    os.remove(cfgpath)
check("missing config falls back", server.get_stream_port() == server.FALLBACK_STREAM_PORT,
      str(server.get_stream_port()))

json.dump({"server": [{"secure": False, "port": 8010}]}, open(cfgpath, "w"))
check("port is read from the config file", server.get_stream_port() == 8010, str(server.get_stream_port()))
check("/api/config serves the same value", c.get("/api/config").json() == {"stream_port": 8010},
      c.get("/api/config").text)

# Changing the file is enough -- nothing else needs editing.
json.dump({"server": [{"secure": False, "port": 9123}]}, open(cfgpath, "w"))
check("changing the file changes the answer", c.get("/api/config").json()["stream_port"] == 9123,
      c.get("/api/config").text)

# A secure-only entry must not be handed out as the plain http port.
json.dump({"server": [{"secure": True, "port": 8443, "options": {}}]}, open(cfgpath, "w"))
check("https-only config does not masquerade as the http port",
      server.get_stream_port() == server.FALLBACK_STREAM_PORT, str(server.get_stream_port()))

open(cfgpath, "w").write("{ not json")
check("corrupt config falls back instead of raising",
      server.get_stream_port() == server.FALLBACK_STREAM_PORT, str(server.get_stream_port()))
os.remove(cfgpath)

# The page must not carry its own copy of the port as the real source.
stream_html = open(os.path.join(ROOT, "static", "stream.html"), encoding="utf-8").read()
check("stream.html asks the server for the port", "/api/config" in stream_html)
check("stream.html no longer hardcodes 8000", ":8000" not in stream_html and "port = 8000" not in stream_html,
      "8000 still present")

check("openapi still builds", c.get("/openapi.json").status_code == 200)

print()
print(f"{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
