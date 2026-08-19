"""배포한 빌드를 원격에서 멈추는 스위치.

배포본을 남에게 주고 나면 회수할 방법이 없습니다. 이 모듈은 그 구멍을 메웁니다 —
배포자가 관리하는 파일 하나를 실행할 때마다 읽어서, 거기서 막으라고 하면 서버가
뜨지 않습니다.

## 무엇을 할 수 있고 무엇을 못 하나

**분명히 해 둘 것: 이건 자물쇠가 아니라 문패입니다.** 남의 PC에서 도는 코드라
마음먹으면 우회됩니다 — 번들을 풀어 이 파일을 들어내거나, hosts로 주소를 막거나,
`DEVICE_FARM_CONTROL_URL`을 자기 서버로 돌리면 그만입니다. 이걸 DRM으로 쓰면 안
됩니다. 목적은 "무분별하게 쓰는 사람에게 그만 쓰라고 말하고 실제로 멈추게 하는
것"이지, 적대적인 사용자를 막는 게 아닙니다.

## 오프라인은 어떻게 하나

QA 장비는 폐쇄망에 있는 일이 흔합니다. 네트워크가 안 되면 못 쓰게 만들면 정작
정상 사용자만 곤란해집니다. 그래서 **마지막으로 확인한 결과를 캐시하고, 유예
기간(기본 14일) 동안은 확인 없이도 돕니다.** 유예가 지나면 한 번은 확인에
성공해야 합니다. 이미 "차단"을 받아 둔 상태라면 오프라인이어도 계속 차단입니다 —
그래야 네트워크를 끊는 것이 우회 수단이 되지 않습니다.

## 사용자에게 숨기지 않습니다

실행할 때 어디를 확인하는지, 이 설치본의 ID가 무엇인지 콘솔에 찍습니다. 배포본
안내문에도 적습니다. 몰래 통신하는 프로그램을 QA 팀에 돌리게 할 수는 없습니다.
"""

import json
import os
import time
import urllib.error
import urllib.request
import uuid

# 배포자가 고쳐 쓰는 파일. GitHub raw면 웹에서 바로 편집할 수 있어 서버가 따로
# 필요 없습니다. 빌드할 때 DEVICE_FARM_CONTROL_URL로 덮어쓸 수 있습니다.
DEFAULT_CONTROL_URL = (
    "https://raw.githubusercontent.com/kimyeongseong/qa-device-farm/main/dist-control.json"
)

# 확인에 실패해도 이 기간까지는 캐시로 버팁니다.
GRACE_SECONDS = 14 * 24 * 3600

# 오래 켜 두는 팜이라, 기동할 때 한 번 보고 끝내면 차단이 며칠 뒤에나 먹습니다.
RECHECK_SECONDS = 6 * 3600

FETCH_TIMEOUT = 6.0


def control_url() -> str:
    return os.environ.get("DEVICE_FARM_CONTROL_URL", "").strip() or DEFAULT_CONTROL_URL


def install_id(state_dir: str) -> str:
    """이 설치본을 가리키는 ID.

    개인 정보가 아니라 그냥 난수입니다. 배포자가 "누구를 막을지" 고를 수 있어야
    해서 있는 것이고, 사용자에게도 콘솔에 보여줍니다.
    """
    path = os.path.join(state_dir, "install-id.txt")
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing
    except OSError:
        pass

    fresh = uuid.uuid4().hex[:12]
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fresh)
    except OSError:
        pass  # 못 써도 동작은 해야 합니다. 다음 실행에 새 ID가 나올 뿐입니다.
    return fresh


def _cache_path(state_dir: str) -> str:
    return os.path.join(state_dir, "dist-control-cache.json")


def read_cache(state_dir: str):
    try:
        with open(_cache_path(state_dir), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "checked_at" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def write_cache(state_dir: str, payload: dict):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(_cache_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def fetch_control(url: str, timeout: float = FETCH_TIMEOUT):
    """관리 파일을 읽어 옵니다. 못 읽으면 (None, 사유)."""
    try:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(64 * 1024).decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None, "관리 파일 형식이 올바르지 않습니다"
        return parsed, None
    except urllib.error.HTTPError as e:
        # 파일이 아직 없는 것(404)은 "막지 않음"입니다. 없다고 전부 멈추면
        # 배포자가 파일을 올리기 전까지 아무도 못 씁니다.
        if e.code == 404:
            return {}, None
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def verdict(control: dict, my_id: str):
    """관리 파일 내용에서 이 설치본이 돌아도 되는지 판단합니다."""
    if not control:
        return True, None

    if control.get("allowed") is False:
        return False, control.get("message") or "배포자가 이 프로그램의 사용을 중지했습니다."

    blocked = control.get("blocked_ids") or []
    if my_id in blocked:
        return False, (control.get("blocked_message")
                       or f"이 설치본({my_id})은 배포자가 사용을 중지했습니다.")

    return True, None


def evaluate(state_dir: str, url: str = None, now: float = None):
    """지금 실행해도 되는지 결정합니다.

    돌려주는 것: {allowed, message, source, install_id, checked_at}
    source는 왜 그렇게 판단했는지입니다 — 'network' / 'cache' / 'grace' / 'stale'.
    """
    url = url or control_url()
    now = time.time() if now is None else now
    my_id = install_id(state_dir)

    control, error = fetch_control(url)
    if error is None:
        allowed, message = verdict(control, my_id)
        write_cache(state_dir, {"checked_at": now, "allowed": allowed,
                                "message": message, "id": my_id})
        return {"allowed": allowed, "message": message, "source": "network",
                "install_id": my_id, "checked_at": now}

    cached = read_cache(state_dir)
    if cached is None:
        # 확인한 적이 한 번도 없습니다. 폐쇄망에 처음 설치한 경우가 여기라서,
        # 막지 않고 유예를 시작합니다.
        write_cache(state_dir, {"checked_at": now, "allowed": True,
                                "message": None, "id": my_id, "unverified": True})
        return {"allowed": True, "message": None, "source": "grace",
                "install_id": my_id, "checked_at": now}

    # 이미 차단으로 확인된 뒤라면 네트워크를 끊어도 계속 차단입니다.
    if cached.get("allowed") is False:
        return {"allowed": False, "message": cached.get("message"), "source": "cache",
                "install_id": my_id, "checked_at": cached.get("checked_at")}

    age = now - float(cached.get("checked_at") or 0)
    if age <= GRACE_SECONDS:
        return {"allowed": True, "message": None, "source": "cache",
                "install_id": my_id, "checked_at": cached.get("checked_at")}

    days = int(GRACE_SECONDS / 86400)
    return {"allowed": False, "source": "stale", "install_id": my_id,
            "checked_at": cached.get("checked_at"),
            "message": (f"{days}일 넘게 사용 확인을 하지 못했습니다 "
                        f"({error}). 네트워크에 한 번 연결한 뒤 다시 실행하세요.")}
