"""iOS 프로바이더를 맥 없이 검증합니다.

phone-harness 어댑터를 가짜로 갈아끼웁니다 — 기존 테스트가 `server.adb_exec`를
갈아끼우는 것과 같은 방식입니다. 실제 맥에서 확인해야 하는 것(미러링 창을
정말 누르는지, Vision OCR 결과 형식)은 여기서 알 수 없고 수동 검증 항목입니다.

여기서 확인하는 것은 팜 쪽 계약입니다: 아이폰이 기기 목록에 어떻게 실리고,
점유가 걸리고, 안드로이드 전용 기능이 어떻게 거절되고, 무엇보다 **iOS가 없는
호스트에서 안드로이드가 그대로 도는가**.
"""
import sys, os, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.chdir(tempfile.mkdtemp(prefix="farm_ios_"))

import server
from fastapi.testclient import TestClient

class FakeDev:
    def __init__(self, serial): self.serial = serial
class FakeInfo:
    def __init__(self, serial, state): self.serial, self.state = serial, state
server.adb.device_list = lambda: [FakeDev("DROID_A")]
server.adb.list = lambda: [FakeInfo("DROID_A", "device")]
server.device_leases.clear()

adb_calls = []
async def fake_exec(adb_path, serial, *args):
    adb_calls.append((serial,) + args)
server.adb_exec = fake_exec
server.device_detail = lambda d, refresh=False: {
    "model": "Pixel", "version": "14", "sdk": "34",
    "width": 1080, "height": 2400, "ip": "1.2.3.4", "battery": "88%"}

class FakeAdapter:
    """phone-harness 대신. 무엇이 불렸는지만 기록합니다."""
    def __init__(self):
        self.calls = []
        self._size = (1170, 2532)
        self.ocr_items = [
            {"text": "새로운 메모", "center": {"x": 100, "y": 200},
             "bounds": {"x1": 50, "y1": 180, "x2": 150, "y2": 220},
             "content_desc": "", "resource_id": "", "class": "ocr",
             "clickable": True, "enabled": True},
        ]
    async def tap(self, x, y): self.calls.append(("tap", x, y))
    async def swipe(self, x1, y1, x2, y2, duration=300):
        self.calls.append(("swipe", x1, y1, x2, y2, duration))
    async def type_text(self, text):
        # 진짜 어댑터와 같은 제약: phone-harness가 US 배열 키코드로 칩니다.
        if not str(text).isascii():
            raise ValueError("iOS 입력도 ASCII만 됩니다")
        self.calls.append(("type_text", text))
    async def open_app(self, name): self.calls.append(("open_app", name))
    async def ocr(self):
        self.calls.append(("ocr",))
        return self.ocr_items
    async def screenshot(self, full=True):
        self.calls.append(("screenshot", full))
        return b"\x89PNG\r\n\x1a\n" + b"0" * 40
    async def screen_size(self):
        return self._size
    async def connection_state(self):
        self.calls.append(("connection_state",))
        return "ready"

fake = FakeAdapter()
server.ios.adapter = fake

# 진짜 감지 로직은 마지막 절에서 따로 확인합니다. server.ios는 ios_mirror
# 모듈 그 자체라, 갈아끼우기 전에 원본을 챙겨둬야 되돌릴 수 있습니다.
REAL_IOS_STATUS = server.ios.ios_status
REAL_IOS_AVAILABLE = server.ios.available

def set_ios(available, reason=None):
    server.ios.ios_status = lambda refresh=False: {
        "available": available, "binary": "/fake/phone-harness" if available else None,
        "reason": reason}
    server.ios.available = lambda: available

c = TestClient(server.app)
fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

IOS = server.ios.IOS_SERIAL

print("=== 맥이 아니거나 phone-harness가 없을 때 ===")
set_ios(False, "iOS 미러링은 macOS 호스트에서만 동작합니다 (현재 호스트: linux)")

r = c.get("/api/devices")
serials = [d["serial"] for d in r.json()["devices"]]
check("아이폰 칸이 아예 없습니다", IOS not in serials, str(serials))
check("안드로이드는 그대로 보입니다", "DROID_A" in serials, str(serials))
check("안드로이드에 platform이 붙습니다",
      r.json()["devices"][0]["platform"] == "android", r.text)

r = c.get(f"/api/device/{IOS}/screenshot")
check("스크린샷은 503", r.status_code == 503, r.text)
check("503이 이유를 말합니다", "macOS" in r.json()["message"], r.text)

r = c.get(f"/api/agent/{IOS}/elements")
check("elements도 503", r.status_code == 503, r.text)

r = c.post(f"/api/agent/{IOS}/tap-text", json={"text": "메모"})
check("tap-text도 503", r.status_code == 503, r.text)

r = c.post(f"/api/device/{IOS}/input", json={"type": "tap", "x": 1, "y": 2})
check("input도 503", r.status_code == 503, r.text)

r = c.get("/api/health")
check("health가 iOS 상태를 싣습니다", r.json()["ios"]["available"] is False, r.text)
check("health가 안드로이드 기준으로는 정상",
      r.json()["status"] in ("ok", "degraded"), r.text)

adb_calls.clear()
r = c.post("/api/device/DROID_A/input", json={"type": "tap", "x": 5, "y": 6})
check("안드로이드 조작은 영향 없음", r.status_code == 200, r.text)
check("adb로 정상적으로 나갑니다",
      adb_calls == [("DROID_A", "shell", "input", "tap", "5", "6")], str(adb_calls))

print()
print("=== 맥 + phone-harness가 있을 때 ===")
set_ios(True)

r = c.get("/api/devices")
devices = {d["serial"]: d for d in r.json()["devices"]}
check("아이폰이 기기로 실립니다", IOS in devices, str(list(devices)))
check("platform이 ios입니다", devices[IOS]["platform"] == "ios", str(devices.get(IOS)))
check("state는 device입니다", devices[IOS]["state"] == "device", str(devices.get(IOS)))
check("안드로이드도 여전히 있습니다", "DROID_A" in devices, str(list(devices)))

print()
print("=== 조작은 adb가 아니라 phone-harness로 ===")
fake.calls.clear(); adb_calls.clear()
r = c.post(f"/api/device/{IOS}/input", json={"type": "tap", "x": 10, "y": 20})
check("탭 200", r.status_code == 200, r.text)
check("어댑터가 불립니다", fake.calls == [("tap", 10, 20)], str(fake.calls))
check("adb는 전혀 불리지 않습니다", adb_calls == [], str(adb_calls))

fake.calls.clear()
r = c.post(f"/api/device/{IOS}/input",
           json={"type": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 250})
check("스와이프 200", r.status_code == 200, r.text)
check("스와이프가 어댑터로", fake.calls == [("swipe", 1, 2, 3, 4, 250)], str(fake.calls))

fake.calls.clear()
r = c.post(f"/api/device/{IOS}/input", json={"type": "text", "text": "hello world"})
check("텍스트 입력 200", r.status_code == 200, r.text)
check("문자열이 그대로 넘어갑니다 (안드로이드 같은 셸 인용이 없습니다)",
      fake.calls == [("type_text", "hello world")], str(fake.calls))

fake.calls.clear()
r = c.post(f"/api/device/{IOS}/input", json={"type": "text", "text": "한글"})
check("iOS도 한글은 400", r.status_code == 400, r.text)
check("400이 이유를 말합니다", "ASCII" in r.json()["message"], r.text)
check("400이면 미러링 창을 건드리지 않습니다", fake.calls == [], str(fake.calls))

fake.calls.clear()
r = c.post(f"/api/device/{IOS}/input", json={"type": "key", "keycode": 4})
check("keycode는 400", r.status_code == 400, r.text)
check("400이 대안을 알려줍니다", "open-app" in r.json()["message"], r.text)
check("400이면 어댑터를 부르지 않습니다", fake.calls == [], str(fake.calls))

print()
print("=== 점유는 안드로이드와 같은 규칙 ===")
r = c.post(f"/api/device/{IOS}/occupy", json={"owner": "ai-a", "ttl_seconds": 60})
check("아이폰도 점유됩니다", r.status_code == 200, r.text)
r = c.post(f"/api/device/{IOS}/occupy", json={"owner": "다른사람"})
check("점유 중이면 409", r.status_code == 409, r.text)

fake.calls.clear()
r = c.post(f"/api/agent/{IOS}/tap-text", json={"text": "메모", "owner": "intruder"})
check("남이 점유하면 verb도 409", r.status_code == 409, r.text)
check("409면 미러링 창을 건드리지 않습니다", fake.calls == [], str(fake.calls))

r = c.get("/api/devices")
entry = {d["serial"]: d for d in r.json()["devices"]}[IOS]
check("목록에 점유자가 보입니다", entry["occupied_by"] == "ai-a", str(entry))

r = c.post(f"/api/device/{IOS}/release", json={"owner": "ai-a"})
check("반납 200", r.status_code == 200, r.text)

print()
print("=== '아무거나 하나'는 아이폰을 주지 않습니다 ===")
# 아이폰은 GUI에 묶인 한 대뿐이고 맥 앞에 사람이 앉아 있을 수 있습니다.
# 파이프라인이 "안드로이드 한 대"를 달라고 했을 때 이게 나오면 안 됩니다.
c.post("/api/device/DROID_A/occupy", json={"owner": "ci-hog", "ttl_seconds": 60})
r = c.post("/api/devices/occupy", json={"owner": "ci-next"})
check("안드로이드가 다 차면 409", r.status_code == 409, r.text)
check("아이폰이 대신 나오지 않습니다", IOS not in r.text, r.text)
c.post("/api/device/DROID_A/release", json={"owner": "ci-hog"})

print()
print("=== AI verb는 두 플랫폼에서 같은 모양 ===")
fake.calls.clear()
r = c.get(f"/api/agent/{IOS}/elements")
check("elements 200", r.status_code == 200, r.text)
check("OCR이 불립니다", ("ocr",) in fake.calls, str(fake.calls))
body = r.json()
check("요소 형식이 안드로이드와 같습니다",
      body["elements"][0]["center"] == {"x": 100, "y": 200}, r.text)
check("화면 크기를 같이 줍니다", (body["width"], body["height"]) == (1170, 2532), r.text)

fake.calls.clear()
r = c.post(f"/api/agent/{IOS}/tap-text", json={"text": "새로운 메모"})
check("tap-text 200", r.status_code == 200, r.text)
check("OCR 좌표로 탭합니다", ("tap", 100, 200) in fake.calls, str(fake.calls))

fake.calls.clear()
r = c.post(f"/api/agent/{IOS}/open-app", json={"name": "Notes"})
check("이름으로 앱을 엽니다", r.status_code == 200, r.text)
check("어댑터에 이름이 갑니다", fake.calls == [("open_app", "Notes")], str(fake.calls))

fake.calls.clear()
r = c.post(f"/api/agent/{IOS}/open-app", json={"package": "com.apple.Notes"})
check("iOS에 package만 주면 400", r.status_code == 400, r.text)
check("400이 앱 이름을 요구합니다", "이름" in r.json()["message"], r.text)
check("400이면 아무것도 열지 않습니다", fake.calls == [], str(fake.calls))

print()
print("=== 안드로이드 전용 기능은 이유를 말하고 거절합니다 ===")
for path, method, label in [
    (f"/api/logcat/{IOS}/start", "POST", "logcat"),
    (f"/api/app/{IOS}/launch", "POST", "앱 제어"),
    (f"/api/wireless/{IOS}", "POST", "무선 디버깅"),
    (f"/api/packages/{IOS}", "GET", "패키지 목록"),
    (f"/api/info/{IOS}", "GET", "기기 정보"),
    (f"/api/device/{IOS}/reset-stream", "POST", "스트림 정리"),
]:
    r = c.request(method, path, json={})
    check(f"{label}은 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    check(f"{label} 거절 사유가 한국어", "안드로이드 전용" in r.text, r.text[:120])

# 조사는 받침을 보고 고릅니다 — 메시지 하나만 기계처럼 보이지 않도록.
check("받침이 있으면 '은'", server.topic_particle("logcat 수집") == "은")
check("받침이 없으면 '는'", server.topic_particle("스트림 정리") == "는")
check("한글이 아니면 둘 다 적습니다", server.topic_particle("APK") == "은(는)")
r = c.post(f"/api/logcat/{IOS}/start", json={})
check("메시지에 조사가 자연스럽게 붙습니다", "수집은 안드로이드" in r.text, r.text[:120])

print()
print("=== 배치에 섞이면 그 줄만 건너뜁니다 ===")
adb_calls.clear(); fake.calls.clear()
r = c.post("/api/batch/app", json={"serials": ["DROID_A", IOS],
                                   "action": "launch", "package": "com.x.y"})
body = r.json()
check("배치는 200으로 답합니다", r.status_code == 200, r.text)
check("전체가 실패하지 않습니다", body["succeeded"] == 1, r.text)
ios_row = [x for x in body["results"] if x["serial"] == IOS][0]
check("아이폰은 skipped", ios_row["status"] == "skipped", str(ios_row))
check("skipped 사유가 있습니다", "iOS" in ios_row["message"], str(ios_row))
check("안드로이드는 정상 실행됩니다", len(adb_calls) == 1, str(adb_calls))

print()
print("=== 상태 감지 로직 (실제 함수) ===")
import ios_mirror
ios_mirror.ios_status = REAL_IOS_STATUS
ios_mirror.available = REAL_IOS_AVAILABLE

check("ios-mirror 시리얼을 알아봅니다", ios_mirror.is_ios("ios-mirror"))
check("안드로이드 시리얼은 아닙니다", not ios_mirror.is_ios("R3CN30ABCDE"))

real_status = ios_mirror.ios_status(refresh=True)
if sys.platform != "darwin":
    check("맥이 아니면 사용 불가", real_status["available"] is False, str(real_status))
    check("사유가 호스트를 말합니다", "macOS" in real_status["reason"], str(real_status))
else:
    check("맥에서는 phone-harness 유무로 갈립니다",
          isinstance(real_status["available"], bool), str(real_status))

print()
print("=== OCR 결과 변환 (phone-harness 실제 형식) ===")
# phone-harness의 ocr()은 {text, confidence, x, y, w, h}를 주고 (x, y)가 이미
# **중심점**입니다. 스크립트가 이미지 픽셀로 되돌린 뒤 어댑터가 팜 형식으로
# 맞춥니다. 여기서는 어댑터의 변환만 확인합니다 (_run을 가로채서).
real = ios_mirror.PhoneHarnessAdapter()

async def fake_run(verb, env_extra=None, timeout=None):
    return {"ok": True, "img": [1170, 2532], "items": [
        {"text": "New Note", "confidence": 0.98,
         "cx": 100.4, "cy": 200.6, "w": 80.0, "h": 40.0},
        {"text": "   ", "confidence": 0.4, "cx": 5, "cy": 5, "w": 2, "h": 2},
    ]}
real._run = fake_run

import asyncio as _asyncio
els = _asyncio.run(real.ocr())
check("요소 하나만 남습니다 (공백 텍스트는 버림)", len(els) == 1, str(els))
check("중심점을 그대로 씁니다", els[0]["center"] == {"x": 100, "y": 201}, str(els[0]))
check("w/h로 bounds를 만듭니다",
      els[0]["bounds"] == {"x1": 60, "y1": 181, "x2": 140, "y2": 221}, str(els[0]))
check("안드로이드와 같은 키를 갖습니다",
      all(k in els[0] for k in ("text", "content_desc", "clickable", "center", "bounds")),
      str(els[0]))
check("OCR 응답에서 화면 크기를 받아둡니다", real._size == (1170, 2532), str(real._size))

print()
print("=== 연결 상태 ===")
check("ready면 사유가 없습니다", ios_mirror.STATE_HINTS["ready"] is None)
for state in ("not-running", "no-window", "blocked"):
    check(f"{state}에 한국어 사유가 있습니다", bool(ios_mirror.STATE_HINTS.get(state)))

ios_mirror._state_cache.update({"at": 0.0, "value": None})
check("확인한 적 없으면 캐시는 비어 있습니다", ios_mirror.cached_state() is None)
ios_mirror.note_state("blocked")
check("확인한 값은 캐시에 남습니다", ios_mirror.cached_state() == "blocked")

entry = ios_mirror.device_entry(state="blocked")
check("연결이 끊겼으면 state가 device가 아닙니다", entry["state"] == "disconnected", str(entry))
check("끊긴 이유를 목록에 적습니다", "아이폰을 잠그면" in entry["state_hint"], str(entry))
ready = ios_mirror.device_entry(state="ready")
check("ready면 평소처럼 device", ready["state"] == "device", str(ready))
check("ready면 사유가 없습니다", ready["state_hint"] is None, str(ready))

ios_mirror._state_cache.update({"at": 0.0, "value": None})

print()
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
