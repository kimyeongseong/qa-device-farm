# QA Device Farm

한국어 · [English](README.en.md)

**[MIT 라이선스](LICENSE)** — 김영성 &lt;cds04130@kakao.com&gt;
번들된 서드파티 구성요소는 [NOTICE.md](NOTICE.md), 릴리즈별 변경 사항은 [CHANGELOG.md](CHANGELOG.md)에 있습니다.

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
   adb (adbutils)      ws-scrcpy (:8010)
        │               H.264 저지연 미러링
        ▼
   USB / 무선 연결 안드로이드 기기
```

---

## 무엇을 할 수 있나

| | |
|---|---|
| **기기 대시보드** | 연결된 기기의 모델·OS·해상도·배터리·RAM·IP를 한 화면에서. 기기별 별칭 지정. USB 승인 대기·offline 기기도 이유와 함께 표시합니다. |
| **원격 조작** | 화면을 보면서 탭·스와이프·키 입력. 미러링은 ws-scrcpy(H.264), 조작은 adb input. |
| **점유(Lease)** | 기기를 쓰기 전에 이름을 걸어둡니다. TTL이 있어 죽은 CI 잡이 기기를 영구 점유하지 못합니다. |
| **다중 기기 배치 작업** | 여러 기기를 체크해서 APK 설치·매크로 재생·앱 제어를 한 번에. 기기별 성공/실패를 따로 보고하고, 남이 점유 중인 기기는 건너뜁니다. |
| **Logcat 수집 + 크래시 감지** | 기기별로 logcat을 모으고 `FATAL EXCEPTION`·ANR·네이티브 시그널을 자동 감지. 필터·다운로드, 장시간 실행용 파일 저장 지원. |
| **매크로 녹화/재생** | 조작을 타임스탬프째 기록해 JSON으로 저장하고 N회 반복 재생. 녹화 해상도를 같이 저장해서 **다른 해상도 기기에서도 좌표를 스케일링**해 재생합니다. |
| **앱 제어** | 실행·강제종료·데이터 초기화. 테스트 전 상태 리셋에 매번 쓰는 동작들. |
| **APK 설치/삭제** | 브라우저에서 APK를 드롭하면 선택한 기기에 `install -r`. 서드파티 패키지 목록 조회·삭제. |
| **무선 디버깅 전환** | 버튼 한 번으로 `tcpip 5555` + `connect`. USB를 뽑아도 유지. |
| **오디오 포워딩** | scrcpy 바이너리로 기기 소리를 PC로. |
| **Picture-in-Picture** | 미러링 화면을 항상 위에 뜨는 작은 창으로. 다른 창에서 작업하면서 기기를 계속 보거나, 여러 대를 동시에 띄워둘 수 있습니다. |
| **CLI / CI 연동** | 세션 개념 없이 HTTP 호출만으로 기기 점유 → 조작 → 로그 확인 → 반납. |

---

## 빠르게 실행하기

**필요한 것:** Python 3.10+, Node.js 16+, [Android platform-tools](https://developer.android.com/tools/releases/platform-tools), USB 디버깅을 켠 안드로이드 기기.

adb는 `scrcpy_bin/` 안의 것을 먼저 쓰고, 없으면 PATH에서 찾습니다. 서버가 실제로 어느 adb를 쓰는지는 `GET /api/health`의 `adb_path`로 확인할 수 있습니다.

**두 서버가 adb를 찾는 방식이 다릅니다.** `server.py`는 위 순서로 경로를 직접 해석하지만, **ws-scrcpy는 `adb`를 PATH에서 찾아 프로세스로 띄웁니다**(프로토콜을 직접 말하지 않고 바이너리를 실행합니다). 그래서 adb가 `scrcpy_bin/`에만 있으면 대시보드·입력·설치는 다 되는데 미러링만 안 되고, ws-scrcpy 로그에 `spawn adb ENOENT`가 남습니다. `run_root_server.bat`은 `scrcpy_bin/`을 PATH 앞에 붙여 두 서버를 맞춰주지만, `npm start`를 직접 띄운다면 **adb가 PATH에 있어야 합니다.**

오디오 포워딩은 [scrcpy](https://github.com/Genymobile/scrcpy/releases) 2.7 이상 바이너리가 따로 필요합니다(라이선스상 이 저장소에 포함하지 않습니다). `scrcpy_bin/`에 두거나 PATH에 추가하세요. 없으면 오디오 버튼만 503을 반환하고 나머지 기능은 정상 동작합니다.

```bash
git clone https://github.com/kimyeongseong/qa-device-farm.git
cd qa-device-farm

pip install -r requirements.txt

cd ws-scrcpy && npm install && npm run dist && cd ..
```

> **번들된 ws-scrcpy에 대해** — upstream(`2bde541`) 원본 상태로는 최신 Node에서 설치도 빌드도
> 되지 않았습니다. 이 저장소에는 그 수정이 적용되어 있어 위 명령이 그대로 동작합니다.
> 무엇을 왜 고쳤는지는 [NOTICE.md](NOTICE.md)에 있습니다. 요점:
>
> - 이 팜이 쓰지 않는 iOS/Appium 경로와 브라우저 내 셸을 빌드에서 제외했습니다.
>   그 결과 의존성 878개 → 446개, 보안 권고 54건(critical 2, high 17) → **0건**.
> - `node-pty`(2021년판, Node 24에서 네이티브 빌드 실패)가 빠져 별도 빌드 도구 없이 설치됩니다.
> - upstream의 `package-lock.json`이 자기 `package.json`과 어긋나 `npm ci`가 거부되던 문제도
>   해결됐습니다.

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

### 접근 토큰

기본은 인증 없음입니다. 신뢰된 사내망이면 그대로 쓰고, 팜을 밖으로 노출할 때 토큰을 켜세요.

```bash
# Windows
set DEVICE_FARM_TOKEN=아무거나-긴-임의문자열
# Linux/mac
export DEVICE_FARM_TOKEN=아무거나-긴-임의문자열
```

켜면 API 전체가 `X-Farm-Token` 헤더(또는 `?token=`)를 요구합니다. 대시보드는 처음 한 번 물어보고
`localStorage`에 담아두며, CLI는 `DEVICE_FARM_TOKEN` 환경변수나 `--token`을 씁니다.

```bash
DEVICE_FARM_TOKEN=... python cli.py devices
python cli.py --token ... devices
```

토큰 없이도 열려 있는 것: 대시보드 페이지와 정적 파일(페이지가 토큰을 물어봐야 하니까),
그리고 `/api/health` — 모니터링이 생존 확인만 하려고 비밀을 알 필요는 없습니다.

브라우저는 WebSocket에 헤더를 못 실으므로 `/ws/...`는 쿼리스트링으로 토큰을 받습니다.
같은 이유로 썸네일과 로그 다운로드는 `fetch` 후 blob으로 넘깁니다 — 2초마다 도는 폴링 URL에
토큰을 붙이면 접근 로그마다 비밀이 남기 때문입니다.

### 포트

| 포트 | 프로세스 | 바꾸는 법 |
|---|---|---|
| 8001 | 대시보드·API (`server.py`) | `DEVICE_FARM_PORT` 환경변수 (호스트는 `DEVICE_FARM_HOST`) |
| 8010 | 스트림 서버 (ws-scrcpy) | **`ws-scrcpy.config.json`** |

포트가 이미 점유돼 있으면 서버가 기동을 거부하고 누가 쓰는지 찾는 명령까지 알려줍니다. 원시 `OSError` 스택트레이스를 보는 것보다 낫고, Windows에서 다른 프로그램이 조용히 대신 응답하는 상황(아래)을 곧바로 드러냅니다.

스트림 포트는 `ws-scrcpy.config.json` **한 곳에서만** 정의합니다. ws-scrcpy는 `WS_SCRCPY_CONFIG` 환경변수로 이 파일을 읽고, `server.py`도 같은 파일을 읽어 `GET /api/config`로 대시보드에 알려줍니다. 포트를 바꿀 때 이 파일만 고치면 됩니다.

ws-scrcpy 기본값인 8000을 쓰지 않는 이유가 있습니다. 8000은 언리얼 에디터·Django·`python -m http.server` 등이 흔히 쓰고, **Windows에서는 `127.0.0.1:8000`에 붙은 프로세스가 `::`에 붙은 프로세스를 이깁니다.** 그러면 대시보드가 미러링 프레임에 그 프로그램의 에러 페이지를 그대로 띄워서, 원인 찾기가 매우 어려워집니다. 실제로 개발 중에 이 상황을 겪었습니다.

**미러링이 안 나올 때**: 스트림 창 상단에 실제로 접속 중인 주소(`호스트:포트`)가 표시됩니다. 그 포트를 다른 프로그램이 쓰고 있는지 확인하세요.

```bash
Get-NetTCPConnection -LocalPort 8010 -State Listen | ForEach-Object { Get-Process -Id $_.OwningProcess }
```

점유돼 있으면 `ws-scrcpy.config.json`의 `port`만 비어 있는 값으로 바꾸고 두 서버를 재시작하면 됩니다.

**미러링 화질이 갑자기 나빠질 때 — 같은 기기를 보는 사람이 또 있습니다**: ws-scrcpy는 한 기기에 여러 시청자가 붙으면 **가장 작은 창에 맞춰 스트림 해상도를 협상합니다**(`StreamClientScrcpy.ts`의 최소값 선택). 동료가 작은 창으로 같은 기기를 열면 내 화면도 같이 내려갑니다. 실측: 폭 385px 창 하나가 붙자 1600×2560 태블릿이 **320×512**로 떨어졌고, 그 창을 닫고 새로고침하니 창 크기대로 복구됐습니다.

PiP 창 크기는 브라우저가 **영상의 원본 해상도**로 정하기 때문에 이 영향을 그대로 받습니다. PiP를 크게 쓰려면 스트림 창을 충분히 크게 띄운 뒤 진입하세요. 해상도를 직접 고정하려면 툴바의 **⋯ (More)** 에서 bitrate와 최대 크기를 지정할 수 있습니다.

```bash
adb -s <serial> shell "ps -A | grep app_process"
```

기기에 스트림 프로세스가 하나만 보이는데도 화질이 낮으면 브라우저 탭이 여러 개 붙어 있는지 확인하세요.

**adb 서버를 재시작한 뒤 미러링이 안 될 때**: `adb kill-server`를 하면 ws-scrcpy가 들고 있던 연결이 끊기고, 그 node 프로세스는 스스로 복구하지 않습니다. 브라우저 콘솔에 `WS closed: socket hang up`이 뜨면 **ws-scrcpy를 재시작**하세요. `[스트림 정리]`로는 안 됩니다 — 그건 기기 쪽 잔존 프로세스를 지우는 버튼이고, 이 경우는 PC 쪽 상태가 문제입니다.

**미러링이 검은 화면이거나 `unknown host service` 오류가 날 때**: ws-scrcpy를 재시작해도 기기 안의 scrcpy 서버 프로세스가 남아 있는 경우가 있습니다. 그 잔존 프로세스가 포트를 쥐고 있으면 새 연결이 붙지 못합니다. 기기 쪽을 정리하고 다시 시도하세요.

```bash
adb -s <serial> shell "ps -A | grep app_process"
```

대시보드 기기 카드의 **[스트림 정리]** 버튼이 이걸 대신합니다 (`POST /api/device/{serial}/reset-stream`).
`app_process`라는 이름만 보고 지우면 다른 앱까지 죽으므로, scrcpy 클래스명으로 정확히 골라 정리합니다.
기기를 뽑았다 꽂은 뒤에도 자주 발생합니다.

**무선 연결이 자꾸 끊길 때 — adb 버전 충돌**: PC에 adb 바이너리가 여러 개 있고 버전이 다르면,
서로 상대의 adb 서버를 죽입니다.

```
adb server version (40) doesn't match this client (41); killing...
```

`adb connect`로 만든 무선 연결은 **서버 상태**라서 서버가 재시작될 때마다 사라집니다. USB 기기는
다시 잡히므로 무선만 조용히 빠지는 것처럼 보입니다. 개발 중 SuperDisplay가 자체 adb 1.0.40
서비스를 계속 띄워 이 문제를 겪었습니다.

```bash
Get-CimInstance Win32_Process -Filter "Name='adb.exe'" | Select-Object ProcessId, ExecutablePath
```

여러 경로가 나오면 하나로 통일하세요. 다른 adb를 쓰는 프로그램(스크린 미러링 유틸리티 등)이
있으면 그 서비스를 끄거나, 같은 버전의 adb를 쓰도록 맞춰야 무선 연결이 유지됩니다.

### Node 없이 쓰는 간이 미러링

ws-scrcpy를 띄우지 않고 `server.py`만으로도 미러링이 됩니다. `adb exec-out screenrecord`의
H.264를 WebSocket으로 넘겨 브라우저에서 jmuxer로 디코드하는 경로입니다.

```
http://localhost:8001/control?serial=<serial>&model=<model>
```

측정값(레노보 TB373FU, USB): 전체 해상도 2944×1840로 재생되고, 조작 → 화면 반영까지
**약 1.3초**(앱 실행 시간 포함)입니다. 화면을 지켜보는 모니터링에는 충분하지만, 즉각적인
반응이 필요한 조작에는 ws-scrcpy 경로가 낫습니다.

Node·npm 설치가 어려운 환경, 또는 ws-scrcpy가 죽었을 때의 대체 경로로 쓰세요.
대시보드 기기 카드의 **[간이 미러링]** 버튼으로 바로 열 수 있습니다.

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
| `GET` | `/api/health` | 서버·adb 상태, 기기 수, adb 경로·서버 버전 (토큰 불필요) |
| `GET` | `/api/config` | 대시보드가 알아야 할 값 (스트림 포트) |
| `GET` | `/api/devices` | 기기 목록 (점유 상태 + `state`/`state_hint`). `?refresh=1` 로 캐시 무시 |
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
| `POST` | `/api/alias/{serial}` | 기기 별칭 지정 (빈 값이면 모델명으로 복원) |
| `POST` | `/api/audio/start/{serial}` | 기기 소리를 PC로 (scrcpy 바이너리 필요) |
| `POST` | `/api/audio/stop/{serial}` | 오디오 중지 |
| `POST` | `/api/wireless/{serial}` | 무선 디버깅 전환 |
| `POST` | `/api/usb/{serial}` | USB 모드로 복귀 (위의 반대) |
| `POST` | `/api/device/{serial}/reset-stream` | 기기에 남은 스트림 프로세스 정리 |
| `POST` | `/api/macros/start_record/{serial}` | 매크로 녹화 시작 (해상도 같이 저장) |
| `POST` | `/api/macros/stop_record/{serial}` | 녹화 종료 후 이름 지정해 저장 |
| `POST` | `/api/macros/play/{serial}` | 매크로 재생 (반복 횟수 지정, 좌표 자동 스케일) |
| `GET` | `/api/macros` | 매크로 목록 + 스텝 수·녹화 해상도 |
| `DELETE` | `/api/macros/{name}` | 매크로 삭제 |
| `POST` | `/api/logcat/{serial}/start` | 로그 수집 시작 (`level`, `clear`, `to_file`) |
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

### 수집한 로그 분석

장시간 캡처는 수만 줄이라 처음부터 읽을 수 없습니다. 실행 후 알고 싶은 것만 뽑아줍니다.

```bash
python analyze_logs.py logs/logcat_R3CN30_20251228-004055.txt
```

```
2264 lines, 2 without a standard header

레벨별            Fatal 상위 태그        크래시
  F Fatal      1     1  AndroidRuntime     line 1604  [java crash] F/AndroidRuntime(22117): FATAL EXCEPTION: main
  E Error     46   Error 상위 태그
  W Warn       1    37  HfLooper
  D Debug   2183     9  mtk_storageproxyd
```

크래시가 하나라도 있으면 종료 코드 2로 끝나므로 CI 스텝에서 바로 쓸 수 있습니다.

---

## 테스트

기기 없이 돌아갑니다. adb 계층을 가짜로 바꿔서, 실제 폰이 없어도 전 구간이 검증됩니다.

```bash
pip install -r requirements-dev.txt
python tests/run_all.py
```

```
test_leases_and_input.py     ok       26 passed, 0 failed
test_features.py             ok      194 passed, 0 failed
test_edge_cases.py           ok       15 passed, 0 failed
test_cli.py                  ok       24 passed, 0 failed
259 passed, 0 failed across 4 suites
```

각 스위트는 별도 프로세스에서 임시 디렉터리를 cwd로 잡고 돌기 때문에, 서로의 monkeypatch나
런타임 상태(`device_leases.json`, `macros/`)가 섞이지 않고 저장소도 오염되지 않습니다.

무엇을 덮는지: 점유 충돌·TTL 만료·풀 고갈, 입력 인젝션 차단, 매크로 해상도 스케일링과 v1 호환,
경로 탈출 차단, 앱 제어 argv, 크래시 패턴 매칭, logcat 죽은 세션 복구와 파일 저장, 배치 부분 실패 격리,
무선 시리얼 4종, 기기 정보 캐시, lease 영속화, 접근 토큰 경계, 잔존 스트림 프로세스 선별 종료,
CLI 전 서브커맨드.

`cli.py`는 실제 서브프로세스로 띄워 검증합니다 — 인프로세스 테스트로는 못 잡는 인자 처리 버그가
실제로 있었기 때문입니다.

**기기가 있어야만 되는 것**은 자동화하지 않았습니다: 미러링 화질·지연, 오디오, 실제 크래시 감지,
무선 전환. 이건 실기기로 수동 확인했습니다.

---

## 설계 메모

- **세션이 없습니다.** 기기를 조작하려고 세션을 열고 닫지 않습니다. 조작 하나가 HTTP 요청 하나이고, 서버는 요청 사이에 기기 세션을 붙들지 않습니다. 상태로 남기는 건 점유(lease)와 폴링용 기기 정보 캐시뿐입니다.
- **점유는 시한부입니다.** lease에 TTL이 있어서, 잡은 쪽이 죽어도 기기가 알아서 풀립니다.
- **점유를 강제하는 범위를 나눴습니다.** 화면을 보고 탭하는 실시간 조작(WebSocket)은 막지 않고 대시보드에 점유자만 표시합니다 — 사람이 급히 확인해야 할 때 락에 막히면 곤란하니까요. 반면 되돌릴 수 없는 HTTP 동작(입력 API, 앱 제어, 배치 전부)은 점유를 강제합니다. 남의 CI가 도는 기기의 앱 데이터를 지우면 그 회차가 통째로 날아갑니다.
- **배치는 부분 실패를 인정합니다.** 열 대에 돌리면 한 대는 빠져 있거나 남이 쓰고 있습니다. 전체를 실패시키는 대신 기기별로 `success` / `error` / `skipped`를 따로 돌려주고, 하나라도 성하지 않으면 `partial`로 표시합니다.
- **입력은 셸을 거치지 않습니다.** 좌표와 키코드는 정수로 파싱한 뒤 adb에 argv로 넘깁니다. 문자열을 조립해 셸에 던지지 않습니다.
- **기기 정보는 캐시합니다.** 대시보드가 2초마다 폴링하는데, 기기 하나를 adb로 캐묻는 데 실측 0.8~2.6초가 걸립니다. 폴링 주기보다 느린 데다 기기 수에 비례해 늘어나서, 팜이 커질수록 먼저 무너지는 지점입니다. 모델·해상도·SDK는 연결 중 바뀌지 않으니 한 번만 읽고, 배터리(20초)·IP(60초)만 짧게 캐시합니다. 폴링 응답이 **0.8~2.6초 → 약 25ms**가 됐습니다. 최신값이 필요하면 `?refresh=1`.
- **점유는 파일로 남습니다.** `device_leases.json`에 기록해서 서버를 재시작해도 유지됩니다. 재시작은 하필 가장 곤란한 순간에 일어납니다 — 기기를 잡고 있는 CI 잡은 계속 돌고 있는데, 팜만 그 기기를 유휴로 착각하는 상황이니까요. 서버가 죽어 있는 동안 만료된 lease는 복원하지 않습니다.
- **`/api/health`가 adb 서버 버전을 보고합니다.** 클라이언트 경로만으로는 드러나지 않는 문제가 있습니다 — 머신에 다른 버전의 adb가 있으면 서로 서버를 죽이고, 그때마다 무선 연결이 전부 끊깁니다. 41 미만이면 경고 문구가 함께 나옵니다.
- **adb 실행 파일이 없으면 `degraded`입니다.** adbutils는 adb 서버에 TCP로 붙기 때문에, 머신에 adb 바이너리가 하나도 없어도 기기 목록·상세정보·썸네일은 정상으로 나옵니다. 반면 프로세스를 띄우는 기능(입력·앱 제어·설치·logcat·무선·간이 미러링)은 전부 실패합니다. 경로만 보고하면 이 상태에서 팜이 `ok`라고 답합니다. 그래서 해석된 경로가 실제로 존재하는지 확인하고, 없으면 무엇이 죽는지까지 `adb_binary`에 담아 돌려줍니다.
- **기기당 스크린샷 직렬화.** 같은 기기에 스크린샷 요청이 겹치면 adb가 불안정해져서, 기기별 `asyncio.Lock`으로 한 번에 하나만 통과시킵니다.
- **adb 조회는 스레드에서 돕니다.** adbutils는 동기 라이브러리라 `async def` 핸들러에서 그대로 부르면 이벤트 루프가 멈춥니다. 실측으로 로컬 파일만 읽는 `/api/config`가 7ms → 750ms가 됐습니다. 공유 팜에서 한 사람이 기기 상세를 열면 다른 사람 대시보드까지 서는 셈이라, adb를 만지는 조회는 `asyncio.to_thread`로 넘깁니다(같은 조건에서 10ms 이내).

설계 배경과 한계는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 정리했습니다.

---

## 알려진 한계

- **안드로이드 전용입니다.** iOS는 지원하지 않습니다.
- **인증은 선택입니다.** `DEVICE_FARM_TOKEN`을 설정하지 않으면 접근 가능한 누구나 기기를 조작하고 APK를 설치할 수 있습니다. 사내망 전용이면 그대로도 되지만, 밖으로 노출한다면 반드시 켜세요. 공유 비밀 하나이지 사용자별 계정이 아닙니다.
- **한글·이모지 입력이 안 됩니다.** `adb shell input text`가 ASCII만 받습니다. 기기에 별도 IME를 붙여야 합니다.
- **매크로는 여전히 좌표 기반입니다.** 해상도 차이는 비례 스케일링으로 보정하지만, 화면비가 다르거나 레이아웃 자체가 바뀌는 기기(폴더블, 태블릿)에서는 어긋납니다. UI 요소를 찾아 누르는 방식이 아닙니다. 녹화 전에 저장된 v1 매크로는 해상도 정보가 없어 스케일 없이 재생됩니다.
- **메모리 로그 버퍼는 기기당 2만 줄입니다.** 넘치면 오래된 줄부터 버려지고 서버 재시작 시 사라집니다. 생각보다 빨리 찹니다 — 레벨 V로 앱이 돌고 있는 태블릿에서 실측 초당 400~1,000줄, 즉 **30초~1분이면 한 바퀴**입니다. 가득 차면 로그 탭이 `버퍼 가득 · 오래된 로그 삭제 중`을 띄우지만, 재현이 길면 처음부터 `to_file`(대시보드의 '파일로 저장')을 켜세요 — `logs/` 에 전체가 남습니다.
- **PiP는 Chromium 계열에서만 됩니다.** Firefox는 이 API가 없어서 버튼이 아예 나타나지 않습니다. 브라우저 정책상 사용자가 직접 클릭해야 진입합니다(스크립트 클릭으로는 안 됩니다).
