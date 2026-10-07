"""Virtual devices: Android emulators (AVDs) run by the farm.

A shelf of real phones runs out in predictable ways. There are only so many of
them, and the OS version or screen size a bug report names is never the one on
the shelf. An emulator fills that gap: any API level, any screen, started from
a known state and gone when the run is over.

Nothing here reinvents the SDK. The farm drives Google's own `emulator` and
`avdmanager`, and once an emulator is up it is just another adb device --
`emulator-<port>` -- so leases, input, logcat, macros, mirroring and batch runs
all work on it with no special casing in server.py.

What lives in this module is only what is genuinely emulator-specific:

* finding the SDK, the emulator binary, avdmanager and the AVD directory;
* reading AVD definitions straight from their .ini files -- the dashboard
  polls, and spawning `emulator -list-avds` every few seconds would be waste;
* choosing a console port, launching, checking boot, stopping;
* mapping an `emulator-<port>` serial back to the AVD it is running.
"""

import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from adbutils import adb

# avdmanager's own rule is "a-z A-Z 0-9 . _ -". The first character is held to
# alphanumerics on top of that: a name starting with "-" would be read as an
# option by both tools, and one starting with "." hides the .ini file.
AVD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Hardware profile ids ("pixel_6", "Nexus 5X", "7.6in Foldable"). On Windows
# avdmanager is a .bat, which cmd.exe re-parses, so anything cmd treats as
# syntax (& | < > ^ % ") is kept out by construction rather than escaped.
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,63}$")

EMULATOR_SERIAL_RE = re.compile(r"^emulator-(\d+)$")

GPU_MODES = ("auto", "host", "swiftshader_indirect", "angle_indirect", "guest")

# The emulator takes an even console port in this range; adb listens on +1.
FIRST_PORT, LAST_PORT = 5554, 5682

LOG_DIR = "logs"

# A missing engine, no hardware acceleration, a locked AVD: the common failures
# all make the emulator exit before it ever registers with adb. The start call
# waits for one or the other, so they come back as an immediate error instead
# of a silent "starting". Measured with emulator 37.2: a missing-acceleration
# exit takes 0.05s normally, but several seconds on the first run after the
# SDK is installed -- which is exactly when someone is trying it out -- so a
# fixed two-second wait missed it. The cap only matters on that slow path.
EARLY_EXIT_GRACE = 15.0
LAUNCH_POLL = 0.25
BOOT_POLL = 2.0

# The lines an emulator prints when it gives up. Its log opens with a dozen
# INFO lines, and the one that explains the failure is easy to miss among them.
FAILURE_LINE = re.compile(r"^(?:ERROR|PANIC|FATAL)\b[\s|:]*(.*)$")


class VirtualDeviceError(Exception):
    """A request the farm cannot carry out, with a message meant for the user."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_emulator_serial(serial: str) -> bool:
    return bool(EMULATOR_SERIAL_RE.match(serial or ""))


def check_avd_name(name: str) -> str:
    if not name or not AVD_NAME_RE.match(name):
        raise VirtualDeviceError(
            f"Invalid AVD name: {name!r} (letters, digits, '.', '_' and '-'; "
            "must start with a letter or digit)")
    return name


# --- Locating the SDK ---

def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def sdk_root():
    """The Android SDK directory, or None.

    The two documented variables first, then where Android Studio puts the SDK
    on each OS, and finally the SDK that the adb on PATH came from -- which is
    the usual state of a machine where someone installed Studio and nothing else.
    """
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        path = os.environ.get(var, "").strip()
        if path and os.path.isdir(path):
            return path

    home = os.path.expanduser("~")
    if os.name == "nt":
        default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")
    elif sys.platform == "darwin":
        default = os.path.join(home, "Library", "Android", "sdk")
    else:
        default = os.path.join(home, "Android", "Sdk")
    if os.path.isdir(default):
        return default

    adb_on_path = shutil.which("adb")
    if adb_on_path:
        root = os.path.dirname(os.path.dirname(os.path.realpath(adb_on_path)))
        if os.path.isdir(os.path.join(root, "emulator")) or \
                os.path.isdir(os.path.join(root, "system-images")):
            return root
    return None


def emulator_binary():
    root = sdk_root()
    if root:
        path = os.path.join(root, "emulator", _exe("emulator"))
        if os.path.exists(path):
            return path
    return shutil.which("emulator")


def avdmanager_binary():
    """avdmanager from cmdline-tools (latest first), the legacy tools/, or PATH."""
    name = "avdmanager.bat" if os.name == "nt" else "avdmanager"
    root = sdk_root()
    if root:
        candidates = [os.path.join(root, "cmdline-tools", "latest", "bin", name)]
        candidates += sorted(glob.glob(os.path.join(root, "cmdline-tools", "*", "bin", name)),
                             reverse=True)
        candidates.append(os.path.join(root, "tools", "bin", name))
        for path in candidates:
            if os.path.exists(path):
                return path
    return shutil.which("avdmanager")


def avd_home():
    """Where AVD definitions live, honouring the same variables the SDK does."""
    explicit = os.environ.get("ANDROID_AVD_HOME", "").strip()
    if explicit:
        return explicit
    user_home = os.environ.get("ANDROID_USER_HOME", "").strip()
    if user_home:
        return os.path.join(user_home, "avd")
    legacy = os.environ.get("ANDROID_SDK_HOME", "").strip()
    if legacy:
        return os.path.join(legacy, ".android", "avd")
    return os.path.join(os.path.expanduser("~"), ".android", "avd")


def sdk_info():
    """What the farm found, for /api/health and the dashboard."""
    info = {
        "sdk_root": sdk_root(),
        "emulator": emulator_binary(),
        "avdmanager": avdmanager_binary(),
        "avd_home": avd_home(),
    }
    if not info["emulator"]:
        info["note"] = ("Android emulator not found. Install it with the SDK Manager "
                        "(Android Studio) or `sdkmanager emulator`, and set ANDROID_HOME "
                        "to the SDK directory.")
    elif not info["avdmanager"]:
        info["note"] = ("avdmanager not found, so AVDs cannot be created or deleted from "
                        "the farm (starting existing ones still works). Install "
                        "\"Android SDK Command-line Tools\".")
    return info


# --- Reading AVD definitions ---

def read_kv(path: str) -> dict:
    """Parse the key=value files the SDK uses for AVDs (no sections, no quoting)."""
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def image_id(sysdir: str):
    """'system-images/android-34/google_apis/x86_64/' -> 'system-images;android-34;google_apis;x86_64'."""
    parts = [p for p in re.split(r"[\\/]+", sysdir or "") if p]
    if len(parts) >= 4 and parts[0] == "system-images":
        return ";".join(parts[:4])
    return None


def _int(value):
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def _avd_dir(name: str, meta: dict, home: str) -> str:
    path = meta.get("path")
    if path and os.path.isdir(path):
        return path
    # path.rel is relative to the .android directory, i.e. the parent of avd/.
    rel = meta.get("path.rel")
    if rel:
        candidate = os.path.join(os.path.dirname(home), rel)
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(home, f"{name}.avd")


def list_avds():
    """Every AVD defined on this host, read from disk."""
    home = avd_home()
    avds = []
    for ini in sorted(glob.glob(os.path.join(home, "*.ini"))):
        name = os.path.basename(ini)[:-len(".ini")]
        try:
            meta = read_kv(ini)
        except OSError:
            continue
        path = _avd_dir(name, meta, home)
        try:
            cfg = read_kv(os.path.join(path, "config.ini"))
        except OSError:
            cfg = {}

        image = image_id(cfg.get("image.sysdir.1", ""))
        api = image.split(";")[1].replace("android-", "") if image else None
        if not api and meta.get("target", "").startswith("android-"):
            api = meta["target"][len("android-"):]

        avds.append({
            "name": name,
            "display_name": cfg.get("avd.ini.displayname") or name,
            "api": api,
            "image": image,
            "tag": cfg.get("tag.id"),
            "abi": cfg.get("abi.type"),
            "device": cfg.get("hw.device.name"),
            "width": _int(cfg.get("hw.lcd.width")),
            "height": _int(cfg.get("hw.lcd.height")),
            "ram_mb": _int(cfg.get("hw.ramSize")),
            "path": path,
            # An .ini whose .avd directory is gone: the emulator refuses it, so
            # say so instead of letting a start fail with a cryptic panic.
            "broken": not cfg,
        })
    return avds


def find_avd(name: str):
    return next((a for a in list_avds() if a["name"] == name), None)


def list_system_images():
    """System images installed in the SDK -- the only ones an AVD can be made from."""
    root = sdk_root()
    if not root:
        return []
    images = []
    pattern = os.path.join(root, "system-images", "*", "*", "*", "source.properties")
    for props in sorted(glob.glob(pattern)):
        abi_dir = os.path.dirname(props)
        abi = os.path.basename(abi_dir)
        tag = os.path.basename(os.path.dirname(abi_dir))
        platform = os.path.basename(os.path.dirname(os.path.dirname(abi_dir)))
        images.append({
            "id": f"system-images;{platform};{tag};{abi}",
            "api": platform.replace("android-", ""),
            "tag": tag,
            "abi": abi,
        })
    return images


# --- avdmanager ---

def run_tool(cmd, timeout, stdin_text=None):
    """Run an SDK tool and return stdout, or raise with what it said."""
    try:
        result = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise VirtualDeviceError(f"{cmd[0]} not found", 503)
    except subprocess.TimeoutExpired:
        raise VirtualDeviceError(f"{os.path.basename(cmd[0])} did not finish in {timeout}s", 504)
    if result.returncode != 0:
        # avdmanager reports a missing JAVA_HOME and a bad package on stdout as
        # often as on stderr, so keep the tail of both.
        said = "\n".join(s.strip() for s in (result.stderr, result.stdout) if s and s.strip())
        raise VirtualDeviceError(
            "\n".join(said.splitlines()[-8:]) or f"exited with code {result.returncode}", 500)
    return result.stdout


def _require_avdmanager():
    tool = avdmanager_binary()
    if not tool:
        raise VirtualDeviceError(sdk_info().get("note") or "avdmanager not found", 503)
    return tool


_profiles_cache = None


def list_device_profiles():
    """Hardware profile ids avdmanager knows. Spawns Java, so cached for the process."""
    global _profiles_cache
    if _profiles_cache is None:
        out = run_tool([_require_avdmanager(), "list", "device", "-c"], timeout=90)
        _profiles_cache = [
            line.strip() for line in out.splitlines()
            # Progress and warning lines share stdout with the ids.
            if PROFILE_RE.match(line.strip()) and "..." not in line
            and not line.strip().lower().startswith(("warning", "error", "loading"))
        ]
    return _profiles_cache


def create_avd(name: str, image: str, device: str = None):
    check_avd_name(name)
    if find_avd(name):
        raise VirtualDeviceError(f"AVD '{name}' already exists", 409)
    # Checking against what is installed doubles as input validation: only a
    # package id that exists on disk ever reaches avdmanager.
    if image not in {i["id"] for i in list_system_images()}:
        raise VirtualDeviceError(
            f"System image not installed: {image!r}. Install it first, e.g. "
            f"sdkmanager \"system-images;android-34;google_apis;x86_64\"", 400)
    if device and not PROFILE_RE.match(device):
        raise VirtualDeviceError(f"Invalid device profile: {device!r}")

    cmd = [_require_avdmanager(), "create", "avd", "-n", name, "-k", image]
    if device:
        cmd += ["-d", device]
    # avdmanager asks whether to create a custom hardware profile and blocks on
    # stdin until it gets an answer.
    run_tool(cmd, timeout=180, stdin_text="no\n")

    created = find_avd(name)
    if not created:
        raise VirtualDeviceError(
            f"avdmanager reported success but no AVD named '{name}' appeared in "
            f"{avd_home()}. Is ANDROID_AVD_HOME the same for the farm and avdmanager?", 500)
    return created


def delete_avd(name: str):
    check_avd_name(name)
    if not find_avd(name):
        raise VirtualDeviceError(f"No AVD named '{name}'", 404)
    run_tool([_require_avdmanager(), "delete", "avd", "-n", name], timeout=60)


# --- Running emulators ---

# Emulators this server process launched: name -> {"proc", "port", "serial",
# "started", "log"}. Emulators started some other way (by hand, by an earlier
# run of the server) are still found through adb; this table only adds what
# adb cannot know -- that a process is starting, or that it died, and why.
launched = {}

_serial_avd = {}   # emulator serial -> AVD name, while the serial stays attached
_booted = set()    # serials that have reported sys.boot_completed=1


def query_avd_name(adb_path: str, serial: str):
    """Ask an emulator's console which AVD it is running.

    Works from the moment the console is up, before Android has booted far
    enough for getprop -- which is exactly the window the dashboard most wants
    to label.
    """
    try:
        result = subprocess.run([adb_path, "-s", serial, "emu", "avd", "name"],
                                capture_output=True, text=True, errors="replace", timeout=5)
    except Exception:
        return None
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    if result.returncode != 0 or not lines or lines[0].startswith("KO"):
        return None
    return lines[0]


def forget_serial(serial: str):
    _serial_avd.pop(serial, None)
    _booted.discard(serial)


def running_emulators(adb_path: str, serials):
    """{AVD name: serial} for every emulator adb currently lists."""
    present = {s for s in serials if is_emulator_serial(s)}
    # emulator-<port> names a port, not a device: once it is gone, the next
    # emulator on that port may be a different AVD entirely.
    for serial in [s for s in set(_serial_avd) | _booted if s not in present]:
        forget_serial(serial)

    running = {}
    for serial in sorted(present):
        name = _serial_avd.get(serial)
        if name is None:
            name = query_avd_name(adb_path, serial)
            if name:
                _serial_avd[serial] = name
        if name:
            running[name] = serial
    return running


def avd_name_for(serial: str):
    return _serial_avd.get(serial)


def is_booted(serial: str) -> bool:
    if serial in _booted:
        return True
    try:
        done = adb.device(serial=serial).shell("getprop sys.boot_completed").strip() == "1"
    except Exception:
        done = False
    if done:
        _booted.add(serial)
    return done


def failure_reason(path):
    """The emulator's own explanation for exiting, e.g.
    'x86_64 emulation currently requires hardware acceleration!', or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                match = FAILURE_LINE.match(line.strip())
                if match and match.group(1).strip():
                    return match.group(1).strip()
    except Exception:
        pass
    return None


def log_tail(path, lines=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-lines:]]
    except Exception:
        return []


def status_of(avd: dict, running: dict, states: dict) -> dict:
    """Where one AVD is in its lifecycle: stopped / starting / booting / running / failed."""
    name = avd["name"]
    entry = launched.get(name)
    alive = entry is not None and entry["proc"].poll() is None
    serial = running.get(name)
    # Launched here and already on adb, but its console has not answered yet:
    # the port says which serial it will be.
    if serial is None and alive and entry["serial"] in states:
        serial = entry["serial"]

    out = {"status": "stopped", "serial": serial, "detail": None}
    if serial:
        booted = states.get(serial) == "device" and is_booted(serial)
        out["status"] = "running" if booted else "booting"
    elif alive:
        out["status"] = "starting"
    elif entry is not None:
        code = entry["proc"].poll()
        if code == 0:
            # Shut down cleanly from somewhere else (`adb emu kill`, the window).
            launched.pop(name, None)
        else:
            out["status"] = "failed"
            reason = failure_reason(entry["log"])
            out["detail"] = f"emulator exited with code {code}" + (f": {reason}" if reason else "")
            out["log"] = log_tail(entry["log"])
    return out


def port_in_use(port: int) -> bool:
    probe = socket.socket()
    probe.settimeout(0.2)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def pick_port(taken_serials) -> int:
    """Lowest free console port.

    Chosen here rather than left to the emulator so the serial is known before
    it boots -- the caller gets `emulator-<port>` back straight away and can
    lease it, instead of guessing which new device in the list is theirs.
    """
    reserved = {e["port"] for e in launched.values() if e["proc"].poll() is None}
    for port in range(FIRST_PORT, LAST_PORT + 1, 2):
        if port in reserved or f"emulator-{port}" in taken_serials:
            continue
        if not port_in_use(port) and not port_in_use(port + 1):
            return port
    raise VirtualDeviceError(
        f"No free emulator port in {FIRST_PORT}-{LAST_PORT}; stop an emulator first.", 409)


def build_command(emulator: str, name: str, port: int, headless=True, wipe=False,
                  cold_boot=False, gpu=None):
    check_avd_name(name)
    # -no-snapshot-save: every run starts from the same state, whatever the
    # previous run left behind. That is the point of a test device; a farm
    # emulator that quietly carries the last tester's session is not one.
    # -no-metrics: emulator 37 prints a consent notice on every launch and says
    # it will become "a one-time blocking prompt" in a future release. Nobody
    # is at the console of a farm emulator to answer it.
    cmd = [emulator, "-avd", name, "-port", str(port), "-no-snapshot-save", "-no-boot-anim",
           "-no-metrics"]
    if headless:
        cmd.append("-no-window")
    if wipe:
        cmd.append("-wipe-data")
    if cold_boot:
        cmd.append("-no-snapshot-load")
    if gpu:
        if gpu not in GPU_MODES:
            raise VirtualDeviceError(f"Invalid gpu mode: {gpu!r} (expected one of {list(GPU_MODES)})")
        cmd += ["-gpu", gpu]
    return cmd


def _detach_kwargs():
    # A new session / process group, so Ctrl+C on the farm server does not take
    # every emulator down with it. Restarting the server should not kill the
    # runs in progress -- the same reasoning that made leases persistent.
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def launch(name: str, port: int, cmd):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"emulator_{name}.log")
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, **_detach_kwargs())
    serial = f"emulator-{port}"
    forget_serial(serial)
    _serial_avd[serial] = name
    launched[name] = {"proc": proc, "port": port, "serial": serial,
                      "started": time.time(), "log": log_path}
    return launched[name]


def stop(name: str, serial, adb_path: str, wait=20.0):
    """Shut an emulator down: politely through its console, then by force."""
    entry = launched.get(name)
    if serial:
        try:
            subprocess.run([adb_path, "-s", serial, "emu", "kill"],
                           capture_output=True, timeout=10)
        except Exception as e:
            print(f"[{serial}] emu kill: {e}")

    if entry is not None:
        proc = entry["proc"]
        if not serial and proc.poll() is None:
            # Not on adb yet, so there is no console to ask.
            proc.terminate()
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        launched.pop(name, None)

    if serial:
        forget_serial(serial)
