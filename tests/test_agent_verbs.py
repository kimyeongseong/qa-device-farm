"""AI 에이전트 verb들을 기기 없이 검증합니다.

uiautomator 덤프와 스크린샷은 가짜로 갈아끼웁니다. 실기기에서만 확인할 수 있는
것(벤더별 `input text` 인용 처리, uiautomator가 실제로 뭘 못 보는지)은 여기서
확인할 수 없고, README의 수동 검증 항목으로 남깁니다.
"""
import sys, os, io, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.chdir(tempfile.mkdtemp(prefix="farm_agent_"))

import server
from fastapi.testclient import TestClient

class FakeDev:
    def __init__(self, serial): self.serial = serial
class FakeInfo:
    def __init__(self, serial, state): self.serial, self.state = serial, state
server.adb.device_list = lambda: [FakeDev("AGENT_A")]
server.adb.list = lambda: [FakeInfo("AGENT_A", "device")]
server.device_leases.clear()

calls = []
async def fake_exec(adb_path, serial, *args):
    calls.append((serial,) + args)
server.adb_exec = fake_exec

# 화면에 뜬 것: 한글, 공백이 있는 라벨, 음수 bounds(화면 밖으로 걸친 요소),
# 글자가 없는 컨테이너, content-desc만 있는 아이콘.
DUMP_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node index="0" text="설정" resource-id="com.x:id/title" class="android.widget.TextView"
          clickable="false" enabled="true" bounds="[40,100][300,180]" />
    <node index="1" text="네트워크 및 인터넷" resource-id="com.x:id/net"
          class="android.widget.Button" clickable="true" enabled="true"
          bounds="[0,200][1080,320]" />
    <node index="2" text="" content-desc="뒤로" class="android.widget.ImageButton"
          clickable="true" enabled="true" bounds="[-10,90][80,190]" />
    <node index="3" text="Save As" class="android.widget.Button" clickable="true"
          enabled="true" bounds="[100,400][500,500]" />
    <node index="4" text="" class="android.widget.LinearLayout" bounds="[0,600][1080,700]" />
  </node>
</hierarchy>
UI hierchary dumped to: /dev/tty"""

async def fake_capture(adb_path, serial, *args):
    calls.append((serial,) + args)
    return DUMP_XML
server.adb_capture = fake_capture
server.get_device_resolution = lambda serial: (1080, 2400)

c = TestClient(server.app)
fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

print("=== uiautomator 덤프 파싱 ===")
els = server.parse_ui_dump(DUMP_XML)
texts = [e["text"] or e["content_desc"] for e in els]
check("글자 없는 컨테이너는 버립니다", len(els) == 4, str(texts))
check("한글 텍스트를 읽습니다", "설정" in texts, str(texts))
check("content-desc만 있어도 잡습니다", "뒤로" in texts, str(texts))
net = next(e for e in els if e["text"] == "네트워크 및 인터넷")
check("bounds에서 center를 계산합니다",
      net["center"] == {"x": 540, "y": 260}, str(net))
check("clickable을 읽습니다", net["clickable"] is True, str(net))
back = next(e for e in els if e["content_desc"] == "뒤로")
check("음수 bounds도 파싱합니다", back["bounds"]["x1"] == -10, str(back))
check("덤프 뒤 안내문이 붙어도 파싱합니다", len(els) == 4, str(texts))

bad = server.parse_bounds("이건 bounds가 아닙니다")
check("bounds가 아니면 None", bad is None, str(bad))

try:
    server.parse_ui_dump("완전히 다른 출력")
    check("XML이 없으면 ValueError", False, "예외가 없었습니다")
except ValueError:
    check("XML이 없으면 ValueError", True)

print()
print("=== elements 엔드포인트 ===")
calls.clear()
r = c.get("/api/agent/AGENT_A/elements")
check("elements 200", r.status_code == 200, r.text)
body = r.json()
check("count가 요소 수와 같습니다", body["count"] == 4, r.text)
check("해상도를 같이 돌려줍니다",
      (body["width"], body["height"]) == (1080, 2400), r.text)

print()
print("=== tap_text ===")
calls.clear()
r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "네트워크 및 인터넷"})
check("정확히 일치하면 200", r.status_code == 200, r.text)
tap_calls = [x for x in calls if "input" in x]
check("가운데 좌표로 탭이 나갑니다",
      tap_calls == [("AGENT_A", "shell", "input", "tap", "540", "260")], str(tap_calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "네트워크"})
check("부분 일치도 누릅니다", r.status_code == 200, r.text)

calls.clear()
r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "네트워크", "exact": True})
check("exact면 부분 일치는 404", r.status_code == 404, r.text)
check("못 찾으면 adb를 부르지 않습니다",
      [x for x in calls if "input" in x] == [], str(calls))

r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "save as"})
check("대소문자를 가리지 않습니다", r.status_code == 200, r.text)

r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "없는 글자"})
check("없으면 404", r.status_code == 404, r.text)
check("404 메시지가 무엇을 못 찾았는지 말합니다",
      "없는 글자" in r.json()["message"], r.text)

r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "설정", "index": 5})
check("index가 범위를 넘으면 400", r.status_code == 400, r.text)

print()
print("=== tap_text와 점유 ===")
c.post("/api/device/AGENT_A/occupy", json={"owner": "ci-a", "ttl_seconds": 60})
calls.clear()
r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "설정", "owner": "intruder"})
check("남이 점유한 기기는 409", r.status_code == 409, r.text)
check("409면 기기를 건드리지 않습니다",
      [x for x in calls if "input" in x] == [], str(calls))
r = c.post("/api/agent/AGENT_A/tap-text", json={"text": "설정", "owner": "ci-a"})
check("점유자 본인은 200", r.status_code == 200, r.text)
c.post("/api/device/AGENT_A/release", json={"owner": "ci-a"})

print()
print("=== type_text: 기기 셸에서 다시 쪼개지지 않는가 ===")
calls.clear()
r = c.post("/api/agent/AGENT_A/type-text", json={"text": "hello world"})
check("공백이 있는 문자열도 200", r.status_code == 200, r.text)
check("공백은 %s로, 전체는 인용부호로",
      calls == [("AGENT_A", "shell", "input", "text", "'hello%sworld'")], str(calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/type-text", json={"text": "a'b"})
check("작은따옴표가 있어도 200", r.status_code == 200, r.text)
check("작은따옴표는 이스케이프됩니다",
      calls == [("AGENT_A", "shell", "input", "text", "'a'\\''b'")], str(calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/type-text", json={"text": "x; reboot"})
check("세미콜론이 있어도 200", r.status_code == 200, r.text)
check("명령 분리자는 인용 안에 갇힙니다",
      calls == [("AGENT_A", "shell", "input", "text", "'x;%sreboot'")], str(calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/type-text", json={"text": "$(reboot)"})
check("명령 치환도 인용 안에 갇힙니다",
      calls == [("AGENT_A", "shell", "input", "text", "'$(reboot)'")], str(calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/type-text", json={"text": "한글"})
check("비ASCII는 400", r.status_code == 400, r.text)
check("400 메시지가 IME를 안내합니다", "IME" in r.json()["message"], r.text)
check("400이면 adb에 닿지 않습니다", calls == [], str(calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/type-text", json={"text": "a\nb"})
check("제어문자는 400", r.status_code == 400, r.text)
check("제어문자도 adb에 닿지 않습니다", calls == [], str(calls))

print()
print("=== open_app ===")
calls.clear()
r = c.post("/api/agent/AGENT_A/open-app", json={"package": "com.android.settings"})
check("패키지로 실행하면 200", r.status_code == 200, r.text)
check("monkey로 런처를 띄웁니다",
      calls == [("AGENT_A", "shell", "monkey", "-p", "com.android.settings",
                 "-c", "android.intent.category.LAUNCHER", "1")], str(calls))

calls.clear()
r = c.post("/api/agent/AGENT_A/open-app", json={"package": "not a package"})
check("이상한 패키지명은 400", r.status_code == 400, r.text)
check("400이면 실행하지 않습니다", calls == [], str(calls))

r = c.post("/api/agent/AGENT_A/open-app", json={"name": "Notes"})
check("안드로이드에 name만 주면 400", r.status_code == 400, r.text)
check("400 메시지가 패키지명을 요구합니다",
      "패키지" in r.json()["message"], r.text)

print()
print("=== wait_stable ===")
# 스크린샷을 시나리오대로 돌려주는 가짜. 실제 이미지 비교를 그대로 태웁니다.
def png(color):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 80), color).save(buf, format="PNG")
    return buf.getvalue()

RED, BLUE = png((200, 20, 20)), png((20, 20, 200))

frames = []
def fake_capture_bytes(serial, full=False, size=None):
    return (frames.pop(0) if frames else RED), "image/png"
server.capture_screenshot_bytes = fake_capture_bytes

frames = [RED, RED, RED]
r = c.post("/api/agent/AGENT_A/wait-stable", json={"timeout": 3, "interval": 0.05})
check("이미 멈춰 있으면 stable=true", r.json().get("stable") is True, r.text)

frames = [RED, BLUE, RED, BLUE, RED, BLUE, RED, BLUE, RED, BLUE,
          RED, BLUE, RED, BLUE, RED, BLUE, RED, BLUE, RED, BLUE]
r = c.post("/api/agent/AGENT_A/wait-stable", json={"timeout": 0.3, "interval": 0.05})
check("계속 바뀌면 200 + stable=false", r.status_code == 200, r.text)
check("타임아웃은 오류가 아닙니다", r.json().get("stable") is False, r.text)
check("얼마나 기다렸는지 알려줍니다", "waited" in r.json(), r.text)

frames = [RED, BLUE, BLUE, BLUE]
r = c.post("/api/agent/AGENT_A/wait-stable", json={"timeout": 3, "interval": 0.05})
check("두 프레임이 같아지면 stable=true", r.json().get("stable") is True, r.text)

check("같은 그림은 비슷하다고 봅니다", server.images_similar(RED, RED, 0.02))
check("다른 그림은 아니라고 봅니다", not server.images_similar(RED, BLUE, 0.02))

print()
print("=== screenshot: 에이전트는 원본 해상도가 필요합니다 ===")
r = c.get("/api/device/AGENT_A/screenshot?full=1")
check("full=1은 PNG", r.headers.get("content-type") == "image/png", r.headers.get("content-type"))

def thumb_capture(serial, full=False, size=None):
    return (b"jpegbytes", "image/jpeg") if not full else (RED, "image/png")
server.capture_screenshot_bytes = thumb_capture
r = c.get("/api/device/AGENT_A/screenshot")
check("기본은 여전히 JPEG 썸네일",
      r.headers.get("content-type") == "image/jpeg", r.headers.get("content-type"))

print()
print("=== 요소 검색 규칙 ===")
sample = [
    {"text": "확인", "content_desc": "", "center": {"x": 1, "y": 1}},
    {"text": "확인하려면 여기를 누르세요", "content_desc": "", "center": {"x": 2, "y": 2}},
]
ranked = server.find_elements(sample, "확인", exact=False)
check("정확히 맞는 것이 부분 일치보다 먼저",
      ranked[0]["text"] == "확인", str([e["text"] for e in ranked]))
check("빈 문자열은 아무것도 찾지 않습니다",
      server.find_elements(sample, "  ") == [], "빈 검색어가 매치됐습니다")

print()
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
