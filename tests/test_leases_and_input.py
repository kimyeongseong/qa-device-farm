"""Smoke-test the device-farm endpoints without any real device attached."""
import sys, os, time, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Run in a scratch directory. The server writes runtime state next to its cwd
# (device_leases.json, macros/), so running from the repo would both pollute the
# working tree and let one run inherit the leases of the last one.
os.chdir(tempfile.mkdtemp(prefix="farm_leases_"))

import server
from fastapi.testclient import TestClient

# Pretend two devices are attached. Both adb entry points have to be faked:
# device_list() drives the usable list, and list() drives the attached-but-
# unusable one -- leaving the second real means any device actually plugged
# into the machine running the tests shows up in the results.
class FakeDev:
    def __init__(self, serial): self.serial = serial
class FakeInfo:
    def __init__(self, serial, state): self.serial, self.state = serial, state
server.adb.device_list = lambda: [FakeDev("SERIAL_A"), FakeDev("SERIAL_B")]
server.adb.list = lambda: [FakeInfo("SERIAL_A", "device"), FakeInfo("SERIAL_B", "device")]
server.device_leases.clear()

# Capture adb calls instead of executing them.
calls = []
async def fake_exec(adb_path, serial, *args):
    calls.append((serial,) + args)
server.adb_exec = fake_exec

c = TestClient(server.app)
fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# --- health ---
r = c.get("/api/health")
check("health 200", r.status_code == 200, r.text)
check("health counts 2 free", r.json().get("devices_free") == 2, r.text)

# --- occupy any ---
r = c.post("/api/devices/occupy", json={"owner": "ci-smoke", "ttl_seconds": 60})
check("occupy any -> 200", r.status_code == 200, r.text)
serial = r.json().get("serial")
check("occupy any returns a serial", serial == "SERIAL_A", r.text)

# --- second owner blocked on same device ---
r = c.post(f"/api/device/{serial}/occupy", json={"owner": "someone-else"})
check("occupy held device -> 409", r.status_code == 409, r.text)

# --- same owner re-occupy is fine (idempotent renew) ---
r = c.post(f"/api/device/{serial}/occupy", json={"owner": "ci-smoke"})
check("re-occupy by same owner -> 200", r.status_code == 200, r.text)

# --- occupy any again gets the OTHER device ---
r = c.post("/api/devices/occupy", json={"owner": "ci-perf"})
check("occupy any -> second device", r.json().get("serial") == "SERIAL_B", r.text)

# --- pool exhausted ---
r = c.post("/api/devices/occupy", json={"owner": "ci-third"})
check("pool exhausted -> 409", r.status_code == 409, r.text)

# --- input blocked for non-owner ---
r = c.post(f"/api/device/{serial}/input", json={"type": "tap", "x": 5, "y": 6, "owner": "intruder"})
check("input by non-owner -> 409", r.status_code == 409, r.text)

# --- input allowed for owner ---
calls.clear()
r = c.post(f"/api/device/{serial}/input", json={"type": "tap", "x": 540, "y": 1200, "owner": "ci-smoke"})
check("input by owner -> 200", r.status_code == 200, r.text)
check("tap reached adb as argv", calls == [("SERIAL_A", "shell", "input", "tap", "540", "1200")], str(calls))

# --- injection attempt is rejected, not executed ---
calls.clear()
r = c.post(f"/api/device/{serial}/input", json={"type": "tap", "x": "1 && calc.exe", "y": 2, "owner": "ci-smoke"})
check("injection payload -> 4xx", r.status_code in (400, 422), f"{r.status_code} {r.text}")
check("injection never reached adb", calls == [], str(calls))

# --- swipe / key / text ---
calls.clear()
c.post(f"/api/device/{serial}/input", json={"type": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 250, "owner": "ci-smoke"})
check("swipe argv", calls == [("SERIAL_A", "shell", "input", "swipe", "1", "2", "3", "4", "250")], str(calls))

calls.clear()
c.post(f"/api/device/{serial}/input", json={"type": "key", "keycode": 4, "owner": "ci-smoke"})
check("key argv", calls == [("SERIAL_A", "shell", "input", "keyevent", "4")], str(calls))

calls.clear()
r = c.post(f"/api/device/{serial}/input", json={"type": "text", "text": "한글", "owner": "ci-smoke"})
check("non-ascii text -> 400", r.status_code == 400, r.text)
check("non-ascii never reached adb", calls == [], str(calls))

# --- unknown event type ---
r = c.post(f"/api/device/{serial}/input", json={"type": "explode", "owner": "ci-smoke"})
check("unknown event type -> 400", r.status_code == 400, r.text)

# --- release by wrong owner blocked, right owner works ---
r = c.post(f"/api/device/{serial}/release", json={"owner": "intruder"})
check("release by non-owner -> 409", r.status_code == 409, r.text)
r = c.post(f"/api/device/{serial}/release", json={"owner": "ci-smoke"})
check("release by owner -> 200", r.status_code == 200, r.text)
r = c.post(f"/api/device/{serial}/occupy", json={"owner": "fresh"})
check("released device is re-claimable", r.status_code == 200, r.text)

# --- TTL expiry ---
server.device_leases.clear()
c.post("/api/device/SERIAL_A/occupy", json={"owner": "shortlived", "ttl_seconds": 1})
check("lease active before expiry", server.get_lease("SERIAL_A") is not None)
server.device_leases["SERIAL_A"]["expires_at"] = time.time() - 1
check("lease auto-expires", server.get_lease("SERIAL_A") is None)
r = c.post("/api/device/SERIAL_A/occupy", json={"owner": "next-in-line"})
check("expired device re-claimable", r.status_code == 200, r.text)

# --- leases listing ---
r = c.get("/api/leases")
check("leases lists holder", r.json()["leases"].get("SERIAL_A", {}).get("owner") == "next-in-line", r.text)

# --- openapi renders ---
check("openapi builds", c.get("/openapi.json").status_code == 200)

print()
print(f"{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
