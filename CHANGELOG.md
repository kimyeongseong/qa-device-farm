# 변경 이력 / Changelog

이 프로젝트의 릴리즈 노트입니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/),
버전은 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다.

---

## [Unreleased]

실기기와 브라우저로 직접 돌려보다 나온 수정입니다.

### 수정 (Fixed)

- **`run_root_server.bat`이 실행되지 않던 문제** — 줄바꿈이 LF였습니다. cmd.exe는 .bat을
  바이트로 읽어서 CRLF가 아니면 글자를 흘리기 때문에, `Starting`이 `rting`이 되고
  `cd /d "%~dp0"`가 무시된 채 엉뚱하게 `[ERROR] python not found on PATH`가 떴습니다.
  CRLF로 바꾸고, 다른 머신에서 클론해도 재발하지 않게 `.gitattributes`에
  `*.bat text eol=crlf`를 고정했습니다.
- **스트림 서버 창이 열리자마자 죽던 문제** — `cmd /k "cd /d "경로" && npm start"` 형태로
  따옴표가 겹쳐 cmd가 파싱하지 못했습니다. 경로에 공백과 괄호가 있으면 더 확실히 깨집니다.
  `start`의 `/d` 옵션으로 작업 디렉터리를 넘겨 중첩을 없앴습니다.
- **adb 실행 파일이 없어도 `/api/health`가 `ok`라고 답하던 문제** — adbutils는 adb 서버에
  TCP로 붙으므로 기기 목록·상세정보·썸네일은 바이너리 없이도 나오고, 프로세스를 띄우는
  기능(입력·앱 제어·설치·logcat·무선·간이 미러링)만 조용히 전부 실패합니다. 경로만
  보고하던 탓에 팜이 절반 죽은 상태로 초록불이었습니다. 이제 해석된 경로의 존재를 확인해
  `status: "degraded"`와 `adb_binary`(무엇이 죽는지 포함)를 돌려주고, 기동 배너와 대시보드
  상태바도 `adb 실행 파일 없음`으로 정확히 표시합니다 — 전에는 `adb 연결 불가`로 나올
  자리였는데 adb 서버에는 닿으니 틀린 메시지입니다.
- **테스트 하나가 개발자 작업 트리 상태에 의존하던 문제** — adb 탐색 테스트가 실제
  `scrcpy_bin/` 디렉터리를 읽고 있어서, 번들 adb를 넣는 순간 깨졌습니다. 존재 여부를
  스텁으로 갈아 번들이 있을 때와 없을 때를 양방향으로 검증합니다.

검증 232개 → **241개**, 4스위트 전부 통과.

---

## [1.0.0] — 2025-12-28

첫 공개 릴리즈. 실기기 팜을 브라우저·CLI·CI 어디서든 같은 방식으로 쓸 수 있는 상태로 정리했습니다.

### 추가 (Added)

- **접근 토큰** — `DEVICE_FARM_TOKEN`을 설정하면 API 전체가 `X-Farm-Token`을 요구합니다.
  대시보드 페이지와 `/api/health`만 열어둡니다(페이지가 토큰을 물어봐야 하고, 모니터링이
  생존 확인만 하려고 비밀을 알 필요는 없으니까요). WebSocket은 헤더를 못 실으므로 쿼리스트링으로
  받고, 썸네일·로그 다운로드는 `fetch` + blob으로 넘겨 폴링 URL에 비밀이 남지 않게 했습니다.
- **Picture-in-Picture** — 미러링 화면을 항상 위에 뜨는 창으로 띄웁니다. 여러 대를 동시에
  띄워둘 수 있습니다. ws-scrcpy 툴바의 동작하지 않던 매크로 버튼을 대체했습니다.
- **`POST /api/device/{serial}/reset-stream`** — 기기 안에 남은 scrcpy 서버 프로세스를 정리합니다.
  `app_process` 이름만 보고 죽이면 무관한 앱까지 날아가므로 scrcpy 클래스명으로 골라 종료합니다.
- **`GET /api/config`** — 스트림 포트를 대시보드에 알려줍니다. 포트는 `ws-scrcpy.config.json`
  한 곳에서만 정의하고 `server.py`와 ws-scrcpy가 같은 파일을 읽습니다.
- **점유(lease) 영속화** — `device_leases.json`에 남겨 서버 재시작에도 유지됩니다. 서버가 죽어
  있는 동안 만료된 lease는 복원하지 않습니다.
- **기기 상태 표시** — USB 승인 대기·offline 기기가 목록에서 사라지지 않고 이유(`state_hint`)와
  함께 표시됩니다.
- **`POST /api/batch/install`** — 여러 기기에 APK를 한 번에 설치합니다.
- **`DELETE /api/macros/{name}`** — 매크로 삭제. 이름에 경로 탈출 문자가 들어오면 거부합니다.
- **logcat 파일 저장(`to_file`)과 전체 다운로드** — 메모리 버퍼(기기당 2만 줄)를 넘기는
  장시간 실행용.
- **`analyze_logs.py`** — 수집한 logcat에서 레벨 분포·상위 태그·크래시 위치를 뽑습니다.
  크래시가 있으면 종료 코드 2.
- **`/api/health`가 adb 서버 버전을 보고** — 머신에 다른 버전의 adb가 있으면 서로 서버를 죽이고
  무선 연결이 전부 끊깁니다. 41 미만이면 설명 문구가 함께 나옵니다.
- **테스트 스위트 4종, 232개 검증** — 기기 없이 돌아갑니다. 각 스위트는 별도 프로세스에서
  임시 디렉터리를 cwd로 잡고 돌아 서로 오염되지 않습니다. `cli.py`는 실제 서브프로세스로 띄워
  검증합니다.
- **문서** — README를 한국어/영어로 분리([README.en.md](README.en.md)),
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 설계 배경과 한계,
  [NOTICE.md](NOTICE.md)에 번들된 서드파티 구성요소를 정리했습니다.

### 변경 (Changed)

- **기기 정보 캐시** — 대시보드가 2초마다 폴링하는데 기기 하나를 adb로 캐묻는 데 실측
  0.8~2.6초가 걸렸습니다. 모델·해상도·SDK는 연결 중 바뀌지 않으니 한 번만 읽고 배터리(20초)·
  IP(60초)만 짧게 캐시합니다. **폴링 응답 0.8~2.6초 → 약 25ms.** 최신값은 `?refresh=1`.
- **입력이 셸을 거치지 않습니다** — 좌표·키코드를 정수로 파싱해 adb에 argv로 넘깁니다.
  APK 설치 경로도 같습니다.
- **스트림 포트 기본값 8000 → 8010** — 8000은 언리얼 에디터·Django·`python -m http.server`가
  흔히 쓰고, Windows에서는 `127.0.0.1:8000`에 붙은 프로세스가 `::`에 붙은 쪽을 이깁니다.
  그러면 대시보드가 미러링 프레임에 남의 에러 페이지를 띄워 원인 찾기가 어려워집니다.
- **기동 시 포트 점유 검사** — 이미 쓰이고 있으면 원시 `OSError` 대신, 누가 쥐고 있는지
  찾는 명령까지 알려주고 멈춥니다. 호스트·포트는 `DEVICE_FARM_HOST`/`DEVICE_FARM_PORT`.
- **ws-scrcpy 의존성 정리** — 이 팜이 쓰지 않는 iOS/Appium 경로와 브라우저 내 셸을 빌드에서
  제외했습니다. **878개 → 446개 패키지, 보안 권고 54건(critical 2, high 17) → 0건.**
  `node-pty`(Node 24에서 네이티브 빌드 실패)가 빠져 별도 빌드 도구 없이 설치됩니다.
- **점유 강제 범위를 나눴습니다** — 실시간 WebSocket 조작은 막지 않고 점유자만 표시합니다.
  되돌릴 수 없는 HTTP 동작(입력 API·앱 제어·배치 전부)만 lease를 강제합니다.

### 수정 (Fixed)

- **adb 탐색이 PATH를 무시하던 문제** — 번들 adb가 없으면 조작이 전부 실패하는데 대시보드는
  살아 있는 것처럼 보였습니다. `scrcpy_bin/` → PATH 순으로 찾고 실제 경로를 `/api/health`에
  노출합니다.
- **무선 연결 기기가 목록에서 통째로 빠지던 문제** — 존재하지 않는 adbutils API를 호출하고
  있었습니다. IP 형식 시리얼을 인식하고 `wlan0` 조회로 보완합니다.
- **logcat 세션이 죽으면 영구히 막히던 문제** — `capturing: true`인 채로 남아 "Already capturing"만
  반복했습니다. 종료 상태를 따로 표시하고 파이프를 회수합니다.
- **`cli.py`의 입력 계열 명령 전체가 동작하지 않던 문제** — 인자 처리가 다른 서브커맨드의
  네임스페이스를 건드리고 있었습니다. 이 버그 때문에 CLI는 실제 서브프로세스로 테스트합니다.
- **HTTP 입력이 매크로에 녹화되지 않던 문제** — 조용히 `count: 0`짜리 매크로가 저장됐습니다.
- **cp949(한국어 Windows) 인코딩 크래시** — scrcpy 창 제목의 이모지 한 글자가 오디오
  엔드포인트를 죽였습니다. 표준 출력을 UTF-8로 재설정하고 제목을 ASCII로 바꿨습니다.
- **`/api/wireless`가 성공을 실패로 보고하던 문제** — `tcpip`은 스스로 자기 연결을 끊습니다.
  이제 그 끊김을 정상으로 보고 기기가 다시 목록에 뜰 때까지 재시도합니다.
- **`npm ci`가 아예 거부되던 문제** — upstream의 `package-lock.json`이 자기 `package.json`과
  어긋나 있었습니다. 재생성했습니다.
- **대시보드 인라인 스크립트 손상** — 자동 포매터가 템플릿 리터럴과 태그를 망가뜨려 브라우저에서
  6개 기능이 죽어 있었습니다.

### 제거 (Removed)

- 스탠드에그 사내 게임 로그·크래시 덤프 등 공개 저장소에 있으면 안 되는 산출물 일체.
- `debug_adb.py` — 일회성 진단 스크립트.
- ws-scrcpy의 매크로 컨트롤러 — 동작하지 않는 껍데기였습니다. PiP로 대체.

### 알려진 한계 (Known limitations)

안드로이드 전용, 인증은 공유 비밀 하나(사용자별 계정 아님), `adb shell input text`가 ASCII만
받아 한글·이모지 입력 불가, 매크로는 좌표 기반이라 화면비가 다르면 어긋남, PiP는 Chromium 계열
전용. 자세한 내용은 [README](README.md#알려진-한계)에 있습니다.

---

<details>
<summary><b>English</b></summary>

## [1.0.0] — 2025-12-28

First public release. The farm is now usable the same way from a browser, a terminal and a CI job.

### Added

- **Access token** — set `DEVICE_FARM_TOKEN` and the whole API requires `X-Farm-Token`.
  Only the dashboard page and `/api/health` stay open (the page has to load before it can ask
  for the token, and monitoring should not need the secret to check the farm is alive).
  WebSockets take it in the query string; thumbnails and log downloads go through `fetch` + blob
  so a URL polled every two seconds never carries the secret.
- **Picture-in-Picture** — float the mirror in an always-on-top window, several at once.
  Replaces the ws-scrcpy toolbar's non-functional macro button.
- **`POST /api/device/{serial}/reset-stream`** — clear a leftover scrcpy server process on the
  device. Matched by scrcpy class rather than the `app_process` name, which would take unrelated
  apps with it.
- **`GET /api/config`** — tells the dashboard the stream port. The port is defined once, in
  `ws-scrcpy.config.json`, and both `server.py` and ws-scrcpy read that same file.
- **Lease persistence** — `device_leases.json` survives a restart. Leases that expired while the
  server was down are not restored.
- **Device state surfacing** — devices waiting for USB approval or offline stay in the list with
  the reason (`state_hint`) instead of vanishing.
- **`POST /api/batch/install`** — install an APK across several devices at once.
- **`DELETE /api/macros/{name}`** — delete a macro; names containing path traversal are rejected.
- **logcat to file (`to_file`) and full download** — for runs longer than the 20k-line in-memory
  buffer.
- **`analyze_logs.py`** — level distribution, top tags and crash locations out of a capture.
  Exits 2 if any crash was found.
- **`/api/health` reports the adb server version** — a second adb at a different version keeps
  restarting the server and every wireless device dies with it. Below 41 the response explains why.
- **4 test suites, 232 assertions** — no device needed. Each suite runs in its own process with a
  temporary cwd, so they cannot contaminate each other. `cli.py` is driven as a real subprocess.
- **Docs** — README split by language ([README.en.md](README.en.md)), design reasoning and limits
  in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), bundled third-party components in
  [NOTICE.md](NOTICE.md).

### Changed

- **Device details are cached** — the dashboard polls every two seconds, and interrogating one
  device over adb measured 0.8–2.6s. Model, resolution and SDK cannot change while a device stays
  attached, so they are read once; battery (20s) and IP (60s) are cached briefly.
  **Poll responses went from 0.8–2.6s to about 25ms.** `?refresh=1` forces a re-read.
- **Input never goes through a shell** — coordinates and keycodes are parsed as integers and passed
  to adb as argv. Same for the APK install path.
- **Stream port default 8000 → 8010** — Unreal Editor, Django and `python -m http.server` all like
  8000, and on Windows a process bound to `127.0.0.1:8000` beats one bound to `::`. The dashboard
  then frames that other program's error page and the failure looks like broken mirroring.
- **Port check at startup** — if the port is taken the server prints the command to find out who
  holds it and stops, instead of a raw `OSError`. Host and port via `DEVICE_FARM_HOST` /
  `DEVICE_FARM_PORT`.
- **ws-scrcpy dependencies trimmed** — the iOS/Appium path and the in-browser shell are excluded
  from the build. **878 → 446 packages, 54 security advisories (2 critical, 17 high) → 0.**
  Dropping `node-pty` (native build fails on Node 24) means no build toolchain is needed to install.
- **Lease enforcement split by reversibility** — live WebSocket control is not blocked, only
  attributed. Irreversible HTTP actions (the input API, app control, every batch endpoint) enforce it.

### Fixed

- **adb lookup ignored PATH** — without the bundled adb every control action failed while the
  dashboard still looked alive. Now `scrcpy_bin/` → PATH, with the resolved path in `/api/health`.
- **Wireless devices dropped from the list entirely** — a non-existent adbutils API was being
  called. IP-form serials are recognised, with a `wlan0` lookup as backup.
- **A dead logcat session blocked a device permanently** — it stayed `capturing: true` and only
  ever answered "Already capturing". The ended state is now tracked and the pipe reaped.
- **Every input command in `cli.py` was broken** — argument handling reached into other
  subcommands' namespaces. This bug is why the CLI is tested as a real subprocess.
- **HTTP input was not recorded into macros** — a macro with `count: 0` was saved silently.
- **cp949 (Korean Windows) encoding crash** — one emoji in the scrcpy window title killed the audio
  endpoint. stdout is reconfigured to UTF-8 and the title is ASCII.
- **`/api/wireless` reported success as failure** — `tcpip` drops its own connection. That drop is
  now expected, and the call retries until the device is listed again.
- **`npm ci` refused to run at all** — upstream's `package-lock.json` did not match its own
  `package.json`. Regenerated.
- **Dashboard inline script corruption** — an autoformatter mangled template literals and tags,
  leaving six features dead in the browser.

### Removed

- All internal game logs and crash dumps that had no business in a public repository.
- `debug_adb.py` — a one-off diagnostic script.
- ws-scrcpy's macro controller — a non-functional shell, replaced by PiP.

</details>

---

## [0.2.0] — 2025-12-24

- logcat 수집과 크래시 자동 감지 (`FATAL EXCEPTION`, ANR, 네이티브 시그널)
- 다중 기기 배치 작업 (입력·앱 제어·매크로) — 기기별 성공/실패를 따로 보고
- 앱 제어: 실행·강제종료·데이터 초기화
- 매크로 좌표 스케일링 — 녹화 해상도를 저장해 다른 해상도 기기에서도 재생

## [0.1.0] — 2025-12-21

- 기기 대시보드, ws-scrcpy 미러링, 탭·스와이프·키 입력
- 점유(lease) 모델과 TTL, APK 설치, 매크로 녹화/재생
- 무선 디버깅 전환, 오디오 포워딩, `cli.py`

[1.0.0]: https://github.com/kimyeongseong/qa-device-farm/releases/tag/v1.0.0
