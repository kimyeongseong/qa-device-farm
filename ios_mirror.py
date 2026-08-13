"""iOS 미러링 프로바이더 — phone-harness 어댑터.

이 팜은 안드로이드용으로 만들어졌습니다. adb가 있으니 기기를 목록으로 세고,
입력을 넣고, 화면을 뜨는 일이 전부 한 프로토콜 안에서 끝납니다. 아이폰에는
그런 게 없습니다. 대신 macOS Sequoia의 '아이폰 미러링' 창이 있고,
phone-harness(https://github.com/ShawnPana/phone-harness)가 그 창을 Quartz로
클릭하고 Vision으로 읽습니다.

그래서 여기서 하는 일은 하나입니다. **미러링 중인 아이폰 한 대를 팜의 기기
하나처럼 보이게 만드는 것.** 시리얼은 `ios-mirror`이고, 점유(lease)·입력·
스크린샷·AI verb가 안드로이드와 같은 경로를 탑니다.

## 왜 라이브러리 import가 아니라 subprocess인가

phone-harness는 설치 가이드가 CLI(`phone-harness`)를 보장합니다. 파이썬 API
표면은 버전에 따라 달라질 수 있고, 무엇보다 그쪽은 맥 GUI에 붙는 pyobjc
라이브러리라서 이 서버 프로세스 안으로 끌고 들어오면 임포트 실패 하나가 팜
전체를 못 뜨게 만듭니다. 프로세스 경계를 두면 아이폰 쪽이 어떻게 부서지든
안드로이드 팜은 계속 돕니다.

## 스크립트에 사용자 데이터를 넣지 않는 이유

phone-harness는 stdin으로 파이썬 스크립트를 받습니다. 탭 좌표나 입력할 문자열을
그 스크립트 문자열에 끼워 넣으면, 서버가 받은 값이 그대로 파이썬 코드가 됩니다
— 팜이 네트워크에 열려 있고 그 값을 종종 LLM이 만든다는 걸 생각하면 답이
없습니다. 그래서 **스크립트 템플릿은 상수**이고, 값은 전부 환경변수(`PH_*`)로
넘깁니다. 서버 쪽에서 adb를 argv로만 부르는 것과 같은 규칙을 한 축 더 적용한
셈입니다.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

# 팜 안에서 아이폰이 갖는 시리얼. 안드로이드 시리얼과 겹칠 수 없는 형태입니다.
IOS_SERIAL = "ios-mirror"

# 한 번의 verb가 이보다 오래 걸리면 미러링 창이 사라졌거나 맥이 잠긴 겁니다.
CALL_TIMEOUT = 30.0

# ios_status()는 요청마다 불립니다(기기 목록 폴링이 2초 주기). 파일시스템을
# 매번 뒤질 이유가 없어서 짧게 캐시합니다.
STATUS_TTL = 10.0
_status_cache = {"at": 0.0, "value": None}


def is_ios(serial: str) -> bool:
    return serial == IOS_SERIAL


def find_harness():
    """phone-harness 실행 파일 경로, 없으면 None.

    설치 가이드는 `pip install -e .`을 권하니 보통 PATH에 잡힙니다. 그런데 팜
    서버를 launchd나 다른 셸에서 띄우면 그 PATH가 아닐 수 있어서, 가이드가
    권장하는 설치 위치(`~/.phone-harness`)도 같이 봅니다.
    """
    override = os.environ.get("PHONE_HARNESS_BIN", "").strip()
    if override:
        return override if os.path.isfile(override) and os.access(override, os.X_OK) else None

    found = shutil.which("phone-harness")
    if found:
        return found

    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".phone-harness", ".venv", "bin", "phone-harness"),
        os.path.join(home, ".phone-harness", "bin", "phone-harness"),
        os.path.join(home, ".local", "bin", "phone-harness"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ios_status(refresh: bool = False) -> dict:
    """iOS를 쓸 수 있는 상태인지, 아니면 왜 못 쓰는지.

    사유를 한국어 문장으로 돌려주는 게 요점입니다. 대시보드에 아이폰 칸이 아예
    없으면 "지원 안 하나 보다"로 끝나는데, 실제로는 맥이 아니거나 설치가 안 된
    것뿐인 경우가 대부분입니다.
    """
    now = time.time()
    if not refresh and _status_cache["value"] and (now - _status_cache["at"]) < STATUS_TTL:
        return _status_cache["value"]

    if sys.platform != "darwin":
        value = {"available": False, "binary": None,
                 "reason": "iOS 미러링은 macOS 호스트에서만 동작합니다 "
                           "(현재 호스트: %s). 안드로이드 기능은 영향받지 않습니다." % sys.platform}
    else:
        binary = find_harness()
        if not binary:
            value = {"available": False, "binary": None,
                     "reason": "phone-harness를 찾지 못했습니다. "
                               "~/.phone-harness에 설치하거나 PATH에 등록한 뒤 "
                               "PHONE_HARNESS_BIN으로 경로를 지정하세요."}
        else:
            value = {"available": True, "binary": binary, "reason": None}

    _status_cache.update({"at": now, "value": value})
    return value


def available() -> bool:
    return ios_status()["available"]


# --- 좌표계 ---
# phone-harness는 **맥 화면 좌표(screen points)**로 말합니다. `tap(x, y)`도,
# `ocr()`이 돌려주는 중심점도 전부 그쪽입니다. 그런데 팜의 조작 API는 안드로이드
# 기준으로 **스크린샷 이미지의 픽셀**입니다 — 대시보드도 에이전트도 화면을 보고
# 그 위의 좌표를 보냅니다.
#
# 두 좌표계가 API 밖으로 새어 나가면 아이폰만 다른 규칙이 됩니다. 그래서 경계인
# 여기서 변환합니다: 팜은 어느 플랫폼이든 이미지 픽셀로 말하고, 아이폰 쪽 변환은
# harness 스크립트 안에서 `screen_info()`로 그때그때 계산합니다(창을 옮기거나
# 크기를 바꿔도 맞도록).

# --- phone-harness 스크립트 템플릿 (상수) ---
# 값은 환경변수로만 들어옵니다. 결과는 `__FARM__` 접두사가 붙은 JSON 한 줄로
# 찍습니다 — harness나 pyobjc가 무엇을 더 출력하든 그 줄만 골라 읽으면 되도록.

_PRELUDE = """
import json, os
def _emit(payload):
    print("__FARM__" + json.dumps(payload, ensure_ascii=False))

def _scale():
    # (창 원점, 이미지픽셀 -> 화면포인트 배율). screen_info()는 캡처를 한 번
    # 하므로 창을 옮겨도 맞습니다.
    info = screen_info()
    win, (iw, ih) = info["window"], info["img_px"]
    return win, win["w"] / iw, win["h"] / ih

def _to_points(x, y, win, sx, sy):
    return win["x"] + float(x) * sx, win["y"] + float(y) * sy
"""

SCRIPTS = {
    "tap": _PRELUDE + """
win, sx, sy = _scale()
gx, gy = _to_points(os.environ["PH_X"], os.environ["PH_Y"], win, sx, sy)
tap(gx, gy)
_emit({"ok": True})
""",
    # 좌표 스와이프는 helpers.swipe가 아니라 drag입니다 — swipe()는 방향
    # 문자열('up'/'down')을 받는 다른 함수입니다.
    "swipe": _PRELUDE + """
win, sx, sy = _scale()
x1, y1 = _to_points(os.environ["PH_X1"], os.environ["PH_Y1"], win, sx, sy)
x2, y2 = _to_points(os.environ["PH_X2"], os.environ["PH_Y2"], win, sx, sy)
drag(x1, y1, x2, y2, duration=max(0.05, float(os.environ["PH_DURATION"]) / 1000.0))
_emit({"ok": True})
""",
    "type_text": _PRELUDE + """
type_text(os.environ["PH_TEXT"])
_emit({"ok": True})
""",
    "open_app": _PRELUDE + """
open_app(os.environ["PH_NAME"])
_emit({"ok": True})
""",
    # ocr()은 화면 포인트 기준 중심점을 돌려주므로, 팜이 쓰는 이미지 픽셀로
    # 되돌려서 내보냅니다.
    "ocr": _PRELUDE + """
win, sx, sy = _scale()
items = []
for o in ocr():
    items.append({
        "text": o["text"],
        "confidence": o.get("confidence"),
        "cx": (o["x"] - win["x"]) / sx,
        "cy": (o["y"] - win["y"]) / sy,
        "w": o["w"] / sx,
        "h": o["h"] / sy,
    })
_emit({"ok": True, "items": items, "img": [win["w"] / sx, win["h"] / sy]})
""",
    "screenshot": _PRELUDE + """
screenshot(os.environ["PH_OUT"])
_emit({"ok": True, "path": os.environ["PH_OUT"]})
""",
    "wait_stable": _PRELUDE + """
_emit({"ok": True, "stable": bool(wait_stable(timeout=float(os.environ["PH_TIMEOUT"])))})
""",
    # 미러링이 끊겼는지, 왜 끊겼는지. phone-harness는 연결 화면을 대신 눌러주지
    # 않습니다(사용자가 직접 해야 하는 물리 동작이라고 못박아 뒀습니다). 그
    # 판단을 팜도 그대로 존중하고, 사용자에게 이유만 전달합니다.
    "state": _PRELUDE + """
_emit({"ok": True, "state": connection_state()})
""",
}

# connection_state()가 돌려주는 값 -> 사람이 읽을 한국어 사유.
STATE_HINTS = {
    "ready": None,
    "not-running": "맥에서 '아이폰 미러링' 앱이 실행 중이 아닙니다.",
    "no-window": "아이폰 미러링은 열려 있지만 아이폰이 연결되지 않았습니다.",
    "blocked": ("아이폰 미러링이 연결 대기 화면입니다. 맥에서 직접 연결하세요 "
                "('사용 중'이라고 나오면 아이폰을 잠그면 미러링이 재개됩니다)."),
}


class PhoneHarnessAdapter:
    """phone-harness CLI 앞에 씌운 얇은 껍데기.

    메서드는 서버가 안드로이드에 쓰는 동사와 1:1입니다. 실패하면 harness의
    stderr를 그대로 담은 RuntimeError를 올립니다 — 이 층에서 해석해봐야 원인이
    맥 쪽에 있는데 메시지만 뭉개집니다.
    """

    def __init__(self):
        # 미러링 창은 하나뿐이고 GUI 조작이라 겹치면 서로를 밟습니다.
        self._lock = asyncio.Lock()
        self._size = None

    async def _run(self, verb: str, env_extra: dict = None, timeout: float = CALL_TIMEOUT):
        status = ios_status()
        if not status["available"]:
            raise RuntimeError(status["reason"])

        env = dict(os.environ)
        env.update({k: str(v) for k, v in (env_extra or {}).items()})

        async with self._lock:
            proc = await asyncio.create_subprocess_exec(
                status["binary"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(SCRIPTS[verb].encode("utf-8")), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(
                    f"phone-harness {verb}가 {timeout:.0f}초 안에 끝나지 않았습니다 "
                    f"(아이폰 미러링 창이 열려 있고 맥이 잠금 해제 상태인지 확인하세요)")

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"phone-harness가 종료 코드 {proc.returncode}로 끝났습니다")

        for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
            if line.startswith("__FARM__"):
                return json.loads(line[len("__FARM__"):])
        raise RuntimeError("phone-harness 응답을 해석하지 못했습니다: "
                           + stdout.decode("utf-8", errors="replace")[-200:])

    async def tap(self, x: int, y: int):
        """스크린샷 이미지 픽셀 기준 좌표. 화면 포인트 변환은 스크립트가 합니다."""
        await self._run("tap", {"PH_X": int(x), "PH_Y": int(y)})

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300):
        await self._run("swipe", {"PH_X1": int(x1), "PH_Y1": int(y1),
                                  "PH_X2": int(x2), "PH_Y2": int(y2),
                                  "PH_DURATION": int(duration)})

    async def type_text(self, text: str):
        """맥 키보드 이벤트로 입력합니다.

        안드로이드와 마찬가지로 한글·이모지는 들어가지 않습니다. phone-harness가
        US 배열 키코드로 한 글자씩 치기 때문에 매핑 없는 문자는 거기서 거절됩니다.
        기기가 아니라 맥 쪽 제약이라는 것만 다릅니다.
        """
        if not str(text).isascii():
            raise ValueError(
                "iOS 입력도 ASCII만 됩니다 (phone-harness가 US 배열 키코드로 칩니다)")
        await self._run("type_text", {"PH_TEXT": str(text)})

    async def open_app(self, name: str):
        if not str(name).strip():
            raise ValueError("앱 이름이 비어 있습니다")
        await self._run("open_app", {"PH_NAME": str(name)})

    async def wait_stable(self, timeout: float = 10.0):
        result = await self._run("wait_stable", {"PH_TIMEOUT": float(timeout)},
                                 timeout=timeout + CALL_TIMEOUT)
        return bool(result.get("stable"))

    async def connection_state(self):
        """'ready' / 'blocked' / 'no-window' / 'not-running'."""
        result = await self._run("state")
        return result.get("state")

    async def ocr(self):
        """Vision OCR 결과를 팜의 element 형식으로 맞춰 돌려줍니다.

        phone-harness는 `{text, confidence, x, y, w, h}`를 주는데 (x, y)가 이미
        **중심점**입니다. 팜의 나머지(안드로이드 uiautomator)는 x1/y1/x2/y2 +
        center를 씁니다. 두 형식이 API 밖으로 새어 나가면 에이전트가 플랫폼마다
        다른 코드를 짜야 하므로 경계인 여기서 맞춥니다. 좌표는 스크립트가 이미
        이미지 픽셀로 되돌려 보냈습니다.
        """
        result = await self._run("ocr")
        img = result.get("img")
        if img and len(img) == 2:
            self._size = (int(round(img[0])), int(round(img[1])))

        elements = []
        for item in result.get("items", []):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            cx, cy = int(round(item["cx"])), int(round(item["cy"]))
            half_w, half_h = int(round(item["w"] / 2)), int(round(item["h"] / 2))
            elements.append({
                "text": text,
                "content_desc": "",
                "resource_id": "",
                "class": "ocr",
                # OCR로 본 글자는 눌러보는 것 말고 할 게 없습니다. 뷰 트리가
                # 아니라서 정말 눌리는지는 알 수 없고, 모른다고 답하느니
                # 에이전트가 시도할 수 있게 둡니다.
                "clickable": True,
                "enabled": True,
                "confidence": item.get("confidence"),
                "bounds": {"x1": cx - half_w, "y1": cy - half_h,
                           "x2": cx + half_w, "y2": cy + half_h},
                "center": {"x": cx, "y": cy},
            })
        return elements

    async def screenshot(self, full: bool = True):
        """미러링 창 캡처. full=False면 비교용으로 줄여서 돌려줍니다."""
        fd, path = tempfile.mkstemp(prefix="farm-ios-", suffix=".png")
        os.close(fd)
        try:
            await self._run("screenshot", {"PH_OUT": path})
            with open(path, "rb") as f:
                data = f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not data:
            raise RuntimeError("phone-harness가 빈 스크린샷을 돌려줬습니다")

        self._size = _png_size(data) or self._size
        if full:
            return data
        return _shrink(data)

    async def screen_size(self):
        """(width, height). 한 번 캡처해 본 값을 재사용합니다."""
        if self._size:
            return self._size
        try:
            await self.screenshot(full=True)
        except Exception:
            return None
        return self._size


def _png_size(data: bytes):
    """PNG 헤더에서 크기만 읽습니다 (Pillow를 부르지 않아도 되는 경로)."""
    import struct
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    except struct.error:
        return None


def _shrink(data: bytes, size=(200, 400)):
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(data))
    img.thumbnail(size)
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=60)
    return buf.getvalue()


# 서버는 이 인스턴스를 씁니다. 테스트는 `server.ios.adapter`를 가짜로 갈아끼우는데,
# 이는 기존 테스트가 `server.adb_exec`를 갈아끼우는 방식과 같습니다.
adapter = PhoneHarnessAdapter()


# --- 연결 상태 캐시 ---
# connection_state()는 캡처 + OCR입니다. 대시보드가 2초마다 부르는 기기 목록에
# 그걸 달면 맥이 계속 OCR을 돌게 됩니다. 그래서 목록은 **마지막으로 알던 값**만
# 읽고, 실제 확인은 /api/health에서 TTL을 두고 합니다.

STATE_TTL = 30.0
_state_cache = {"at": 0.0, "value": None}


def cached_state():
    """최근에 확인한 연결 상태. 오래됐거나 확인한 적 없으면 None."""
    if _state_cache["value"] and (time.time() - _state_cache["at"]) < STATE_TTL:
        return _state_cache["value"]
    return None


def note_state(value):
    if value:
        _state_cache.update({"at": time.time(), "value": value})


async def refresh_state(force: bool = False):
    """연결 상태를 확인하고 캐시합니다. TTL 안이면 확인하지 않습니다."""
    if not available():
        return None
    if not force and cached_state():
        return cached_state()
    try:
        value = await adapter.connection_state()
    except Exception as e:
        # 상태를 못 물어보는 것도 상태입니다. 미러링 앱이 죽었을 때가 대개 이쪽.
        print(f"[{IOS_SERIAL}] connection_state failed: {e}")
        value = "not-running"
    note_state(value)
    return value


def device_entry(lease=None, alias=None, state=None):
    """`/api/devices`에 실릴 아이폰 한 줄.

    안드로이드 항목과 키를 맞춰 둡니다 — 대시보드가 같은 렌더 경로를 쓰고,
    platform 필드만 보고 다른 버튼을 답니다.

    `state`는 phone-harness의 connection_state()입니다. 'ready'가 아니면
    안드로이드의 unauthorized·offline과 같은 자리에 이유를 적습니다 — 대시보드에
    기기가 있는데 아무것도 안 되는 것보다, 왜 안 되는지 보이는 게 낫습니다.
    """
    size = adapter._size or (0, 0)
    hint = STATE_HINTS.get(state) if state else None
    return {
        "serial": IOS_SERIAL,
        "platform": "ios",
        "model": "iPhone (미러링)",
        "version": "iOS",
        "width": size[0],
        "height": size[1],
        "ip": "-",
        "sdk": "-",
        "battery": "-",
        "alias": alias or "iPhone (미러링)",
        "state": "device" if hint is None else "disconnected",
        "state_hint": hint,
        "occupied_by": lease["owner"] if lease else None,
        "occupied_until": lease["expires_at"] if lease else None,
    }
