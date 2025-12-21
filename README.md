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
| **매크로 녹화/재생** | 조작을 타임스탬프째 기록해 JSON으로 저장하고, N회 반복 재생. |
| **APK 설치/삭제** | 브라우저에서 APK를 드롭하면 선택한 기기에 `install -r`. 서드파티 패키지 목록 조회·삭제. |
| **무선 디버깅 전환** | 버튼 한 번으로 `tcpip 5555` + `connect`. USB를 뽑아도 유지. |
| **오디오 포워딩** | scrcpy 바이너리로 기기 소리를 PC로. |
| **CLI / CI 연동** | 세션 개념 없이 HTTP 호출만으로 기기 점유 → 조작 → 반납. |

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
| `POST` | `/api/wireless/{serial}` | 무선 디버깅 전환 |
| `POST` | `/api/macros/start_record/{serial}` | 매크로 녹화 시작 |
| `POST` | `/api/macros/play/{serial}` | 매크로 재생 (반복 횟수 지정) |
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
- **점유는 시한부입니다.** lease에 TTL이 있어서, 잡은 쪽이 죽어도 기기가 알아서 풀립니다. 점유는 브라우저 UI에는 표시만 하고(강제하지 않고), CI가 쓰는 HTTP 입력 API에서만 강제합니다 — 사람이 옆에서 급히 만져야 할 때 락에 막히면 곤란하니까요.
- **입력은 셸을 거치지 않습니다.** 좌표와 키코드는 정수로 파싱한 뒤 adb에 argv로 넘깁니다. 문자열을 조립해 셸에 던지지 않습니다.
- **기기당 스크린샷 직렬화.** 같은 기기에 스크린샷 요청이 겹치면 adb가 불안정해져서, 기기별 `asyncio.Lock`으로 한 번에 하나만 통과시킵니다.

설계 배경과 한계는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 정리했습니다.

---

## 알려진 한계

- **안드로이드 전용입니다.** iOS는 지원하지 않습니다.
- **인증이 없습니다.** 신뢰할 수 있는 네트워크 안에서만 쓰세요. `0.0.0.0:8001`에 붙고 인증 계층이 없어서, 접근할 수 있는 사람은 누구나 기기를 조작하고 APK를 설치할 수 있습니다. 공개 노출은 터널 앞단에 인증을 두는 것을 전제로 합니다.
- **한글·이모지 입력이 안 됩니다.** `adb shell input text`가 ASCII만 받습니다. 기기에 별도 IME를 붙여야 합니다.
- **매크로는 좌표 기반입니다.** 해상도가 다른 기기에서 그대로 재생하면 어긋납니다.
- **점유 상태는 메모리에만 있습니다.** 서버를 재시작하면 lease가 전부 사라집니다.

---

## 참고

기기 점유(lease) 모델과 세션 없는 조작 API는 [토스의 디바이스 팜 Nebula 아티클](https://toss.tech/article/device-farm-nebula)을 참고해 이 프로젝트 규모에 맞게 축소 적용했습니다. Nebula가 다루는 분산 락·에이전트 계층·iOS 미러링·자체 드라이버는 이 저장소의 범위 밖입니다.

---

## 라이선스

MIT — 김영성. 번들된 서드파티 구성요소는 [NOTICE.md](NOTICE.md)를 참고하세요.
