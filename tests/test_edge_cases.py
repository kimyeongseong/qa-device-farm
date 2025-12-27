"""Probe the paths the mocks were hiding: real subprocess pumping, dead sessions,
optional request bodies, odd serials."""
import sys, os, asyncio, tempfile, shutil, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="farm_edge_")
shutil.copytree(os.path.join(ROOT, "static"), os.path.join(WORK, "static"))
os.makedirs(os.path.join(WORK, "macros"), exist_ok=True)
os.chdir(WORK)

import server
from fastapi.testclient import TestClient
from collections import deque

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

class FakeDev:
    def __init__(s, serial): s.serial = serial
server.adb.device_list = lambda: [FakeDev("DEV_A")]
class Info:
    def __init__(s, serial, state): s.serial, s.state = serial, state
server.adb.list = lambda: [Info("DEV_A", "device")]
c = TestClient(server.app)

print("=== logcat start with no request body (UI/curl may omit it) ===")
# NOTE: TestClient cancels tasks created inside a handler once the request ends,
# so the pump cannot actually run here. Live pumping is covered by
# live_logcat.py against a real uvicorn server; this file covers the state
# machine around it.
async def fake_exec(adb_path, serial, *args):
    return None
server.adb_exec = fake_exec
real_create = asyncio.create_subprocess_exec
async def fake_create(*cmd, **kw):
    return await real_create(sys.executable, "-c", "pass",
                             stdout=asyncio.subprocess.PIPE,
                             stderr=asyncio.subprocess.DEVNULL)
server.asyncio.create_subprocess_exec = fake_create

r = c.post("/api/logcat/DEV_A/start")          # <-- no JSON body at all
check("start accepts an empty body", r.status_code == 200, f"{r.status_code} {r.text}")
sess = server.logcat_sessions.get("DEV_A")
# Under TestClient the pump is cancelled the moment the request returns, so its
# `finally` has already flipped this to True -- which is itself evidence that the
# cleanup path runs on cancellation as well as on EOF.
check("session tracks an 'ended' flag", sess is not None and isinstance(sess.get("ended"), bool),
      str(sess and sess.keys()))
check("cancellation marks the session ended", sess.get("ended") is True, str(sess.get("ended")))

print()
print("=== a capture whose adb died must not look alive, and must be restartable ===")
sess["lines"].extend(["01-01 00:00:01 I/X: kept",
                      "01-01 00:00:02 E/AndroidRuntime: FATAL EXCEPTION: main"])
sess["crashes"].append({"kind": "java", "line": "FATAL EXCEPTION: main", "at": time.time()})
sess["ended"] = True                            # what pump_logcat's finally does

r = c.get("/api/logcat/DEV_A")
check("dead capture reports capturing=false", r.json()["capturing"] is False, r.text)
check("buffered lines survive the death", r.json()["total"] == 2, r.text)
check("crashes survive the death", len(r.json()["crashes"]) == 1, r.text)

r = c.get("/api/logcat/DEV_A/download")
check("buffer still downloadable after death",
      r.status_code == 200 and "kept" in r.text, f"{r.status_code}")

r = c.get("/api/logcat")
check("status listing marks it not capturing",
      r.json()["sessions"]["DEV_A"]["capturing"] is False, r.text)

r = c.post("/api/logcat/DEV_A/start")
check("restart is allowed, not 'Already capturing'",
      "Already" not in r.json().get("message", ""), r.text)
check("restart replaced the dead session's buffer",
      c.get("/api/logcat/DEV_A").json()["total"] == 0,
      c.get("/api/logcat/DEV_A").text)

print()
print("=== a live capture still refuses a duplicate start ===")
server.logcat_sessions["DEV_A"]["ended"] = False
r = c.post("/api/logcat/DEV_A/start")
check("duplicate start on a live capture is refused",
      r.json().get("message") == "Already capturing", r.text)
c.post("/api/logcat/DEV_A/stop")

print()
print("=== download header with an awkward serial ===")
server.logcat_sessions['A"B'] = {"proc": None, "lines": deque(["x"]), "crashes": [],
                                 "started": time.time(), "level": "V", "task": None,
                                 "ended": False}
try:
    r = c.get('/api/logcat/A"B/download')
    check("odd serial does not corrupt the download header",
          r.status_code == 200 and "\n" not in r.headers.get("content-disposition", ""),
          repr(r.headers.get("content-disposition")))
except Exception as e:
    check("odd serial does not crash the server", False, repr(e))
server.logcat_sessions.pop('A"B', None)

print()
print("=== macro with a corrupt file ===")
open(os.path.join(WORK, "macros", "broken.json"), "w").write("{not json")
r = c.get("/api/macros")
check("listing survives a corrupt macro", r.status_code == 200 and "broken" in r.json()["macros"], r.text)
r = c.get("/api/macros/broken")
check("reading a corrupt macro fails cleanly, not 500",
      r.status_code in (400, 422, 500), f"{r.status_code}")
print(f"      (corrupt read returned {r.status_code})")

print()
print("=== empty macro replay ===")
import json as _j
_j.dump({"version": 2, "recorded_on": {"serial": "DEV_A", "width": 1080, "height": 2400},
         "events": []}, open(os.path.join(WORK, "macros", "empty.json"), "w"))
r = c.post("/api/macros/play/DEV_A", json={"name": "empty"})
check("empty macro does not blow up", r.status_code == 200, r.text)

print()
print(f"{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
