"""Drive cli.py as a real subprocess against a real server.

The input subcommands were all broken by an eagerly-built payload dict that
touched arguments other subparsers own. Nothing in the in-process suites caught
it because they never invoked the CLI, so this file does.
"""
import sys, os, json, socket, subprocess, threading, time, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="farm_cli_")
shutil.copytree(os.path.join(ROOT, "static"), os.path.join(WORK, "static"))
os.makedirs(os.path.join(WORK, "macros"), exist_ok=True)
os.chdir(WORK)

import server
import uvicorn

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# ---- fake device layer -------------------------------------------------
class Dev:
    def __init__(s, serial): s.serial = serial
class Info:
    def __init__(s, serial, state): s.serial, s.state = serial, state
server.adb.device_list = lambda: [Dev("CLI_A")]
server.adb.list = lambda: [Info("CLI_A", "device")]
server.get_device_resolution = lambda s: (1080, 2400)

CALLS_FILE = os.path.join(WORK, "calls.jsonl")
async def fake_exec(adb_path, serial, *args):
    with open(CALLS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps([serial, *args]) + "\n")
server.adb_exec = fake_exec

def calls():
    if not os.path.exists(CALLS_FILE):
        return []
    with open(CALLS_FILE, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def reset_calls():
    open(CALLS_FILE, "w").close()

# ---- serve on a free port ---------------------------------------------
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); PORT = sock.getsockname()[1]; sock.close()
cfg = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="error")
srv = uvicorn.Server(cfg)
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(80):
    try:
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1).read()
        break
    except Exception:
        time.sleep(0.1)

CLI = os.path.join(ROOT, "cli.py")
BASE = f"http://127.0.0.1:{PORT}"

def run(*args):
    p = subprocess.run([sys.executable, CLI, "--base", BASE, *args],
                       capture_output=True, text=True, cwd=WORK)
    return p.returncode, p.stdout, p.stderr

print("=== every input subcommand actually runs (was AttributeError) ===")
cases = [
    (["tap", "--serial", "CLI_A", "--x", "540", "--y", "1200"],
     ["CLI_A", "shell", "input", "tap", "540", "1200"]),
    (["swipe", "--serial", "CLI_A", "--x1", "1", "--y1", "2", "--x2", "3", "--y2", "4",
      "--duration", "250"],
     ["CLI_A", "shell", "input", "swipe", "1", "2", "3", "4", "250"]),
    (["key", "--serial", "CLI_A", "--keycode", "3"],
     ["CLI_A", "shell", "input", "keyevent", "3"]),
    (["text", "--serial", "CLI_A", "--value", "qa"],
     ["CLI_A", "shell", "input", "text", "qa"]),
]
for args, expected in cases:
    reset_calls()
    rc, out, err = run(*args)
    check(f"cli {args[0]} exits 0", rc == 0, f"rc={rc} err={err.strip()[:160]}")
    check(f"cli {args[0]} has no traceback", "Traceback" not in err, err.strip()[:160])
    check(f"cli {args[0]} reaches adb correctly", calls() == [expected], str(calls()))

print()
print("=== owner is forwarded, and a held device is refused ===")
reset_calls()
rc, out, _ = run("occupy", "--serial", "CLI_A", "--owner", "ci-a", "--ttl", "60")
check("cli occupy exits 0", rc == 0, out[:160])
rc, out, _ = run("tap", "--serial", "CLI_A", "--x", "1", "--y", "1", "--owner", "intruder")
check("cli tap by non-owner exits 1", rc == 1, f"rc={rc}")
check("intruder's tap never reached adb", calls() == [], str(calls()))
rc, out, _ = run("tap", "--serial", "CLI_A", "--x", "1", "--y", "1", "--owner", "ci-a")
check("cli tap by owner exits 0", rc == 0, out[:160])
run("release", "--serial", "CLI_A", "--owner", "ci-a")

print()
print("=== recording captures HTTP-driven input (was silently empty) ===")
import urllib.request
def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

post("/api/macros/start_record/CLI_A")
run("key", "--serial", "CLI_A", "--keycode", "3")
run("swipe", "--serial", "CLI_A", "--x1", "10", "--y1", "20", "--x2", "30", "--y2", "40")
saved = post("/api/macros/stop_record/CLI_A", {"name": "cli_rec"})
check("HTTP input was recorded", saved["count"] == 2, str(saved))
check("recording stored the resolution", saved["resolution"] == [1080, 2400], str(saved))

macro = json.load(open(os.path.join(WORK, "macros", "cli_rec.json"), encoding="utf-8"))
kinds = [e["type"] for e in macro["events"]]
check("both event types captured in order", kinds == ["key", "swipe"], str(kinds))
check("lease owner is not stored as part of the action",
      all("owner" not in e for e in macro["events"]), str(macro["events"]))
check("steps carry timestamps for replay pacing",
      all("timestamp" in e for e in macro["events"]), str(macro["events"]))

print()
print("=== console-unsafe characters cannot kill a request (cp949) ===")
check("stdout was reconfigured to utf-8",
      (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8",
      str(getattr(sys.stdout, "encoding", None)))
# The audio window title used to embed an emoji, which the cp949 console could
# not encode; the resulting UnicodeEncodeError surfaced as a 500.
src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
title_line = [l for l in src.splitlines() if "--window-title" in l]
check("audio window title is ascii-only",
      bool(title_line) and all(ch.isascii() for l in title_line for ch in l), str(title_line))
try:
    print("  emoji through the reconfigured stream: \U0001f50a ok")
    check("printing an emoji no longer raises", True)
except UnicodeEncodeError as e:
    check("printing an emoji no longer raises", False, repr(e))

print()
print(f"{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
srv.should_exit = True
sys.exit(1 if fails else 0)
