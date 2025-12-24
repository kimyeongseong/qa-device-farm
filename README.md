# QA Device Farm

USB로 꽂아둔 안드로이드 실기기를, 브라우저와 HTTP API 한 곳에서 공유해서 쓰는 셀프호스팅 디바이스 팜입니다.

QA 업무 중 실기기가 개인 PC에 묶여 있어서 생기던 문제 — 기기를 쓰려면 자리로 가야 하고, 누가 쓰는지 몰라 충돌하고, 자동화는 각자 로컬에서만 돌던 — 를 없애려고 만들었습니다.

```
브라우저 (대시보드)          CLI / CI 파이프라인
        │                          │
        └──────────┬───────────────┘
                   ▼
        server.py  (FastAPI, :8001)
        기기 목록 · 점유(lease) · 입력 · 설치 · 매크로 · 스크린샷
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   adb (adbutils)      ws-scrcpy (:8000)
        │               H.264 저지연 미러링
        ▼
   USB / 무선 연결 안드로이드 기기
```

---

## 무엇을 할 수 있나

| | |
|---|---|
| **기기 대시보드** | 연결된 기기의 모델·OS·해상도·배터리·IP를 한 화면에서. 기기별 별칭 지정. |
| **원격 조작** | 화면을 보면서 탭·스와이프·키 입력. 미러링은 ws-scrcpy(H.264), 조작은 adb input. |
| **점유(Lease)** | 기기를 쓰기 전에 이름을 걸어둡니다. TTL이 있어 죽은 CI 잡이 기기를 영구 점유하지 못합니다. |
| **다중 기기 배치 작업** | 여러 기기를 체크해서 APK 설치·매크로 재생·앱 제어를 한 번에. 기기별 성공/실패를 따로 보고하고, 남이 점유 중인 기기는 건너뜁니다. |
| **Logcat 수집 + 크래시 감지** | 기기별로 logcat을 버퍼에 모으고 `FATAL EXCEPTION`·ANR·네이티브 시그널을 자동으로 잡아냅니다. 필터·다운로드 지원. |
| **매크로 녹화/재생** | 조작을 타임스탬프째 기록해 JSON으로 저장하고 N회 반복 재생. 녹화 해상도를 같이 저장해서 **다른 해상도 기기에서도 좌표를 스케일링**해 재생합니다. |
| **앱 제어** | 실행·강제종료·데이터 초기화. 테스트 전 상태 리셋에 매번 쓰는 동작들. |
| **APK 설치/삭제** | 브라우저에서 APK를 드롭하면 선택한 기기에 `install -r`. 서드파티 패키지 목록 조회·삭제. |
| **무선 디버깅 전환** | 버튼 한 번으로 `tcpip 5555` + `connect`. USB를 뽑아도 유지. |
| **오디오 포워딩** | scrcpy 바이너리로 기기 소리를 PC로. |
| **CLI / CI 연동** | 세션 개념 없이 HTTP 호출만으로 기기 점유 → 조작 → 로그 확인 → 반납. |

---

## 빠르게 실행하기

**필요한 것:** Python 3.10+, Node.js 16+, [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)(adb가 PATH에 있거나 `scrcpy_bin/adb.exe`에 위치), USB 디버깅을 켠 안드로이드 기기.

```bash
git clone https://github.com/kimyeongseong/device-farm-server.git
cd device-farm-server

pip install -r requirements.txt

cd ws-scrcpy && npm install && npm run dist && cd ..
```

Windows에서는:

```bash
run_root_server.bat
```

직접 띄우려면 터미널 두 개에서:

```bash
python server.py
```

```bash
cd ws-scrcpy && npm start
```

- 대시보드 — http://localhost:8001/
- API 문서 (자동 생성) — http://localhost:8001/docs

기기 별칭을 미리 넣어두려면 `device_aliases.example.json`을 `device_aliases.json`으로 복사해서 편집하세요. 이 파일은 실기기 시리얼이 들어가므로 gitignore되어 있습니다.

---

## CLI로 쓰기

브라우저 없이 터미널·CI에서 그대로 쓸 수 있습니다.

```bash
python cli.py health
python cli.py devices

# 아무 놀고 있는 기기 하나 잡기 (시리얼이 출력됨)
python cli.py occupy --owner ci-smoke --ttl 300

python cli.py tap --serial R3CN30ABCDE --x 540 --y 1200 --owner ci-smoke
python cli.py screenshot --serial R3CN30ABCDE --out shot.jpg

python cli.py release --serial R3CN30ABCDE --owner ci-smoke
```

이미 다른 사람이 잡고 있으면 `409`와 함께 종료 코드 1이 떨어지므로, 파이프라인에서 바로 분기할 수 있습니다.

### 스모크 테스트 한 바퀴

```bash
SERIAL=$(python cli.py occupy --owner ci --ttl 600 | python -c 'import sys,json;print(json.load(sys.stdin)["serial"])')

python cli.py logcat start --serial $SERIAL --owner ci
python cli.py app --serial $SERIAL --action clear  --package com.example.app --owner ci
python cli.py app --serial $SERIAL --action launch --package com.example.app --owner ci
python cli.py batch-macro --serials $SERIAL --name login_flow --owner ci

# 크래시가 하나라도 잡히면 종료 코드 2 -> CI 스텝이 실패합니다
python cli.py logcat tail --serial $SERIAL --lines 200

python cli.py logcat save --serial $SERIAL --out artifacts/logcat.txt
python cli.py logcat stop --serial $SERIAL
python cli.py release --serial $SERIAL --owner ci
```

### 여러 기기에 한 번에

```bash
python cli.py batch-install --serials R3CN30ABCDE,HA1EJ0000,9A271FFAZ0 --apk build/app-debug.apk
```

```bash
python cli.py batch-macro --serials R3CN30ABCDE,HA1EJ0000 --name login_flow --count 3
```

배치 명령은 기기별 결과를 따로 돌려주고, 한 대라도 실패하거나 남이 점유 중이면 `status: partial` + 종료 코드 1이 됩니다.

```json
{
  "status": "partial", "total": 2, "succeeded": 1, "failed": 0, "skipped": 1,
  "results": [
    { "serial": "R3CN30ABCDE", "status": "skipped", "message": "held by 'ci-smoke'" },
    { "serial": "HA1EJ0000",   "status": "success", "message": "ok" }
  ]
}
```

---

## API

`http://localhost:8001/docs`에 전체 스펙이 자동 생성됩니다. 자주 쓰는 것만:

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/health` | 서버·adb 상태, 전체/유휴 기기 수 |
| `GET` | `/api/devices` | 기기 목록 (점유 상태 포함) |
| `GET` | `/api/device/{serial}/screenshot` | JPEG 스크린샷 |
| `GET` | `/api/info/{serial}` | 상세 정보 (제조사·CPU·현재 앱·IP) |
| `POST` | `/api/devices/occupy` | 유휴 기기 아무거나 점유 |
| `POST` | `/api/device/{serial}/occupy` | 특정 기기 점유 |
| `POST` | `/api/device/{serial}/release` | 반납 |
| `GET` | `/api/leases` | 현재 점유 현황 |
| `POST` | `/api/device/{serial}/input` | tap / swipe / key / text |
| `POST` | `/api/install/{serial}` | APK 업로드 설치 |
| `GET` | `/api/packages/{serial}` | 서드파티 패키지 목록 |
| `POST` | `/api/uninstall/{serial}` | 패키지 삭제 |
| `POST` | `/api/app/{serial}/{action}` | 앱 `launch` / `stop` / `clear` |
| `POST` | `/api/wireless/{serial}` | 무선 디버깅 전환 |
| `POST` | `/api/macros/start_record/{serial}` | 매크로 녹화 시작 (해상도 같이 저장) |
| `POST` | `/api/macros/play/{serial}` | 매크로 재생 (반복 횟수 지정, 좌표 자동 스케일) |
| `GET` | `/api/macros` | 매크로 목록 + 스텝 수·녹화 해상도 |
| `DELETE` | `/api/macros/{name}` | 매크로 삭제 |
| `POST` | `/api/logcat/{serial}/start` | 로그 수집 시작 (`level`, `clear` 지정) |
| `POST` | `/api/logcat/{serial}/stop` | 수집 중지 |
| `GET` | `/api/logcat/{serial}` | 버퍼 조회 (`tail`, `contains`) + 감지된 크래시 |
| `GET` | `/api/logcat/{serial}/download` | 전체 버퍼를 텍스트 파일로 |
| `GET` | `/api/logcat` | 수집 중인 기기와 크래시 건수 |
| `POST` | `/api/batch/input` | 여러 기기에 동시 입력 |
| `POST` | `/api/batch/app` | 여러 기기에 앱 제어 |
| `POST` | `/api/batch/macro` | 여러 기기에 매크로 재생 |
| `POST` | `/api/batch/install` | 여러 기기에 APK 설치 |
| `WS` | `/ws/video/{serial}` | screenrecord H.264 스트림 |
| `WS` | `/ws/control/{serial}` | 실시간 입력 채널 |

점유 예시:

```bash
curl -X POST localhost:8001/api/devices/occupy \
  -H 'Content-Type: application/json' \
  -d '{"owner":"ci-smoke","ttl_seconds":300}'
```

```json
{ "status": "success", "serial": "R3CN30ABCDE", "owner": "ci-smoke", "expires_at": 1786000000.0 }
```

---

## 설계 메모

- **세션이 없습니다.** 기기를 조작하려고 세션을 열고 닫지 않습니다. 조작 하나가 HTTP 요청 하나이고, 서버는 요청 사이에 아무것도 붙들지 않습니다. 기기 목록만 adb에서 그때그때 읽습니다.
- **점유는 시한부입니다.** lease에 TTL이 있어서, 잡은 쪽이 죽어도 기기가 알아서 풀립니다.
- **점유를 강제하는 범위를 나눴습니다.** 화면을 보고 탭하는 실시간 조작(WebSocket)은 막지 않고 대시보드에 점유자만 표시합니다 — 사람이 급히 확인해야 할 때 락에 막히면 곤란하니까요. 반면 되돌릴 수 없는 HTTP 동작(입력 API, 앱 제어, 배치 전부)은 점유를 강제합니다. 남의 CI가 도는 기기의 앱 데이터를 지우면 그 회차가 통째로 날아갑니다.
- **배치는 부분 실패를 인정합니다.** 열 대에 돌리면 한 대는 빠져 있거나 남이 쓰고 있습니다. 전체를 실패시키는 대신 기기별로 `success` / `error` / `skipped`를 따로 돌려주고, 하나라도 성하지 않으면 `partial`로 표시합니다.
- **입력은 셸을 거치지 않습니다.** 좌표와 키코드는 정수로 파싱한 뒤 adb에 argv로 넘깁니다. 문자열을 조립해 셸에 던지지 않습니다.
- **기기당 스크린샷 직렬화.** 같은 기기에 스크린샷 요청이 겹치면 adb가 불안정해져서, 기기별 `asyncio.Lock`으로 한 번에 하나만 통과시킵니다.

설계 배경과 한계는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 정리했습니다.

---

## 알려진 한계

- **안드로이드 전용입니다.** iOS는 지원하지 않습니다.
- **인증이 없습니다.** 신뢰할 수 있는 네트워크 안에서만 쓰세요. `0.0.0.0:8001`에 붙고 인증 계층이 없어서, 접근할 수 있는 사람은 누구나 기기를 조작하고 APK를 설치할 수 있습니다. 공개 노출은 터널 앞단에 인증을 두는 것을 전제로 합니다.
- **한글·이모지 입력이 안 됩니다.** `adb shell input text`가 ASCII만 받습니다. 기기에 별도 IME를 붙여야 합니다.
- **매크로는 여전히 좌표 기반입니다.** 해상도 차이는 비례 스케일링으로 보정하지만, 화면비가 다르거나 레이아웃 자체가 바뀌는 기기(폴더블, 태블릿)에서는 어긋납니다. UI 요소를 찾아 누르는 방식이 아닙니다. 녹화 전에 저장된 v1 매크로는 해상도 정보가 없어 스케일 없이 재생됩니다.
- **로그 버퍼는 메모리에 있고 기기당 2만 줄입니다.** 넘치면 오래된 줄부터 버려집니다. 길게 돌릴 때는 중간중간 다운로드하세요. 서버를 재시작하면 사라집니다.
- **점유 상태도 메모리에만 있습니다.** 서버를 재시작하면 lease가 전부 사라집니다.

---

## 참고

기기 점유(lease) 모델과 세션 없는 조작 API는 [토스의 디바이스 팜 Nebula 아티클](https://toss.tech/article/device-farm-nebula)을 참고해 이 프로젝트 규모에 맞게 축소 적용했습니다. Nebula가 다루는 분산 락·에이전트 계층·iOS 미러링·자체 드라이버는 이 저장소의 범위 밖입니다.

---

## 라이선스

MIT — 김영성. 번들된 서드파티 구성요소는 [NOTICE.md](NOTICE.md)를 참고하세요.
