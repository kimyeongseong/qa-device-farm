"""Virtual devices: AVD discovery, create/delete, start/stop, leases, kind filter.

No SDK is needed. A fake SDK tree is laid out in a temp directory, and the
"emulator" is a small Python script: it either crashes at once (like an
emulator with no hardware acceleration) or idles until it is killed. adb is
faked the same way as in the other suites.
"""
import sys, os, json, time, asyncio, shutil, tempfile, subprocess
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix="farm_vd_")
shutil.copytree(os.path.join(ROOT, "static"), os.path.join(WORK, "static"))
os.makedirs(os.path.join(WORK, "macros"), exist_ok=True)
os.chdir(WORK)

SDK = os.path.join(WORK, "sdk")
AVD_HOME = os.path.join(WORK, "avd")
os.environ["ANDROID_HOME"] = SDK
os.environ["ANDROID_AVD_HOME"] = AVD_HOME

import server
import virtual_devices as vd
from fastapi.testclient import TestClient

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# ---- fake SDK on disk ---------------------------------------------------
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

IMAGE = "system-images;android-34;google_apis;x86_64"
write(os.path.join(SDK, "system-images", "android-34", "google_apis", "x86_64", "source.properties"), "Pkg.Revision=1\n")
write(os.path.join(SDK, "system-images", "android-30", "default", "arm64-v8a", "source.properties"), "Pkg.Revision=1\n")

def make_avd(name, width=1080, height=2400, sysdir="system-images/android-34/google_apis/x86_64/"):
    avd_dir = os.path.join(AVD_HOME, f"{name}.avd")
    write(os.path.join(AVD_HOME, f"{name}.ini"),
          f"avd.ini.encoding=UTF-8\npath={avd_dir}\npath.rel=avd/{name}.avd\ntarget=android-34\n")
    write(os.path.join(avd_dir, "config.ini"),
          f"avd.ini.displayname={name.replace('_', ' ')}\nhw.lcd.width={width}\nhw.lcd.height={height}\n"
          f"hw.device.name=pixel_6\nhw.ramSize=2048M\nimage.sysdir.1={sysdir}\n"
          "tag.id=google_apis\nabi.type=x86_64\n")

make_avd("Pixel_6_API_34")
make_avd("Crash_API_34")
make_avd("Win_Paths", sysdir="system-images\\android-30\\default\\arm64-v8a\\")
# An .ini whose disk image directory has gone missing.
write(os.path.join(AVD_HOME, "Orphan.ini"), f"path={os.path.join(AVD_HOME, 'gone.avd')}\n")

# ---- fake emulator --------------------------------------------------------
FAKE_EMU = os.path.join(WORK, "fake_emulator.py")
write(FAKE_EMU, """import sys, time
name = sys.argv[sys.argv.index('-avd') + 1]
print('emulator: argv', ' '.join(sys.argv[1:]), flush=True)
if name.startswith('Crash'):
    # Captured from emulator 37.2 on a host without KVM.
    print('INFO         | Android emulator version 37.2.12.0 (build_id 16428233) (CL:N/A)', flush=True)
    print('INFO         |   Checking: hasSufficientSystem', flush=True)
    print('WARNING      | encryption is off', flush=True)
    print('ERROR        | x86_64 emulation currently requires hardware acceleration!', flush=True)
    print('CPU acceleration status: KVM requires a CPU that supports vmx or svm', flush=True)
    sys.exit(1)
time.sleep(120)
""")

real_build = vd.build_command
argv_seen = []
def fake_build(emulator, name, port, **kw):
    cmd = real_build(emulator, name, port, **kw)
    argv_seen.append(cmd)
    return [sys.executable, FAKE_EMU] + cmd[1:]
vd.build_command = fake_build
vd.emulator_binary = lambda: "/sdk/emulator/emulator"
vd.EARLY_EXIT_GRACE = 0.7
vd.LAUNCH_POLL = 0.05
vd.BOOT_POLL = 0.05

# A real emulator answers `adb emu kill`; the fake one is simply out-waited and
# killed, which exercises the force path. Keep that wait short.
real_stop = vd.stop
vd.stop = lambda name, serial, adb_path, wait=20.0: real_stop(name, serial, adb_path, wait=0.3)

# ---- fake adb -------------------------------------------------------------
class Props(dict):
    def get(self, k, default=""): return dict.get(self, k, default)

class Dev:
    def __init__(self, serial, props=None):
        self.serial = serial
        self.prop = Props(props or {"ro.product.model": "Pixel 7", "ro.build.version.release": "14",
                                    "ro.build.version.sdk": "34"})
    def shell(self, cmd):
        if cmd == "wm size": return "Physical size: 1080x2400"
        if "battery" in cmd: return "  level: 100"
        return ""

class Info:
    def __init__(self, serial, state): self.serial, self.state = serial, state

ADB = {}   # serial -> state ("device" / "offline")
EMU_PROPS = {"ro.product.model": "sdk_gphone64_x86_64", "ro.build.version.release": "14",
             "ro.build.version.sdk": "34", "ro.boot.qemu.avd_name": "Pixel_6_API_34"}
def device_list():
    return [Dev(s, EMU_PROPS if s.startswith("emulator-") else None)
            for s, st in ADB.items() if st == "device"]
server.adb.device_list = device_list
server.adb.list = lambda: [Info(s, st) for s, st in ADB.items()]
ADB["PHONE_1"] = "device"

AVD_OF = {}     # what `adb -s <serial> emu avd name` answers
vd.query_avd_name = lambda adb_path, serial: AVD_OF.get(serial)
BOOTED = set()
vd.is_booted = lambda serial: serial in BOOTED

c = TestClient(server.app)

def avds():
    return {a["name"]: a for a in c.get("/api/avds").json()["avds"]}

def cleanup():
    for name, entry in list(vd.launched.items()):
        try: entry["proc"].kill()
        except Exception: pass
    vd.launched.clear()

try:
    print("=== discovery ===")
    check("sdk root from ANDROID_HOME", vd.sdk_root() == SDK, vd.sdk_root())
    check("avd home from ANDROID_AVD_HOME", vd.avd_home() == AVD_HOME, vd.avd_home())
    listed = {a["name"]: a for a in vd.list_avds()}
    check("every .ini listed", set(listed) == {"Pixel_6_API_34", "Crash_API_34", "Win_Paths", "Orphan"}, list(listed))
    p6 = listed["Pixel_6_API_34"]
    check("api / tag / abi parsed", (p6["api"], p6["tag"], p6["abi"]) == ("34", "google_apis", "x86_64"), str(p6))
    check("screen and profile parsed", (p6["width"], p6["height"], p6["device"]) == (1080, 2400, "pixel_6"), str(p6))
    check("ram with unit suffix parsed", p6["ram_mb"] == 2048, str(p6))
    check("image id rebuilt from sysdir", p6["image"] == IMAGE, p6["image"])
    check("windows-style sysdir parsed", listed["Win_Paths"]["image"] == "system-images;android-30;default;arm64-v8a",
          listed["Win_Paths"]["image"])
    check("missing .avd dir flagged broken", listed["Orphan"]["broken"] and not p6["broken"], str(listed["Orphan"]))

    imgs = c.get("/api/sdk/system-images").json()["images"]
    check("installed system images listed", sorted(i["id"] for i in imgs) ==
          sorted([IMAGE, "system-images;android-30;default;arm64-v8a"]), str(imgs))

    r = c.get("/api/avds")
    check("/api/avds 200", r.status_code == 200, r.text)
    check("all stopped to start with", all(a["status"] == "stopped" for a in r.json()["avds"]), r.text)
    check("sdk info reported", r.json()["sdk"]["sdk_root"] == SDK, r.text)

    print()
    print("=== validation ===")
    for bad in ["-wipe-data", ".hidden", "a b", "x;rm"]:
        check(f"bad AVD name rejected ({bad!r})",
              c.post(f"/api/avds/{bad}/start", json={}).status_code == 400)
    check("unknown AVD -> 404", c.post("/api/avds/Nope/start", json={}).status_code == 404)
    check("broken AVD refused before launching",
          c.post("/api/avds/Orphan/start", json={}).status_code == 409 and not vd.launched)

    cmd = real_build("emu", "Pixel_6_API_34", 5556)
    check("default command: fixed port, no snapshot save, headless",
          cmd == ["emu", "-avd", "Pixel_6_API_34", "-port", "5556", "-no-snapshot-save",
                  "-no-boot-anim", "-no-metrics", "-no-window"], str(cmd))
    cmd = real_build("emu", "X", 5554, headless=False, wipe=True, cold_boot=True, gpu="swiftshader_indirect")
    check("options map to flags",
          "-no-window" not in cmd and "-wipe-data" in cmd and "-no-snapshot-load" in cmd
          and cmd[-2:] == ["-gpu", "swiftshader_indirect"], str(cmd))
    try:
        real_build("emu", "X", 5554, gpu="host -qemu -monitor")
        check("arbitrary gpu value rejected", False)
    except vd.VirtualDeviceError:
        check("arbitrary gpu value rejected", True)
    r = c.post("/api/avds/Pixel_6_API_34/start", json={"gpu": "evil"})
    check("bad gpu -> 400 and nothing launched", r.status_code == 400 and not vd.launched, r.text)

    print()
    print("=== start: early failure ===")
    r = c.post("/api/avds/Crash_API_34/start", json={})
    check("crashing emulator -> 500", r.status_code == 500, r.text)
    check("failure carries the emulator's own words",
          any("hardware acceleration" in ln for ln in r.json().get("log", [])), r.text)
    check("the ERROR line is lifted into the message",
          "requires hardware acceleration!" in r.json().get("message", ""), r.text)
    t0 = time.time()
    c.post("/api/avds/Crash_API_34/start", json={})
    check("instant failure answered without sitting out the grace period",
          time.time() - t0 < vd.EARLY_EXIT_GRACE, f"{time.time() - t0:.2f}s")
    check("crashed launch not left in the table", "Crash_API_34" not in vd.launched)

    print()
    print("=== start / boot / running ===")
    server.device_leases["emulator-5554"] = {"owner": "ghost", "expires_at": time.time() + 999}
    r = c.post("/api/avds/Pixel_6_API_34/start", json={"owner": "ci-vd", "ttl_seconds": 120})
    d = r.json()
    check("start 200", r.status_code == 200, r.text)
    check("serial known up front", d.get("serial") == "emulator-5554" and d.get("port") == 5554, r.text)
    check("state is starting", d.get("state") == "starting", r.text)
    check("started headless by default", "-no-window" in argv_seen[-1], str(argv_seen[-1]))
    check("lease granted in the same call", (server.get_lease("emulator-5554") or {}).get("owner") == "ci-vd",
          str(server.device_leases))
    check("emulator log written", os.path.exists(d.get("log", "")), d.get("log"))
    check("status: starting before adb sees it", avds()["Pixel_6_API_34"]["status"] == "starting")

    ADB["emulator-5554"] = "offline"
    a = avds()["Pixel_6_API_34"]
    check("status: booting once adb lists it (even before console answers)",
          a["status"] == "booting" and a["serial"] == "emulator-5554", str(a))
    devs = {x["serial"]: x for x in c.get("/api/devices").json()["devices"]}
    check("booting emulator shown as virtual with a booting hint",
          devs["emulator-5554"]["virtual"] and devs["emulator-5554"]["state_hint"] == "에뮬레이터 부팅 중",
          str(devs.get("emulator-5554")))
    check("physical device not virtual", devs["PHONE_1"]["virtual"] is False, str(devs["PHONE_1"]))

    ADB["emulator-5554"] = "device"
    AVD_OF["emulator-5554"] = "Pixel_6_API_34"
    check("status: booting until boot_completed", avds()["Pixel_6_API_34"]["status"] == "booting")
    BOOTED.add("emulator-5554")
    a = avds()["Pixel_6_API_34"]
    check("status: running", a["status"] == "running", str(a))
    check("lease shown on the AVD", a["occupied_by"] == "ci-vd", str(a))

    devs = {x["serial"]: x for x in c.get("/api/devices?refresh=1").json()["devices"]}
    e = devs["emulator-5554"]
    check("running emulator is a normal device", e["state"] == "device" and e["virtual"], str(e))
    check("AVD name read from props", e["avd"] == "Pixel_6_API_34", str(e))
    check("alias defaults to the AVD name, not sdk_gphone",
          e["alias"] == "Pixel_6_API_34", str(e))

    r = c.post("/api/avds/Pixel_6_API_34/start", json={"owner": "ci-vd"})
    check("second start is a no-op that returns the serial",
          r.status_code == 200 and r.json().get("already_running") and r.json().get("serial") == "emulator-5554", r.text)
    r = c.post("/api/avds/Pixel_6_API_34/start", json={"owner": "someone-else"})
    check("second start cannot take someone else's lease", r.status_code == 409, r.text)

    print()
    print("=== kind filter on occupy-any ===")
    server.device_leases.clear()
    r = c.post("/api/devices/occupy", json={"owner": "k1", "kind": "virtual"})
    check("kind=virtual picks the emulator", r.json().get("serial") == "emulator-5554", r.text)
    r = c.post("/api/devices/occupy", json={"owner": "k2", "kind": "virtual"})
    check("no free emulator -> 409 even with a free phone", r.status_code == 409, r.text)
    r = c.post("/api/devices/occupy", json={"owner": "k2", "kind": "physical"})
    check("kind=physical picks the phone", r.json().get("serial") == "PHONE_1", r.text)
    check("bad kind -> 400", c.post("/api/devices/occupy", json={"owner": "x", "kind": "cloud"}).status_code == 400)
    h = c.get("/api/health").json()
    check("health counts virtual devices", h.get("devices_virtual") == 1, str(h))
    check("health reports the SDK", (h.get("virtual") or {}).get("sdk_root") == SDK, str(h.get("virtual")))
    check("health status unaffected by emulator support", h.get("status") in ("ok", "degraded"), str(h))

    print()
    print("=== delete / stop ===")
    r = c.delete("/api/avds/Pixel_6_API_34")
    check("delete while running -> 409", r.status_code == 409, r.text)
    r = c.post("/api/avds/Pixel_6_API_34/stop", json={"owner": "intruder"})
    check("stop blocked by someone else's lease", r.status_code == 409, r.text)
    check("emulator still alive after refused stop", vd.launched["Pixel_6_API_34"]["proc"].poll() is None)

    proc = vd.launched["Pixel_6_API_34"]["proc"]
    r = c.post("/api/avds/Pixel_6_API_34/stop", json={"owner": "k1"})
    check("stop by lease holder 200", r.status_code == 200, r.text)
    check("process actually gone", proc.poll() is not None)
    check("lease on the port serial dropped", server.get_lease("emulator-5554") is None, str(server.device_leases))
    check("boot flag / name cache forgotten", vd.avd_name_for("emulator-5554") is None)
    del ADB["emulator-5554"]; AVD_OF.pop("emulator-5554"); BOOTED.discard("emulator-5554")
    check("status back to stopped", avds()["Pixel_6_API_34"]["status"] == "stopped")
    r = c.post("/api/avds/Pixel_6_API_34/stop", json={})
    check("stopping a stopped AVD is a no-op", r.status_code == 200 and r.json().get("message") == "Not running", r.text)

    print()
    print("=== start with wait ===")
    # Boot "completes" as soon as the farm looks: adb lists the port as a
    # booted device once the process exists.
    orig_states = server.list_device_states
    def booting_states():
        for name, entry in vd.launched.items():
            ADB[entry["serial"]] = "device"; BOOTED.add(entry["serial"])
        return orig_states()
    server.list_device_states = booting_states
    r = c.post("/api/avds/Pixel_6_API_34/start", json={"wait": True, "timeout": 10})
    check("wait returns once booted", r.status_code == 200 and r.json().get("state") == "running", r.text)
    serial = r.json().get("serial")
    server.list_device_states = orig_states
    c.post("/api/avds/Pixel_6_API_34/stop", json={})
    ADB.pop(serial, None); BOOTED.discard(serial)

    BOOTED.clear()
    r = c.post("/api/avds/Pixel_6_API_34/start", json={"wait": True, "timeout": 1})
    check("boot timeout -> 504 but emulator left running",
          r.status_code == 504 and vd.launched.get("Pixel_6_API_34", {}).get("proc").poll() is None, r.text)
    c.post("/api/avds/Pixel_6_API_34/stop", json={})

    print()
    print("=== status of a farm-launched emulator that died ===")
    class DeadProc:
        def __init__(self, code): self.code = code
        def poll(self): return self.code
    logp = os.path.join(WORK, "dead.log"); write(logp, "INFO    | line1\nPANIC: Broken AVD system path\n")
    vd.launched["Pixel_6_API_34"] = {"proc": DeadProc(1), "port": 5554, "serial": "emulator-5554",
                                     "started": time.time(), "log": logp}
    a = avds()["Pixel_6_API_34"]
    check("nonzero exit -> failed with log", a["status"] == "failed" and "PANIC" in " ".join(a.get("log", [])), str(a))
    check("failed status names the reason", a["detail"].endswith(": Broken AVD system path"), a["detail"])
    write(logp, "INFO    | nothing useful\n")
    check("no ERROR line -> plain exit code", vd.failure_reason(logp) is None)
    check("missing log file -> None", vd.failure_reason(os.path.join(WORK, "nope.log")) is None)
    c.post("/api/avds/Pixel_6_API_34/stop", json={})
    check("stop clears a failed entry", "Pixel_6_API_34" not in vd.launched)
    vd.launched["Pixel_6_API_34"] = {"proc": DeadProc(0), "port": 5554, "serial": "emulator-5554",
                                     "started": time.time(), "log": logp}
    check("clean exit elsewhere -> stopped", avds()["Pixel_6_API_34"]["status"] == "stopped")
    check("clean exit entry dropped", "Pixel_6_API_34" not in vd.launched)

    print()
    print("=== ports and serial mapping ===")
    vd.port_in_use = lambda port: port in (5556, 5557)
    check("taken serial skipped", vd.pick_port({"emulator-5554"}) == 5558, vd.pick_port({"emulator-5554"}))
    check("busy host port skipped", vd.pick_port(set()) == 5554)
    vd.launched["X"] = {"proc": DeadProc(None), "port": 5554, "serial": "emulator-5554", "started": 0, "log": ""}
    check("port of an emulator still starting is reserved", vd.pick_port(set()) == 5558)
    vd.launched.clear()
    try:
        vd.pick_port({f"emulator-{p}" for p in range(5554, 5683, 2)})
        check("exhausted ports -> error", False)
    except vd.VirtualDeviceError as e:
        check("exhausted ports -> 409 error", e.status == 409)

    AVD_OF.update({"emulator-5560": "A", "emulator-5562": "B"})
    got = vd.running_emulators("adb", ["PHONE_1", "emulator-5560", "emulator-5562"])
    check("serials mapped to AVD names", got == {"A": "emulator-5560", "B": "emulator-5562"}, str(got))
    AVD_OF["emulator-5560"] = "C"
    check("mapping cached while attached", vd.running_emulators("adb", ["emulator-5560"]) == {"A": "emulator-5560"})
    vd.running_emulators("adb", [])
    check("mapping forgotten once detached (port may host another AVD next)",
          vd.running_emulators("adb", ["emulator-5560"]) == {"C": "emulator-5560"})

    print()
    print("=== create / delete through avdmanager ===")
    vd.avdmanager_binary = lambda: None
    r = c.post("/api/avds", json={"name": "New_1", "image": IMAGE})
    check("no avdmanager -> 503", r.status_code == 503, r.text)

    vd.avdmanager_binary = lambda: "/sdk/cmdline-tools/latest/bin/avdmanager"
    tool_calls = []
    def fake_tool(cmd, timeout, stdin_text=None):
        tool_calls.append((cmd, stdin_text))
        if cmd[1:3] == ["create", "avd"]:
            make_avd(cmd[cmd.index("-n") + 1])
        if cmd[1:3] == ["delete", "avd"]:
            name = cmd[cmd.index("-n") + 1]
            os.remove(os.path.join(AVD_HOME, f"{name}.ini"))
            shutil.rmtree(os.path.join(AVD_HOME, f"{name}.avd"))
        if cmd[1:3] == ["list", "device"]:
            return "Loading local repository...\npixel_6\npixel_tablet\nNexus 5X\n"
        return ""
    vd.run_tool = fake_tool

    r = c.post("/api/avds", json={"name": "New_1", "image": "system-images;android-99;x;y"})
    check("image that is not installed -> 400", r.status_code == 400 and not tool_calls, r.text)
    r = c.post("/api/avds", json={"name": "Pixel_6_API_34", "image": IMAGE})
    check("existing name -> 409", r.status_code == 409 and not tool_calls, r.text)
    r = c.post("/api/avds", json={"name": "New_1", "image": IMAGE, "device": "pixel_6 & calc"})
    check("profile with shell syntax -> 400", r.status_code == 400 and not tool_calls, r.text)
    r = c.post("/api/avds", json={"name": "-n", "image": IMAGE})
    check("option-looking name -> 400", r.status_code == 400 and not tool_calls, r.text)

    r = c.post("/api/avds", json={"name": "New_1", "image": IMAGE, "device": "pixel_6"})
    check("create 200", r.status_code == 200 and r.json()["avd"]["name"] == "New_1", r.text)
    cmd, stdin_text = tool_calls[-1]
    check("avdmanager argv", cmd[1:] == ["create", "avd", "-n", "New_1", "-k", IMAGE, "-d", "pixel_6"], str(cmd))
    check("custom-profile prompt answered", stdin_text == "no\n", repr(stdin_text))

    r = c.get("/api/sdk/device-profiles")
    check("profiles listed without progress noise",
          r.json().get("profiles") == ["pixel_6", "pixel_tablet", "Nexus 5X"], r.text)

    r = c.delete("/api/avds/New_1")
    check("delete 200", r.status_code == 200, r.text)
    check("avdmanager delete argv", tool_calls[-1][0][1:] == ["delete", "avd", "-n", "New_1"], str(tool_calls[-1]))
    check("deleted AVD gone from list", "New_1" not in avds())
    check("delete unknown -> 404", c.delete("/api/avds/New_1").status_code == 404)
finally:
    cleanup()

print()
print(f"{len(fails)} failure(s)" if fails else "all passed")
sys.exit(1 if fails else 0)
