"""harness 스크립트 러너와 skill 생성기를 실제 서버에 붙여 검증합니다.

test_cli.py와 같은 방식입니다 — cli.py를 진짜 서브프로세스로 돌리고, 서버는
가짜 adb 위에서 돕니다. 여기서 보려는 것은 두 가지입니다.

1. 스크립트 한 줄이 기기에 무엇으로 도착하는가.
2. **스크립트에 없는 것은 실행되지 않는가.** 이 러너는 파이썬을 exec하지
   않기로 한 결정 위에 서 있고, 그 결정은 테스트로 못 박아 둬야 합니다.
"""
import sys, os, json, socket, subprocess, threading, time, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WORK = tempfile.mkdtemp(prefix="farm_harness_")
shutil.copytree(os.path.join(ROOT, "static"), os.path.join(WORK, "static"))
os.makedirs(os.path.join(WORK, "macros"), exist_ok=True)
os.chdir(WORK)

import server
import uvicorn

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# ---- 가짜 기기 ---------------------------------------------------------
class Dev:
    def __init__(s, serial): s.serial = serial
class Info:
    def __init__(s, serial, state): s.serial, s.state = serial, state
server.adb.device_list = lambda: [Dev("HAR_A")]
server.adb.list = lambda: [Info("HAR_A", "device")]
server.get_device_resolution = lambda s: (1080, 2400)

CALLS_FILE = os.path.join(WORK, "calls.jsonl")
async def fake_exec(adb_path, serial, *args):
    with open(CALLS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps([serial, *args]) + "\n")
server.adb_exec = fake_exec

DUMP_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" text="네트워크" clickable="true" enabled="true"
        bounds="[0,200][1080,320]" />
</hierarchy>"""

async def fake_capture(adb_path, serial, *args):
    return DUMP_XML
server.adb_capture = fake_capture

def png():
    from PIL import Image
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (20, 40), (30, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()
PNG = png()
server.capture_screenshot_bytes = lambda serial, full=False, size=None: (PNG, "image/png")

def calls():
    if not os.path.exists(CALLS_FILE):
        return []
    with open(CALLS_FILE, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def reset_calls():
    open(CALLS_FILE, "w").close()

# ---- 서버 ---------------------------------------------------------------
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

def run(*args, stdin="", env=None):
    p = subprocess.run([sys.executable, CLI, "--base", BASE, *args],
                       input=stdin, capture_output=True, text=True, cwd=WORK,
                       env=dict(os.environ, **(env or {})))
    return p.returncode, p.stdout, p.stderr

print("=== 개별 verb 서브커맨드 ===")
reset_calls()
rc, out, err = run("tap-text", "--serial", "HAR_A", "--text", "네트워크")
check("tap-text 종료 0", rc == 0, err[:200])
check("가운데 좌표로 탭이 나갑니다",
      calls() == [["HAR_A", "shell", "input", "tap", "540", "260"]], str(calls()))

reset_calls()
rc, out, err = run("tap-text", "--serial", "HAR_A", "--text", "없는글자")
check("못 찾으면 종료 1", rc == 1, f"rc={rc}")
check("못 찾으면 기기를 건드리지 않습니다", calls() == [], str(calls()))

rc, out, _ = run("elements", "--serial", "HAR_A")
check("elements 종료 0", rc == 0, out[:200])
check("elements가 요소를 돌려줍니다", '"count": 1' in out, out[:200])

reset_calls()
rc, out, err = run("type-text", "--serial", "HAR_A", "--value", "hello world")
check("type-text 종료 0", rc == 0, err[:200])
check("공백 문자열이 안전하게 인용됩니다",
      calls() == [["HAR_A", "shell", "input", "text", "'hello%sworld'"]], str(calls()))

reset_calls()
rc, out, err = run("open-app", "--serial", "HAR_A", "--package", "com.x.y")
check("open-app 종료 0", rc == 0, err[:200])
check("monkey로 실행합니다",
      calls() == [["HAR_A", "shell", "monkey", "-p", "com.x.y", "-c",
                   "android.intent.category.LAUNCHER", "1"]], str(calls()))

rc, out, err = run("open-app", "--serial", "HAR_A")
check("open-app에 대상이 없으면 실패", rc != 0, f"rc={rc}")

print()
print("=== harness 스크립트 ===")
reset_calls()
script = """
# 주석과 빈 줄은 건너뜁니다

open_app com.android.settings
wait_stable 1
tap_text "네트워크"
type_text "hello world"
tap 100 200
swipe 1 2 3 4 250
key 4
"""
rc, out, err = run("harness", "--serial", "HAR_A", stdin=script)
check("harness 종료 0", rc == 0, err[:300])
got = calls()
expected = [
    ["HAR_A", "shell", "monkey", "-p", "com.android.settings", "-c",
     "android.intent.category.LAUNCHER", "1"],
    ["HAR_A", "shell", "input", "tap", "540", "260"],
    ["HAR_A", "shell", "input", "text", "'hello%sworld'"],
    ["HAR_A", "shell", "input", "tap", "100", "200"],
    ["HAR_A", "shell", "input", "swipe", "1", "2", "3", "4", "250"],
    ["HAR_A", "shell", "input", "keyevent", "4"],
]
check("스크립트가 순서대로 기기에 도착합니다", got == expected, str(got))
check("스텝마다 결과를 한 줄씩 찍습니다",
      len([l for l in out.splitlines() if l.startswith("{")]) == 7, out[:300])

print()
print("=== 실행 전에 전부 검증합니다 ===")
reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A",
                   stdin="tap 1 2\nreboot_device\ntap 3 4\n")
check("모르는 동사면 종료 2", rc == 2, f"rc={rc} {err[:200]}")
check("몇 행이 문제인지 말합니다", "2행" in err, err[:200])
check("가능한 동사를 알려줍니다", "tap_text" in err, err[:200])
check("첫 줄조차 실행되지 않습니다", calls() == [], str(calls()))

reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A",
                   stdin="import os; os.system('echo pwned')\n")
check("파이썬 코드는 동사가 아닙니다", rc == 2, f"rc={rc}")
check("코드가 실행되지 않습니다", "pwned" not in out, out[:200])
check("기기에도 아무것도 가지 않습니다", calls() == [], str(calls()))

reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A", stdin="tap 100\n")
check("인자 수가 틀리면 종료 2", rc == 2, f"rc={rc}")
check("올바른 사용법을 보여줍니다", "tap <x> <y>" in err, err[:200])
check("인자가 틀리면 실행되지 않습니다", calls() == [], str(calls()))

rc, out, err = run("harness", "--serial", "HAR_A", stdin='tap_text "닫히지 않은\n')
check("따옴표가 안 맞으면 종료 2", rc == 2, f"rc={rc}")

rc, out, err = run("harness", "--serial", "HAR_A", stdin="\n# 주석뿐\n")
check("빈 스크립트는 실패로 끝납니다", rc != 0, f"rc={rc}")

print()
print("=== 실패한 스텝에서 멈춥니다 ===")
reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A",
                   stdin='tap 1 1\ntap_text "화면에 없는 글자"\ntap 2 2\n')
check("중간에 실패하면 종료 1", rc == 1, f"rc={rc} {err[:200]}")
check("실패 지점을 말합니다", "2행" in err, err[:200])
check("실패 뒤의 줄은 실행하지 않습니다",
      calls() == [["HAR_A", "shell", "input", "tap", "1", "1"]], str(calls()))

print()
print("=== 점유와 반납을 대신해 줍니다 ===")
reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A", "--owner", "ai-1",
                   "--occupy", "--release", stdin="tap 5 5\n")
check("--occupy --release 종료 0", rc == 0, err[:300])
import urllib.request
leases = json.loads(urllib.request.urlopen(f"{BASE}/api/leases").read())
check("끝나면 반납되어 있습니다", not leases["leases"], str(leases))

# 남이 잡고 있으면 조작이 거절되는지 (점유 규칙이 harness에도 적용되는지)
urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/api/device/HAR_A/occupy",
    data=json.dumps({"owner": "someone", "ttl_seconds": 60}).encode(),
    headers={"Content-Type": "application/json"}))
reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A", "--owner", "ai-2", stdin="tap 9 9\n")
check("남이 점유 중이면 종료 1", rc == 1, f"rc={rc}")
check("점유 중이면 기기에 닿지 않습니다", calls() == [], str(calls()))

reset_calls()
rc, out, err = run("harness", "--serial", "HAR_A", "--owner", "ai-3",
                   "--occupy", stdin="tap 9 9\n")
check("--occupy가 거절되면 실패로 끝납니다", rc != 0, f"rc={rc}")
check("점유 실패는 스택 트레이스가 아닙니다", "Traceback" not in err, err[:200])
check("점유 실패 사유를 말합니다", "점유하지 못했습니다" in err, err[:200])
check("점유 실패면 스크립트가 돌지 않습니다", calls() == [], str(calls()))
urllib.request.urlopen(urllib.request.Request(
    f"{BASE}/api/device/HAR_A/release",
    data=json.dumps({"owner": "someone"}).encode(),
    headers={"Content-Type": "application/json"}))

print()
print("=== screenshot 스텝은 파일로 떨어집니다 ===")
rc, out, err = run("harness", "--serial", "HAR_A", stdin="screenshot shot.png\n")
check("screenshot 스텝 종료 0", rc == 0, err[:200])
check("파일이 생깁니다", os.path.exists(os.path.join(WORK, "shot.png")))
check("원본 PNG입니다",
      open(os.path.join(WORK, "shot.png"), "rb").read()[:8] == b"\x89PNG\r\n\x1a\n")

print()
print("=== skill 생성 ===")
rc, out, err = run("skill", "--target", "stdout")
check("skill stdout 종료 0", rc == 0, err[:200])
check("frontmatter가 있습니다", out.startswith("---\nname: qa-device-farm"), out[:80])
check("팜 주소가 박힙니다", BASE in out, out[:200])
for must in ["occupy", "release", "tap_text", "wait_stable", "ios-mirror", "ASCII"]:
    check(f"스킬이 '{must}'를 설명합니다", must in out, must)

# HOME을 갈아끼워야 ~/.claude에 쓰는 걸 확인할 수 있는데, pip가 사용자
# 사이트(~/.local)에 깔린 환경에서는 그것만 바꾸면 import부터 깨집니다.
# 패키지 경로는 원래 자리로 고정해 둡니다.
REAL_USER_BASE = os.path.join(os.path.expanduser("~"), ".local")

fake_home = os.path.join(WORK, "fakehome")
rc, out, err = run("skill", "--target", "claude",
                   env={"HOME": fake_home, "PYTHONUSERBASE": REAL_USER_BASE})
check("skill claude 종료 0", rc == 0, err[:200])
claude_path = os.path.join(fake_home, ".claude", "skills", "qa-device-farm", "SKILL.md")
check("~/.claude/skills 아래에 씁니다", os.path.exists(claude_path), out.strip())
check("경로를 출력합니다", claude_path in out, out.strip())

rc, out, err = run("skill", "--target", "codex",
                   env={"HOME": fake_home, "PYTHONUSERBASE": REAL_USER_BASE})
codex_path = os.path.join(fake_home, ".codex", "skills", "qa-device-farm", "SKILL.md")
check("codex 경로에도 씁니다", os.path.exists(codex_path), out.strip())

custom_home = os.path.join(WORK, "customcodex")
rc, out, err = run("skill", "--target", "codex",
                   env={"HOME": fake_home, "CODEX_HOME": custom_home,
                        "PYTHONUSERBASE": REAL_USER_BASE})
check("CODEX_HOME을 존중합니다",
      os.path.exists(os.path.join(custom_home, "skills", "qa-device-farm", "SKILL.md")),
      out.strip())

print()
srv.should_exit = True
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
