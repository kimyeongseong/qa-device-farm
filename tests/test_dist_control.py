"""배포본 원격 차단 스위치.

이 코드가 틀리면 두 방향 모두 나쁩니다. 느슨하면 막았는데 계속 쓰이고, 빡빡하면
멀쩡한 사용자가 네트워크 한 번 끊겼다고 일을 못 합니다. 그래서 판단 규칙과
오프라인 유예를 여기서 못 박습니다.

네트워크는 타지 않습니다 -- fetch를 갈아끼워서 "관리 파일이 이렇게 왔을 때"만
확인합니다.
"""
import os, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dist_control

fails = []
def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

def fresh_dir():
    return tempfile.mkdtemp(prefix="farm_ctl_")

def fake_fetch(payload, error=None):
    def _fetch(url, timeout=None):
        return (payload, error)
    return _fetch

REAL_FETCH = dist_control.fetch_control

print("=== 설치 ID ===")
d = fresh_dir()
first = dist_control.install_id(d)
check("ID가 만들어집니다", bool(first), first)
check("다시 불러도 같은 ID", dist_control.install_id(d) == first, first)
check("설치가 다르면 다른 ID", dist_control.install_id(fresh_dir()) != first)
check("ID는 파일로 남습니다", os.path.exists(os.path.join(d, "install-id.txt")))

print()
print("=== 판단 규칙 ===")
check("관리 파일이 비어 있으면 허용", dist_control.verdict({}, "abc")[0] is True)
check("allowed=true면 허용", dist_control.verdict({"allowed": True}, "abc")[0] is True)

allowed, message = dist_control.verdict({"allowed": False, "message": "사용 중지"}, "abc")
check("allowed=false면 차단", allowed is False)
check("차단 사유를 전달합니다", message == "사용 중지", message)

allowed, message = dist_control.verdict({"allowed": False}, "abc")
check("사유가 없어도 안내 문구는 나옵니다", bool(message), message)

allowed, _ = dist_control.verdict({"blocked_ids": ["abc", "def"]}, "abc")
check("차단 목록에 있으면 차단", allowed is False)
allowed, _ = dist_control.verdict({"blocked_ids": ["def"]}, "abc")
check("목록에 없으면 허용", allowed is True)
allowed, message = dist_control.verdict({"blocked_ids": ["abc"]}, "abc")
check("개별 차단 문구에 ID가 들어갑니다", "abc" in message, message)

print()
print("=== 확인에 성공했을 때 ===")
d = fresh_dir()
dist_control.fetch_control = fake_fetch({"allowed": True})
state = dist_control.evaluate(d)
check("허용이면 실행 가능", state["allowed"] is True, str(state))
check("네트워크로 판단했다고 표시", state["source"] == "network", str(state))
check("결과를 캐시에 남깁니다", dist_control.read_cache(d) is not None)

d = fresh_dir()
dist_control.fetch_control = fake_fetch({"allowed": False, "message": "그만"})
state = dist_control.evaluate(d)
check("차단이면 실행 불가", state["allowed"] is False, str(state))
check("사유를 그대로 전달", state["message"] == "그만", str(state))

print()
print("=== 관리 파일이 아직 없을 때(404) ===")
# 배포자가 파일을 올리기 전이라고 전부 멈추면 아무도 못 씁니다.
d = fresh_dir()
dist_control.fetch_control = fake_fetch({})
check("404는 '막지 않음'으로 봅니다", dist_control.evaluate(d)["allowed"] is True)

print()
print("=== 네트워크가 안 될 때 ===")
d = fresh_dir()
dist_control.fetch_control = fake_fetch(None, "연결 실패")
state = dist_control.evaluate(d)
check("한 번도 확인 못 한 첫 실행은 허용", state["allowed"] is True, str(state))
check("유예로 판단했다고 표시", state["source"] == "grace", str(state))

# 확인에 성공한 적이 있고, 유예 안이면 계속 허용
d = fresh_dir()
dist_control.fetch_control = fake_fetch({"allowed": True})
dist_control.evaluate(d)
dist_control.fetch_control = fake_fetch(None, "연결 실패")
state = dist_control.evaluate(d)
check("유예 안이면 오프라인도 허용", state["allowed"] is True, str(state))
check("캐시로 판단했다고 표시", state["source"] == "cache", str(state))

# 유예가 지나면 한 번은 확인해야 합니다
cache = dist_control.read_cache(d)
cache["checked_at"] = time.time() - (dist_control.GRACE_SECONDS + 3600)
dist_control.write_cache(d, cache)
state = dist_control.evaluate(d)
check("유예가 지나면 차단", state["allowed"] is False, str(state))
check("사유가 네트워크 문제임을 알려줍니다", "네트워크" in (state["message"] or ""), str(state))

print()
print("=== 네트워크를 끊어 차단을 피할 수는 없습니다 ===")
# 차단을 한 번 받은 뒤 랜선을 뽑아도 계속 차단이어야 합니다.
d = fresh_dir()
dist_control.fetch_control = fake_fetch({"allowed": False, "message": "중지됨"})
dist_control.evaluate(d)
dist_control.fetch_control = fake_fetch(None, "연결 실패")
state = dist_control.evaluate(d)
check("차단은 오프라인에서도 유지됩니다", state["allowed"] is False, str(state))
check("차단 사유도 유지됩니다", state["message"] == "중지됨", str(state))

print()
print("=== 관리 파일 주소 ===")
dist_control.fetch_control = REAL_FETCH
os.environ.pop("DEVICE_FARM_CONTROL_URL", None)
check("기본 주소가 있습니다", dist_control.control_url().startswith("https://"),
      dist_control.control_url())
os.environ["DEVICE_FARM_CONTROL_URL"] = "https://example.invalid/x.json"
check("환경변수로 바꿀 수 있습니다",
      dist_control.control_url() == "https://example.invalid/x.json")
os.environ.pop("DEVICE_FARM_CONTROL_URL")

print()
print("=== 저장소의 관리 파일 ===")
import json
with open(os.path.join(ROOT, "dist-control.json"), encoding="utf-8") as f:
    shipped = json.load(f)
check("기본값은 허용입니다", shipped.get("allowed") is True, str(shipped))
check("차단 목록은 비어 있습니다", shipped.get("blocked_ids") == [], str(shipped))
allowed, _ = dist_control.verdict(shipped, "누구든")
check("이 파일로는 아무도 막히지 않습니다", allowed is True)

print()
print(f"{len(fails)} failed")
sys.exit(1 if fails else 0)
