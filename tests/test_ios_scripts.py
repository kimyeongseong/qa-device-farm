"""phone-harness에 보낼 스크립트 템플릿을, 그쪽이 돌리는 방식 그대로 돌려봅니다.

phone-harness의 CLI는 stdin으로 받은 코드를 `exec(code, helpers_globals)`로
실행합니다(그쪽 `run.py`). 그래서 여기서도 같은 방식으로 실행하되, helpers만
**실제 시그니처를 그대로 흉내낸 가짜**로 바꿔 끼웁니다.

이 테스트가 잡는 것은 맥 없이도 확인 가능한 층입니다.

- 템플릿에 문법 오류나 오타가 있는가
- 존재하지 않는 전역을 부르는가 (`swipe(x1,y1,...)`처럼 이름은 있는데 시그니처가
  다른 경우가 실제로 있었습니다 — 그쪽 `swipe`는 방향 문자열을 받고, 좌표
  드래그는 `drag`입니다)
- **좌표계 변환이 맞는가.** phone-harness는 맥 화면 좌표로 말하고 팜은 스크린샷
  이미지 픽셀로 말합니다. 레티나 미러링 창에서 두 값은 2배 차이가 나고, 창을
  옮기면 원점도 달라집니다.

여전히 맥에서만 확인되는 것: 실제로 창이 눌리는가, Vision이 글자를 제대로
읽는가, 권한이 붙어 있는가.
"""
import contextlib, io, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ios_mirror as ios

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# --- 가짜 helpers (phone-harness의 실제 시그니처) ---
# 창은 화면 (100, 50)에 있고 390x844 포인트, 캡처는 2배 해상도라 780x1688 픽셀.
# 원점이 0이 아니고 배율이 1이 아닌 조합이라야 변환 실수가 드러납니다.
calls = []
WIN = {"x": 100, "y": 50, "w": 390, "h": 844, "id": 7}
IMG = [780, 1688]

def screen_info():
    return {"window": WIN, "frontmost": True, "img_px": IMG}

def tap(x, y):
    calls.append(("tap", round(x, 2), round(y, 2)))

def drag(x1, y1, x2, y2, duration=0.35, steps=14):
    calls.append(("drag", round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2), duration))

def swipe(direction, distance=0.4):
    # 좌표를 이쪽으로 보내면 안 됩니다. 실수하면 여기서 드러나라고 둡니다.
    calls.append(("swipe", direction, distance))

def type_text(text, delay=0.03):
    calls.append(("type_text", text))

def open_app(name):
    calls.append(("open_app", name))

def wait_stable(timeout=6.0, interval=0.5, settle=2):
    calls.append(("wait_stable", timeout))
    return True

def screenshot(path=None):
    calls.append(("screenshot", path))
    return path

def connection_state():
    return "ready"

def ocr(min_confidence=0.3):
    """실제 recognize()가 주는 형식: (x, y)가 **화면 포인트 기준 중심점**."""
    return [{"text": "New Note", "confidence": 0.97,
             "x": WIN["x"] + 195.0, "y": WIN["y"] + 422.0, "w": 60.0, "h": 20.0}]

HELPERS = {k: v for k, v in list(globals().items())
           if not k.startswith("_") and k not in ("check", "fails", "ios", "ROOT")}


def run(verb, env=None):
    """phone-harness의 run.py가 하는 것과 같은 실행."""
    g = dict(HELPERS)
    g["__name__"] = "__main__"
    saved = dict(os.environ)
    os.environ.update({k: str(v) for k, v in (env or {}).items()})
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(ios.SCRIPTS[verb], g)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    marked = [l for l in buf.getvalue().splitlines() if l.startswith("__FARM__")]
    if not marked:
        raise AssertionError(f"{verb}: __FARM__ 결과 줄이 없습니다: {buf.getvalue()[:200]}")
    return json.loads(marked[-1][len("__FARM__"):])


print("=== 모든 템플릿이 실행되고 결과를 돌려줍니다 ===")
ENVS = {
    "tap": {"PH_X": 0, "PH_Y": 0},
    "swipe": {"PH_X1": 0, "PH_Y1": 0, "PH_X2": 1, "PH_Y2": 1, "PH_DURATION": 300},
    "type_text": {"PH_TEXT": "x"},
    "open_app": {"PH_NAME": "Notes"},
    "ocr": {},
    "screenshot": {"PH_OUT": "/tmp/farm-ios-test.png"},
    "wait_stable": {"PH_TIMEOUT": 1},
    "state": {},
}
check("템플릿 목록이 전부 실행 대상입니다",
      set(ENVS) == set(ios.SCRIPTS), str(set(ios.SCRIPTS) ^ set(ENVS)))
for verb, env in ENVS.items():
    calls.clear()
    try:
        result = run(verb, env)
        check(f"{verb} 실행 + ok", result.get("ok") is True, str(result))
    except Exception as e:
        check(f"{verb} 실행 + ok", False, f"{type(e).__name__}: {e}")

print()
print("=== 좌표 변환: 이미지 픽셀 -> 화면 포인트 ===")
calls.clear(); run("tap", {"PH_X": 390, "PH_Y": 844})
check("이미지 정중앙이 창 정중앙으로",
      calls == [("tap", 295.0, 472.0)], str(calls))

calls.clear(); run("tap", {"PH_X": 0, "PH_Y": 0})
check("이미지 원점이 창 원점으로 (창 위치가 더해집니다)",
      calls == [("tap", 100.0, 50.0)], str(calls))

calls.clear(); run("swipe", {"PH_X1": 0, "PH_Y1": 0,
                             "PH_X2": 780, "PH_Y2": 1688, "PH_DURATION": 250})
check("좌표 스와이프는 drag()입니다 (swipe()는 방향 문자열용)",
      calls and calls[0][0] == "drag", str(calls))
check("양 끝점이 창 모서리로 변환됩니다",
      calls == [("drag", 100.0, 50.0, 490.0, 894.0, 0.25)], str(calls))

calls.clear(); run("swipe", {"PH_X1": 10, "PH_Y1": 10, "PH_X2": 20, "PH_Y2": 20,
                             "PH_DURATION": 10})
check("아주 짧은 지속시간도 drag가 받아들이는 값으로",
      calls[0][5] >= 0.05, str(calls))

print()
print("=== OCR: 화면 포인트 -> 이미지 픽셀 (반대 방향) ===")
result = run("ocr")
item = result["items"][0]
check("중심점이 이미지 픽셀로 돌아옵니다",
      (round(item["cx"]), round(item["cy"])) == (390, 844), str(item))
check("크기도 이미지 픽셀 기준입니다",
      (item["w"], item["h"]) == (120.0, 40.0), str(item))
check("글자를 그대로 싣습니다", item["text"] == "New Note", str(item))
check("이미지 크기를 같이 알려줍니다", result["img"] == [780.0, 1688.0], str(result))

# 왕복이 맞아야 elements의 중심점을 그대로 tap에 넣을 수 있습니다.
calls.clear(); run("tap", {"PH_X": round(item["cx"]), "PH_Y": round(item["cy"])})
check("elements가 준 좌표를 그대로 tap하면 원래 자리로 돌아갑니다",
      calls == [("tap", 295.0, 472.0)], str(calls))

print()
print("=== 나머지 값 전달 ===")
calls.clear(); result = run("wait_stable", {"PH_TIMEOUT": 3})
check("wait_stable 결과를 bool로 싣습니다", result["stable"] is True, str(result))
check("timeout이 초 단위로 전달됩니다", calls == [("wait_stable", 3.0)], str(calls))

calls.clear(); run("screenshot", {"PH_OUT": "/tmp/farm-ios-test.png"})
check("스크린샷 경로가 전달됩니다",
      calls == [("screenshot", "/tmp/farm-ios-test.png")], str(calls))

calls.clear(); run("type_text", {"PH_TEXT": "hello world"})
check("텍스트는 셸 인용 없이 그대로 (안드로이드와 다릅니다)",
      calls == [("type_text", "hello world")], str(calls))

calls.clear(); run("open_app", {"PH_NAME": "Notes"})
check("앱 이름이 그대로", calls == [("open_app", "Notes")], str(calls))

check("연결 상태를 그대로 돌려줍니다", run("state")["state"] == "ready")

print()
print("=== 사용자 값이 코드가 되지 않습니다 ===")
# 템플릿은 상수이고 값은 환경변수로만 들어갑니다. 파이썬 코드처럼 생긴 문자열이
# 와도 문자열입니다.
calls.clear()
run("type_text", {"PH_TEXT": "'); __import__('os').system('touch /tmp/pwned'); ('"})
check("파이썬처럼 생긴 입력도 문자열로 전달됩니다",
      calls == [("type_text", "'); __import__('os').system('touch /tmp/pwned'); ('")],
      str(calls))
check("코드가 실행되지 않았습니다", not os.path.exists("/tmp/pwned"))

# 값이 템플릿에 끼워지는 경로가 아예 없어야 합니다. 템플릿은 모듈 상수이고,
# 값은 PH_* 환경변수로만 들어옵니다.
import re
for verb, script in ios.SCRIPTS.items():
    envs = set(re.findall(r'os\.environ\[[\'"]([A-Za-z_]+)[\'"]\]', script))
    check(f"{verb}는 PH_* 환경변수만 읽습니다",
          all(e.startswith("PH_") for e in envs), str(envs))
    check(f"{verb} 템플릿에 문자열 치환 자리가 없습니다",
          "%s" not in script and ".format(" not in script, verb)

print()
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
