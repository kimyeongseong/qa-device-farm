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


# --- phone-harness 스크립트 템플릿 (상수) ---
# 값은 환경변수로만 들어옵니다. 결과는 마지막 줄에 JSON 한 줄로 찍습니다 —
# harness 자체가 뭘 출력하든 마지막 줄만 읽으면 되도록.

_PRELUDE = """
import json, os
def _emit(payload):
    print("__FARM__" + json.dumps(payload, ensure_ascii=False))
"""

SCRIPTS = {
    "tap": _PRELUDE + """
tap(int(os.environ["PH_X"]), int(os.environ["PH_Y"]))
_emit({"ok": True})
""",
    "swipe": _PRELUDE + """
swipe(int(os.environ["PH_X1"]), int(os.environ["PH_Y1"]),
      int(os.environ["PH_X2"]), int(os.environ["PH_Y2"]),
      duration=float(os.environ["PH_DURATION"]) / 1000.0)
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
    "ocr": _PRELUDE + """
_emit({"ok": True, "items": ocr()})
""",
    "screenshot": _PRELUDE + """
_img = screenshot(os.environ["PH_OUT"])
_emit({"ok": True, "path": os.environ["PH_OUT"]})
""",
    "wait_stable": _PRELUDE + """
wait_stable(timeout=float(os.environ["PH_TIMEOUT"]))
_emit({"ok": True})
""",
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
        await self._run("tap", {"PH_X": int(x), "PH_Y": int(y)})

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300):
        await self._run("swipe", {"PH_X1": int(x1), "PH_Y1": int(y1),
                                  "PH_X2": int(x2), "PH_Y2": int(y2),
                                  "PH_DURATION": int(duration)})

    async def type_text(self, text: str):
        # 안드로이드와 달리 ASCII 제한이 없습니다. 맥이 키 이벤트를 만들어
        # 넣기 때문에 한글도 그대로 들어갑니다.
        await self._run("type_text", {"PH_TEXT": str(text)})

    async def open_app(self, name: str):
        if not str(name).strip():
            raise ValueError("앱 이름이 비어 있습니다")
        await self._run("open_app", {"PH_NAME": str(name)})

    async def wait_stable(self, timeout: float = 10.0):
        await self._run("wait_stable", {"PH_TIMEOUT": float(timeout)},
                        timeout=timeout + CALL_TIMEOUT)

    async def ocr(self):
        """Vision OCR 결과를 서버의 element 형식으로 맞춰 돌려줍니다.

        phone-harness는 {text, bounds:[x,y,w,h]} 계열을 돌려주는데, 팜의
        나머지(안드로이드 uiautomator)는 x1/y1/x2/y2 + center를 씁니다. 두 형식이
        API 밖으로 새어 나가면 에이전트가 플랫폼별로 다른 코드를 짜야 해서,
        경계인 여기서 맞춥니다.
        """
        result = await self._run("ocr")
        elements = []
        for item in result.get("items", []):
            rect = _normalize_bounds(item)
            if not rect:
                continue
            elements.append({
                "text": str(item.get("text", "")).strip(),
                "content_desc": "",
                "resource_id": "",
                "class": "ocr",
                "clickable": True,   # OCR로 본 글자는 눌러보는 것 말고 할 게 없습니다.
                "enabled": True,
                **rect,
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


def _normalize_bounds(item: dict):
    """phone-harness의 bounds가 어떤 모양으로 오든 x1/y1/x2/y2로 맞춥니다."""
    raw = item.get("bounds") or item.get("rect") or item.get("box")
    if isinstance(raw, dict):
        if all(k in raw for k in ("x1", "y1", "x2", "y2")):
            x1, y1, x2, y2 = raw["x1"], raw["y1"], raw["x2"], raw["y2"]
        elif all(k in raw for k in ("x", "y", "width", "height")):
            x1, y1 = raw["x"], raw["y"]
            x2, y2 = x1 + raw["width"], y1 + raw["height"]
        else:
            return None
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        # [x, y, w, h] — Vision이 돌려주는 관례.
        x, y, w, h = raw
        x1, y1, x2, y2 = x, y, x + w, y + h
    else:
        return None

    x1, y1, x2, y2 = (int(round(v)) for v in (x1, y1, x2, y2))
    return {"bounds": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            "center": {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2}}


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


def device_entry(lease=None, alias=None):
    """`/api/devices`에 실릴 아이폰 한 줄.

    안드로이드 항목과 키를 맞춰 둡니다 — 대시보드가 같은 렌더 경로를 쓰고,
    platform 필드만 보고 다른 버튼을 답니다.
    """
    size = adapter._size or (0, 0)
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
        "state": "device",
        "state_hint": None,
        "occupied_by": lease["owner"] if lease else None,
        "occupied_until": lease["expires_at"] if lease else None,
    }
