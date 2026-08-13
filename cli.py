"""Command-line client for the QA Device Farm.

Lets a terminal or a CI job use the farm without a browser:

    python cli.py devices
    python cli.py health
    python cli.py occupy --owner ci-smoke --ttl 300
    python cli.py tap    --serial R3CN30 --x 540 --y 1200
    python cli.py release --serial R3CN30 --owner ci-smoke

`occupy` without --serial takes any free device and prints its serial, which is
what a pipeline wants: ask for an Android, get one, run, hand it back.
"""

import argparse
import json
import os
import sys

import requests

DEFAULT_BASE = "http://localhost:8001"

# Set DEVICE_FARM_TOKEN when the farm requires one; --token overrides it. Taking
# it from the environment keeps the secret out of CI logs and shell history.
TOKEN = os.environ.get("DEVICE_FARM_TOKEN", "").strip()

def auth_headers():
    return {"X-Farm-Token": TOKEN} if TOKEN else {}


def split_serials(raw):
    serials = [s.strip() for s in raw.split(",") if s.strip()]
    if not serials:
        sys.exit("--serials is empty")
    return serials


def call(base, method, path, payload=None):
    url = f"{base}{path}"
    try:
        resp = requests.request(method, url, json=payload,
                                headers=auth_headers(), timeout=30)
    except requests.RequestException as e:
        sys.exit(f"cannot reach farm at {base}: {e}")

    try:
        body = resp.json()
    except ValueError:
        sys.exit(f"{resp.status_code} {resp.text[:200]}")

    print(json.dumps(body, indent=2, ensure_ascii=False))
    # A held device (409) is a normal pipeline outcome, so report it as failure
    # without a stack trace. A batch that only partly succeeded answers 200 but
    # is still a failed step as far as CI is concerned.
    if not resp.ok or (isinstance(body, dict) and body.get("status") == "partial"):
        sys.exit(1)
    return body


# --- harness: 여러 동작을 한 번에 ---
# phone-harness는 파이썬 스크립트를 heredoc으로 받아 exec합니다. 팜은 그럴 수
# 없습니다 -- 이 스크립트는 대개 LLM이 쓰고, 팜은 실기기 여러 대에 물려
# 네트워크에 열려 있습니다. 그래서 실행하는 게 아니라 **동사 표에 있는 것만
# 골라 HTTP 호출로 바꿉니다.** 표에 없는 줄은 실행 전에 걸러냅니다.
#
# 이 제한이 실제로 잃는 건 별로 없습니다. 에이전트가 쓰는 스크립트는 "열고,
# 기다리고, 누르고, 확인한다"의 반복이지 파이썬 제어 흐름이 아닙니다.

def harness_verbs():
    """{동사: (최소 인자 수, 최대 인자 수, 설명)}"""
    return {
        "open_app":   (1, 1, "open_app <패키지 또는 앱 이름>"),
        "tap_text":   (1, 2, "tap_text <글자> [몇 번째]"),
        "type_text":  (1, 1, "type_text <문자열>"),
        "tap":        (2, 2, "tap <x> <y>"),
        "swipe":      (4, 5, "swipe <x1> <y1> <x2> <y2> [지속ms]"),
        "key":        (1, 1, "key <키코드>"),
        "wait_stable": (0, 1, "wait_stable [초]"),
        "screenshot": (0, 1, "screenshot [파일명]"),
        "elements":   (0, 0, "elements"),
        "ocr":        (0, 0, "ocr (elements와 같습니다)"),
        "sleep":      (1, 1, "sleep <초>"),
    }


def parse_harness_script(text):
    """스크립트를 [(줄번호, 동사, 인자들)]로. 문제가 있으면 ValueError."""
    import shlex

    verbs = harness_verbs()
    steps, problems = [], []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as e:
            problems.append(f"{lineno}행: 따옴표가 맞지 않습니다 ({e})")
            continue
        verb, args = parts[0], parts[1:]
        if verb not in verbs:
            problems.append(f"{lineno}행: 모르는 동사 '{verb}' "
                            f"(가능한 것: {', '.join(sorted(verbs))})")
            continue
        low, high = verbs[verb][0], verbs[verb][1]
        if not (low <= len(args) <= high):
            problems.append(f"{lineno}행: {verbs[verb][2]}")
            continue
        steps.append((lineno, verb, args))

    if problems:
        raise ValueError("\n".join(problems))
    return steps


def run_harness(a):
    """스크립트를 검증하고, 한 줄씩 실행하고, 결과를 한 줄씩 찍습니다."""
    if a.script:
        with open(a.script, encoding="utf-8") as f:
            source = f.read()
    else:
        source = sys.stdin.read()

    # 전부 검증한 뒤에 실행합니다. 여섯 번째 줄의 오타 때문에 다섯 줄이 이미
    # 기기에 들어간 뒤 멈추면, 기기가 어중간한 상태로 남습니다.
    try:
        steps = parse_harness_script(source)
    except ValueError as bad:
        print(bad, file=sys.stderr)
        sys.exit(2)

    if not steps:
        sys.exit("실행할 동작이 없습니다 (스크립트가 비어 있습니다)")

    owner = a.owner
    if a.occupy:
        if not owner:
            sys.exit("--occupy를 쓰려면 --owner가 필요합니다")
        try:
            quiet_call(a.base, "POST", f"/api/device/{a.serial}/occupy",
                       {"owner": owner, "ttl_seconds": a.ttl})
        except HarnessStepError as e:
            # 대개 남이 잡고 있다는 뜻입니다. 스택 트레이스를 뱉을 일이 아닙니다.
            sys.exit(f"{a.serial}을(를) 점유하지 못했습니다: {e}")

    failed = None
    try:
        for lineno, verb, args in steps:
            try:
                result = run_harness_step(a, owner, verb, args)
            except HarnessStepError as e:
                failed = f"{lineno}행 {verb}: {e}"
                print(json.dumps({"line": lineno, "verb": verb, "status": "error",
                                  "message": str(e)}, ensure_ascii=False))
                break
            print(json.dumps({"line": lineno, "verb": verb, **result},
                             ensure_ascii=False))
    finally:
        if a.release and owner:
            try:
                quiet_call(a.base, "POST", f"/api/device/{a.serial}/release",
                           {"owner": owner}, allow_fail=True)
            except Exception:
                pass  # 반납 실패로 스크립트 결과를 덮지 않습니다. TTL이 처리합니다.

    if failed:
        print(f"중단: {failed}", file=sys.stderr)
        sys.exit(1)
    return None


class HarnessStepError(Exception):
    pass


def quiet_call(base, method, path, payload=None, allow_fail=False):
    """call()과 같지만 결과를 찍지 않고 돌려줍니다 (harness가 직접 찍습니다)."""
    try:
        resp = requests.request(method, f"{base}{path}", json=payload,
                                headers=auth_headers(), timeout=120)
    except requests.RequestException as e:
        raise HarnessStepError(f"팜에 연결할 수 없습니다 ({base}): {e}")

    try:
        body = resp.json()
    except ValueError:
        raise HarnessStepError(f"HTTP {resp.status_code} {resp.text[:150]}")

    if not resp.ok and not allow_fail:
        message = body.get("message") if isinstance(body, dict) else None
        raise HarnessStepError(message or f"HTTP {resp.status_code}")
    return body


def run_harness_step(a, owner, verb, args):
    import time as _time

    serial = a.serial
    def owned(body):
        if owner:
            body["owner"] = owner
        return body

    if verb == "sleep":
        _time.sleep(float(args[0]))
        return {"status": "success", "slept": float(args[0])}

    if verb == "screenshot":
        out = args[0] if args else f"{serial}.png"
        try:
            resp = requests.get(f"{a.base}/api/device/{serial}/screenshot",
                                params={"full": 1}, headers=auth_headers(), timeout=60)
        except requests.RequestException as e:
            raise HarnessStepError(str(e))
        if not resp.ok:
            raise HarnessStepError(f"HTTP {resp.status_code}")
        with open(out, "wb") as f:
            f.write(resp.content)
        return {"status": "success", "saved": out, "bytes": len(resp.content)}

    if verb in ("elements", "ocr"):
        body = quiet_call(a.base, "GET", f"/api/agent/{serial}/elements")
        # 전문을 찍으면 화면 하나가 수백 줄입니다. 에이전트가 읽는 건 글자와
        # 좌표뿐이라 그것만 남깁니다.
        return {"status": "success", "count": body.get("count", 0),
                "elements": [{"text": e["text"] or e["content_desc"],
                              "center": e["center"]}
                             for e in body.get("elements", [])]}

    if verb == "open_app":
        target = args[0]
        # 안드로이드 패키지명에는 점이 있고 iOS 앱 이름에는 보통 없습니다.
        # 서버가 최종 판단을 하니 여기서는 둘 다 실어 보냅니다.
        return quiet_call(a.base, "POST", f"/api/agent/{serial}/open-app",
                          owned({"package": target} if "." in target else {"name": target}))

    if verb == "tap_text":
        body = {"text": args[0]}
        if len(args) > 1:
            body["index"] = int(args[1])
        return quiet_call(a.base, "POST", f"/api/agent/{serial}/tap-text", owned(body))

    if verb == "type_text":
        return quiet_call(a.base, "POST", f"/api/agent/{serial}/type-text",
                          owned({"text": args[0]}))

    if verb == "wait_stable":
        body = {"timeout": float(args[0])} if args else {}
        return quiet_call(a.base, "POST", f"/api/agent/{serial}/wait-stable", owned(body))

    if verb == "tap":
        return quiet_call(a.base, "POST", f"/api/device/{serial}/input",
                          owned({"type": "tap", "x": int(args[0]), "y": int(args[1])}))

    if verb == "swipe":
        body = {"type": "swipe", "x1": int(args[0]), "y1": int(args[1]),
                "x2": int(args[2]), "y2": int(args[3])}
        if len(args) > 4:
            body["duration"] = int(args[4])
        return quiet_call(a.base, "POST", f"/api/device/{serial}/input", owned(body))

    if verb == "key":
        return quiet_call(a.base, "POST", f"/api/device/{serial}/input",
                          owned({"type": "key", "keycode": int(args[0])}))

    raise HarnessStepError(f"구현되지 않은 동사: {verb}")


# --- skill: 에이전트에게 이 팜을 설명하는 문서 ---

SKILL_TEMPLATE = """---
name: qa-device-farm
description: >-
  실기기 안드로이드/아이폰을 원격으로 조작합니다. 기기 목록 확인, 앱 실행,
  화면 읽기, 탭·입력, 스크린샷, 로그 수집이 필요할 때 사용하세요.
  "디바이스 팜", "실기기 테스트", "폰에서 확인해줘", "앱 켜서 눌러봐" 같은
  요청이 이 스킬의 대상입니다.
---

# QA Device Farm

USB로 연결된 안드로이드 기기와(맥이라면) 미러링 중인 아이폰을 HTTP 하나로
조작합니다. 세션이 없습니다 — 동작 하나가 요청 하나입니다.

- 팜 주소: `{base}`
- 토큰이 걸려 있으면 `DEVICE_FARM_TOKEN` 환경변수로 넘어갑니다.
- 아래 명령은 전부 이 저장소의 `cli.py`로 실행합니다.

## 반드시 지킬 것

1. **조작 전에 점유하고, 끝나면 반납합니다.** 이 팜은 사람과 CI가 같이
   씁니다. 점유하지 않고 누르면 남의 테스트 회차를 밟습니다.
2. **409가 오면 기다립니다.** 다른 사람이 쓰는 중이라는 뜻이고, 재시도를
   반복해도 뺏어오지 못합니다.
3. **좌표보다 글자를 먼저 씁니다.** `elements`로 보고 `tap_text`로 누르세요.
   좌표는 글자가 없는 아이콘에만 씁니다.
4. **화면을 바꾼 뒤에는 `wait_stable`을 부릅니다.** 전환 애니메이션 중에 화면을
   읽으면 이전 화면이 잡힙니다.

## 기본 흐름

```bash
# 1. 어떤 기기가 있는지
python cli.py devices

# 2. 하나 잡기 (--serial 없이 부르면 유휴 기기를 아무거나 줍니다)
python cli.py occupy --owner ai-agent --ttl 600

# 3. 조작 — 한 번에 여러 줄
python cli.py harness --serial <시리얼> --owner ai-agent <<'EOF'
open_app com.android.settings
wait_stable
tap_text "네트워크"
wait_stable
elements
EOF

# 4. 반납
python cli.py release --serial <시리얼> --owner ai-agent
```

`--occupy --release`를 harness에 같이 주면 2·4번을 알아서 합니다.

## 동사

| 동사 | 하는 일 |
|---|---|
| `open_app <패키지\\|앱이름>` | 앱 실행. 안드로이드는 `com.example.app`, iOS는 `Notes` |
| `elements` | 화면에 보이는 글자와 누를 좌표 |
| `tap_text <글자> [n]` | 그 글자가 있는 것을 누름. 여러 개면 n번째 |
| `type_text <문자열>` | 텍스트 입력 |
| `tap <x> <y>` / `swipe <x1> <y1> <x2> <y2> [ms]` | 좌표 조작 |
| `key <키코드>` | 안드로이드 키 (뒤로가기=4, 홈=3) |
| `wait_stable [초]` | 화면이 멈출 때까지 대기 |
| `screenshot [파일]` | 원본 해상도 PNG로 저장 |
| `sleep <초>` | 그냥 대기 |

개별 호출이 필요하면 같은 이름의 서브커맨드가 있습니다:
`python cli.py tap-text --serial <시리얼> --text "확인"`.

## 화면을 읽는 방법

`elements`는 안드로이드에서 uiautomator(기기의 뷰 트리)를, iOS에서 macOS Vision
OCR을 씁니다. 결과 형식은 같습니다: 글자와 그것을 누를 좌표.

글자가 안 잡히면 `screenshot`으로 그림을 직접 보고 좌표로 누르세요. 다음 경우에
그렇습니다:

- 안드로이드: WebView 안, 게임 화면, `FLAG_SECURE`가 걸린 화면(금융 앱 등)
- iOS: 아이콘만 있고 글자가 없는 버튼

## 알아둘 것

- **안드로이드 텍스트 입력은 ASCII만** 됩니다. 한글이나 이모지는 기기에 IME가
  필요합니다. iOS는 제한이 없습니다.
- **iOS는 `ios-mirror` 한 대뿐**이고, 맥에서 아이폰 미러링 창이 열려 있어야
  합니다. `devices`에 안 보이면 그 팜은 맥이 아니거나 phone-harness가 없는
  겁니다. `python cli.py health`의 `ios.reason`이 이유를 말해줍니다.
- **iOS는 느립니다.** 동작 하나에 수 초가 걸립니다. 조급하게 재시도하지 마세요.
- **APK 설치·logcat·배치 작업은 안드로이드 전용**입니다.

## 로그 확인

```bash
python cli.py logcat start --serial <시리얼>
# ... 재현 조작 ...
python cli.py logcat tail --serial <시리얼> --lines 200
```

크래시가 감지되면 종료 코드 2로 끝납니다.
"""


def write_skill(a):
    base = a.base
    content = SKILL_TEMPLATE.format(base=base)

    if a.out:
        target = a.out
    elif a.target == "claude":
        target = os.path.join(os.path.expanduser("~"), ".claude", "skills",
                              "qa-device-farm", "SKILL.md")
    elif a.target == "codex":
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(
            os.path.expanduser("~"), ".codex")
        target = os.path.join(codex_home, "skills", "qa-device-farm", "SKILL.md")
    else:
        sys.stdout.write(content)
        return None

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    print(target)
    return None


def main():
    p = argparse.ArgumentParser(description="QA Device Farm CLI")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"farm URL (default {DEFAULT_BASE})")
    p.add_argument("--token", help="access token (default: $DEVICE_FARM_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list connected devices")
    sub.add_parser("health", help="server and adb status")
    sub.add_parser("leases", help="who holds what")

    occupy = sub.add_parser("occupy", help="claim a device")
    occupy.add_argument("--serial", help="specific device; omit to take any free one")
    occupy.add_argument("--owner", required=True)
    occupy.add_argument("--ttl", type=int, default=600, help="lease seconds (default 600)")

    release = sub.add_parser("release", help="hand a device back")
    release.add_argument("--serial", required=True)
    release.add_argument("--owner", required=True)

    tap = sub.add_parser("tap")
    tap.add_argument("--serial", required=True)
    tap.add_argument("--x", type=int, required=True)
    tap.add_argument("--y", type=int, required=True)
    tap.add_argument("--owner")

    swipe = sub.add_parser("swipe")
    swipe.add_argument("--serial", required=True)
    swipe.add_argument("--x1", type=int, required=True)
    swipe.add_argument("--y1", type=int, required=True)
    swipe.add_argument("--x2", type=int, required=True)
    swipe.add_argument("--y2", type=int, required=True)
    swipe.add_argument("--duration", type=int, default=300)
    swipe.add_argument("--owner")

    key = sub.add_parser("key")
    key.add_argument("--serial", required=True)
    key.add_argument("--keycode", type=int, required=True)
    key.add_argument("--owner")

    text = sub.add_parser("text", help="type ASCII text")
    text.add_argument("--serial", required=True)
    text.add_argument("--value", required=True)
    text.add_argument("--owner")

    shot = sub.add_parser("screenshot")
    shot.add_argument("--serial", required=True)
    shot.add_argument("--out", default="screenshot.jpg")

    appc = sub.add_parser("app", help="launch / stop / clear an app")
    appc.add_argument("--serial", required=True)
    appc.add_argument("--action", required=True, choices=["launch", "stop", "clear"])
    appc.add_argument("--package", required=True)
    appc.add_argument("--owner")

    sub.add_parser("macros", help="list saved macros")

    macrm = sub.add_parser("macro-delete")
    macrm.add_argument("--name", required=True)

    log = sub.add_parser("logcat", help="capture device logs")
    log.add_argument("action", choices=["start", "stop", "tail", "save", "status"])
    log.add_argument("--serial")
    log.add_argument("--level", default="V", choices=["V", "D", "I", "W", "E", "F"])
    log.add_argument("--contains", help="only lines containing this text")
    log.add_argument("--lines", type=int, default=200)
    log.add_argument("--out", default=None, help="file for `save` (default logcat_<serial>.txt)")
    log.add_argument("--owner")
    log.add_argument("--to-file", action="store_true",
                     help="also write the capture to logs/ on the server "
                          "(the memory buffer is capped and drops old lines)")

    # Batch verbs take --serials as a comma-separated list.
    bapp = sub.add_parser("batch-app")
    bapp.add_argument("--serials", required=True)
    bapp.add_argument("--action", required=True, choices=["launch", "stop", "clear"])
    bapp.add_argument("--package", required=True)
    bapp.add_argument("--owner")

    bmac = sub.add_parser("batch-macro")
    bmac.add_argument("--serials", required=True)
    bmac.add_argument("--name", required=True)
    bmac.add_argument("--count", type=int, default=1)
    bmac.add_argument("--owner")

    bins = sub.add_parser("batch-install")
    bins.add_argument("--serials", required=True)
    bins.add_argument("--apk", required=True)
    bins.add_argument("--owner")

    # --- AI 에이전트용 ---
    # 좌표 대신 화면에 보이는 것으로 말하는 동사들입니다.
    els = sub.add_parser("elements", help="화면에 보이는 요소와 좌표")
    els.add_argument("--serial", required=True)

    tapt = sub.add_parser("tap-text", help="이 글자가 있는 것을 누릅니다")
    tapt.add_argument("--serial", required=True)
    tapt.add_argument("--text", required=True)
    tapt.add_argument("--index", type=int, default=0, help="같은 글자가 여러 개일 때 몇 번째")
    tapt.add_argument("--exact", action="store_true", help="부분 일치를 허용하지 않음")
    tapt.add_argument("--owner")

    typet = sub.add_parser("type-text", help="텍스트 입력 (안드로이드는 ASCII만)")
    typet.add_argument("--serial", required=True)
    typet.add_argument("--value", required=True)
    typet.add_argument("--owner")

    opena = sub.add_parser("open-app", help="앱 실행")
    opena.add_argument("--serial", required=True)
    opena.add_argument("--package", help="안드로이드 패키지명")
    opena.add_argument("--name", help="iOS 앱 이름")
    opena.add_argument("--owner")

    waits = sub.add_parser("wait-stable", help="화면이 멈출 때까지 대기")
    waits.add_argument("--serial", required=True)
    waits.add_argument("--timeout", type=float, default=10.0)
    waits.add_argument("--interval", type=float, default=0.5)
    waits.add_argument("--owner")

    harness = sub.add_parser(
        "harness",
        help="여러 동작을 한 번에 실행 (stdin 또는 --script)",
        description="한 줄에 동사 하나. phone-harness의 heredoc과 같은 사용감입니다.")
    harness.add_argument("--serial", required=True)
    harness.add_argument("--owner")
    harness.add_argument("--script", help="스크립트 파일 (기본: stdin)")
    harness.add_argument("--occupy", action="store_true", help="실행 전에 기기를 점유")
    harness.add_argument("--release", action="store_true", help="끝나면 반납")
    harness.add_argument("--ttl", type=int, default=600, help="--occupy의 점유 시간(초)")

    skill = sub.add_parser("skill", help="에이전트용 SKILL.md 생성")
    skill.add_argument("--target", choices=["claude", "codex", "stdout"], default="stdout")
    skill.add_argument("--out", help="직접 경로를 지정 (--target보다 우선)")

    a = p.parse_args()
    global TOKEN
    if a.token:
        TOKEN = a.token.strip()

    if a.cmd == "devices":
        return call(a.base, "GET", "/api/devices")
    if a.cmd == "health":
        return call(a.base, "GET", "/api/health")
    if a.cmd == "leases":
        return call(a.base, "GET", "/api/leases")

    if a.cmd == "occupy":
        body = {"owner": a.owner, "ttl_seconds": a.ttl}
        path = f"/api/device/{a.serial}/occupy" if a.serial else "/api/devices/occupy"
        return call(a.base, "POST", path, body)

    if a.cmd == "release":
        return call(a.base, "POST", f"/api/device/{a.serial}/release", {"owner": a.owner})

    if a.cmd == "screenshot":
        url = f"{a.base}/api/device/{a.serial}/screenshot"
        resp = requests.get(url, headers=auth_headers(), timeout=30)
        if not resp.ok:
            sys.exit(f"screenshot failed: {resp.status_code}")
        with open(a.out, "wb") as f:
            f.write(resp.content)
        print(a.out)
        return None

    if a.cmd == "app":
        body = {"package": a.package}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", f"/api/app/{a.serial}/{a.action}", body)

    if a.cmd == "macros":
        return call(a.base, "GET", "/api/macros")

    if a.cmd == "macro-delete":
        return call(a.base, "DELETE", f"/api/macros/{a.name}")

    if a.cmd == "logcat":
        if a.action == "status":
            return call(a.base, "GET", "/api/logcat")
        if not a.serial:
            sys.exit("logcat %s needs --serial" % a.action)
        if a.action == "start":
            body = {"clear": True, "level": a.level, "to_file": a.to_file}
            if a.owner:
                body["owner"] = a.owner
            return call(a.base, "POST", f"/api/logcat/{a.serial}/start", body)
        if a.action == "stop":
            return call(a.base, "POST", f"/api/logcat/{a.serial}/stop", {})
        if a.action == "save":
            resp = requests.get(f"{a.base}/api/logcat/{a.serial}/download",
                                headers=auth_headers(), timeout=60)
            if not resp.ok:
                sys.exit(f"download failed: {resp.status_code} {resp.text[:200]}")
            out = a.out or f"logcat_{a.serial}.txt"
            with open(out, "wb") as f:
                f.write(resp.content)
            print(out)
            return None
        # tail: print the log itself rather than a JSON blob, and make a crash
        # fail the step so CI notices without anyone reading the output.
        params = {"tail": a.lines}
        if a.contains:
            params["contains"] = a.contains
        resp = requests.get(f"{a.base}/api/logcat/{a.serial}", params=params,
                            headers=auth_headers(), timeout=30)
        if not resp.ok:
            sys.exit(f"not capturing on {a.serial} (start it first)")
        d = resp.json()
        for line in d["lines"]:
            print(line)
        if d["crashes"]:
            print(f"\n!! {len(d['crashes'])} crash(es) detected:", file=sys.stderr)
            for cr in d["crashes"]:
                print(f"   [{cr['kind']}] {cr['line']}", file=sys.stderr)
            sys.exit(2)
        return None

    if a.cmd == "elements":
        return call(a.base, "GET", f"/api/agent/{a.serial}/elements")

    if a.cmd == "tap-text":
        body = {"text": a.text, "index": a.index, "exact": a.exact}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", f"/api/agent/{a.serial}/tap-text", body)

    if a.cmd == "type-text":
        body = {"text": a.value}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", f"/api/agent/{a.serial}/type-text", body)

    if a.cmd == "open-app":
        if not (a.package or a.name):
            sys.exit("open-app은 --package(안드로이드) 또는 --name(iOS)이 필요합니다")
        body = {}
        if a.package:
            body["package"] = a.package
        if a.name:
            body["name"] = a.name
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", f"/api/agent/{a.serial}/open-app", body)

    if a.cmd == "wait-stable":
        body = {"timeout": a.timeout, "interval": a.interval}
        if a.owner:
            body["owner"] = a.owner
        # 화면이 끝내 멈추지 않은 것은 서버 오류가 아니라 결과입니다. CI가 그걸로
        # 스텝을 가를 수 있게 종료 코드를 따로 씁니다.
        body = call(a.base, "POST", f"/api/agent/{a.serial}/wait-stable", body)
        if isinstance(body, dict) and body.get("stable") is False:
            sys.exit(3)
        return body

    if a.cmd == "harness":
        return run_harness(a)

    if a.cmd == "skill":
        return write_skill(a)

    if a.cmd == "batch-app":
        body = {"serials": split_serials(a.serials), "action": a.action, "package": a.package}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", "/api/batch/app", body)

    if a.cmd == "batch-macro":
        body = {"serials": split_serials(a.serials), "name": a.name, "count": a.count}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", "/api/batch/macro", body)

    if a.cmd == "batch-install":
        data = {"serials": ",".join(split_serials(a.serials))}
        if a.owner:
            data["owner"] = a.owner
        with open(a.apk, "rb") as fh:
            resp = requests.post(f"{a.base}/api/batch/install", data=data,
                                 files={"file": (os.path.basename(a.apk), fh)},
                                 headers=auth_headers(), timeout=600)
        body = resp.json()
        print(json.dumps(body, indent=2, ensure_ascii=False))
        # A partial batch is a failure for a pipeline, even though HTTP said 200.
        if not resp.ok or body.get("status") == "partial":
            sys.exit(1)
        return body

    # Build only the payload for the command that ran. Each input subparser
    # declares its own arguments, so touching another one's (a.x for `key`)
    # raises AttributeError before anything is sent.
    if a.cmd == "tap":
        payload = {"type": "tap", "x": a.x, "y": a.y}
    elif a.cmd == "swipe":
        payload = {"type": "swipe", "x1": a.x1, "y1": a.y1,
                   "x2": a.x2, "y2": a.y2, "duration": a.duration}
    elif a.cmd == "key":
        payload = {"type": "key", "keycode": a.keycode}
    elif a.cmd == "text":
        payload = {"type": "text", "text": a.value}
    else:
        sys.exit(f"unhandled command: {a.cmd}")

    if a.owner:
        payload["owner"] = a.owner
    return call(a.base, "POST", f"/api/device/{a.serial}/input", payload)


if __name__ == "__main__":
    main()
