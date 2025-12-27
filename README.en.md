# QA Device Farm

[한국어](README.md) · English

**[MIT License](LICENSE)** — Kim Yeongseong &lt;cds04130@kakao.com&gt;
Bundled third-party components are listed in [NOTICE.md](NOTICE.md); per-release changes in [CHANGELOG.md](CHANGELOG.md).

A self-hosted Android device farm: share the real phones plugged into one PC through a browser dashboard and a single HTTP API.

It exists because of how QA actually goes wrong when devices live on individual desks — you have to walk over to use one, you have no idea who else is on it, and every automation script only runs on the machine it was written on.

```
Browser (dashboard)          CLI / CI pipeline
        │                          │
        └──────────┬───────────────┘
                   ▼
        server.py  (FastAPI, :8001)
        device list · leases · input · install · macros · screenshots
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   adb (adbutils)      ws-scrcpy (:8010)
        │               low-latency H.264 mirroring
        ▼
   Android devices over USB / Wi-Fi
```

---

## What it does

| | |
|---|---|
| **Device dashboard** | Model, OS, resolution, battery, RAM and IP for every attached device, with per-device aliases. Devices that are plugged in but unusable (waiting for USB approval, offline) are listed with the reason instead of vanishing. |
| **Remote control** | Tap, swipe and key input while watching the screen. Mirroring is ws-scrcpy (H.264); input goes through `adb input`. |
| **Leases** | Claim a device before driving it. Leases carry a TTL, so a CI job that dies cannot park a device forever. |
| **Batch across devices** | Tick several devices and install an APK, replay a macro or drive an app on all of them at once. Results are reported per device, and devices someone else holds are skipped rather than failed. |
| **Logcat capture + crash detection** | Per-device logcat with automatic detection of `FATAL EXCEPTION`, ANR and native signals. Filter, download, or stream to a file for long runs. |
| **Macro record / replay** | Records input with timestamps as JSON and replays it N times. The recording resolution is stored too, so **coordinates are rescaled for devices with a different screen**. |
| **App control** | Launch, force-stop, clear data — the three things you do between test runs. |
| **APK install / uninstall** | Drop an APK on a device card for `install -r`. List and remove third-party packages. |
| **Wireless debugging** | One button for `tcpip 5555` + `connect`. Survives unplugging the cable. |
| **Audio forwarding** | Device audio to the PC through the scrcpy binary. |
| **Picture-in-Picture** | Float the mirror in an always-on-top window: keep watching a device while working elsewhere, or several at once. |
| **CLI / CI** | No sessions — claim, drive, read logs and release over plain HTTP. |

---

## Getting started

**You need:** Python 3.10+, Node.js 16+, [Android platform-tools](https://developer.android.com/tools/releases/platform-tools), and an Android device with USB debugging on.

adb is taken from `scrcpy_bin/` first, then from PATH. `GET /api/health` reports the `adb_path` actually in use.

Audio forwarding needs a [scrcpy](https://github.com/Genymobile/scrcpy/releases) 2.7+ binary, which is not redistributed here for licensing reasons. Put it in `scrcpy_bin/` or on PATH. Without it only the audio button returns 503; everything else works.

```bash
git clone https://github.com/kimyeongseong/qa-device-farm.git
cd qa-device-farm

pip install -r requirements.txt

cd ws-scrcpy && npm install && npm run dist && cd ..
```

> **About the bundled ws-scrcpy** — upstream (`2bde541`) as-is neither installs
> nor builds on a current Node. This repository carries the fixes, so the
> commands above work unchanged. [NOTICE.md](NOTICE.md) has the details:
>
> - The iOS/Appium path and the in-browser shell are excluded from the build,
>   since this farm uses neither. Dependencies went 878 → 446 packages and
>   security advisories 54 (2 critical, 17 high) → **0**.
> - Dropping `node-pty` (a 2021 release whose native build fails on Node 24)
>   means no build toolchain is needed to install.
> - Upstream's `package-lock.json` did not match its own `package.json`, so
>   `npm ci` refused to run at all. Regenerated.

On Windows:

```bash
run_root_server.bat
```

Or run the two processes yourself:

```bash
python server.py
```

```bash
cd ws-scrcpy && npm start
```

- Dashboard — http://localhost:8001/
- API docs (generated) — http://localhost:8001/docs

### Access token

Open by default. Leave it that way on a trusted lab network; turn the token on before exposing the farm.

```bash
# Windows
set DEVICE_FARM_TOKEN=some-long-random-string
# Linux/mac
export DEVICE_FARM_TOKEN=some-long-random-string
```

With it set, the whole API requires an `X-Farm-Token` header (or `?token=`). The dashboard asks once and keeps it in `localStorage`; the CLI reads `DEVICE_FARM_TOKEN` or takes `--token`.

```bash
DEVICE_FARM_TOKEN=... python cli.py devices
python cli.py --token ... devices
```

Reachable without a token: the dashboard page and its static files (the page has to load before it can ask for the token), and `/api/health` — monitoring should not need the secret just to check the farm is alive.

Browsers cannot put headers on a WebSocket, so `/ws/...` takes the token in the query string. For the same reason thumbnails and log downloads are fetched and handed over as blobs: putting the token in a URL that polls every two seconds would write the secret into every access-log line.

### Ports

| Port | Process | How to change |
|---|---|---|
| 8001 | Dashboard and API (`server.py`) | `DEVICE_FARM_PORT` env var (`DEVICE_FARM_HOST` for the interface) |
| 8010 | Stream server (ws-scrcpy) | **`ws-scrcpy.config.json`** |

If the port is already taken the server refuses to start and prints the command to find out who holds it, rather than a raw `OSError` traceback.

The stream port is defined in `ws-scrcpy.config.json` and **nowhere else**. ws-scrcpy reads it through `WS_SCRCPY_CONFIG`; `server.py` reads the same file and tells the dashboard through `GET /api/config`.

ws-scrcpy's own default of 8000 is deliberately avoided. Unreal Editor, Django and `python -m http.server` all like that port, and **on Windows a process bound to `127.0.0.1:8000` beats one bound to `::`**. The dashboard then frames that other program's error page and the failure looks like broken mirroring. This happened during development.

**When the mirror does not appear**: the stream window shows the address it is actually connecting to. Check whether something else holds that port.

```bash
Get-NetTCPConnection -LocalPort 8010 -State Listen | ForEach-Object { Get-Process -Id $_.OwningProcess }
```

If it is taken, change `port` in `ws-scrcpy.config.json` and restart both servers.

**When the mirror is black, or the log says `unknown host service`**: restarting ws-scrcpy does not always take down the scrcpy server it pushed onto the device, and the leftover process holds the port so the next connection gets nothing.

```bash
adb -s <serial> shell "ps -A | grep app_process"
```

The **[스트림 정리 / Reset stream]** button on each device card does this for you (`POST /api/device/{serial}/reset-stream`). Killing by the name `app_process` alone would take unrelated apps with it, so the cleanup matches the scrcpy class instead. Common after unplugging and replugging a device.

**When wireless keeps dropping — adb version conflict**: if the machine has more than one adb binary at different versions, each kills the other's server.

```
adb server version (40) doesn't match this client (41); killing...
```

A wireless connection made with `adb connect` is **server state**, so it disappears every time the server restarts. USB devices come straight back, which makes it look like only wireless is flaky. During development SuperDisplay kept starting its own adb 1.0.40 service and caused exactly this.

```bash
Get-CimInstance Win32_Process -Filter "Name='adb.exe'" | Select-Object ProcessId, ExecutablePath
```

If several paths show up, settle on one. Any other program shipping its own adb (screen-mirroring utilities and the like) has to be stopped or aligned to the same version for wireless to hold.

### Mirroring without Node

`server.py` can mirror on its own, without ws-scrcpy: it pipes H.264 from `adb exec-out screenrecord` over a WebSocket and decodes it in the browser with jmuxer.

```
http://localhost:8001/control?serial=<serial>&model=<model>
```

Measured on a Lenovo TB373FU over USB: full 2944×1840 resolution, about **1.3 seconds** from action to visible change (app launch time included). Fine for watching a device; the ws-scrcpy path is better when you need the screen to answer immediately.

Use it where Node cannot be installed, or when the stream server is down. The **[간이 미러링 / Simple mirror]** button on each device card opens it.

To preset device aliases, copy `device_aliases.example.json` to `device_aliases.json` and edit. That file holds real serials, so it is gitignored.

---

## Using the CLI

Everything the dashboard does, from a terminal or a CI job.

```bash
python cli.py health
python cli.py devices

# claim any free device (prints the serial)
python cli.py occupy --owner ci-smoke --ttl 300

python cli.py tap --serial R3CN30ABCDE --x 540 --y 1200 --owner ci-smoke
python cli.py screenshot --serial R3CN30ABCDE --out shot.jpg

python cli.py release --serial R3CN30ABCDE --owner ci-smoke
```

If someone else holds the device you get a `409` and exit code 1, so a pipeline can branch on it directly.

### A smoke run

```bash
SERIAL=$(python cli.py occupy --owner ci --ttl 600 | python -c 'import sys,json;print(json.load(sys.stdin)["serial"])')

python cli.py logcat start --serial $SERIAL --owner ci
python cli.py app --serial $SERIAL --action clear  --package com.example.app --owner ci
python cli.py app --serial $SERIAL --action launch --package com.example.app --owner ci
python cli.py batch-macro --serials $SERIAL --name login_flow --owner ci

# exit code 2 if any crash was seen -> the CI step fails
python cli.py logcat tail --serial $SERIAL --lines 200

python cli.py logcat save --serial $SERIAL --out artifacts/logcat.txt
python cli.py logcat stop --serial $SERIAL
python cli.py release --serial $SERIAL --owner ci
```

### Several devices at once

```bash
python cli.py batch-install --serials R3CN30ABCDE,HA1EJ0000,9A271FFAZ0 --apk build/app-debug.apk
```

```bash
python cli.py batch-macro --serials R3CN30ABCDE,HA1EJ0000 --name login_flow --count 3
```

Batch commands report per device. If any device fails or is held by someone else the call is `status: partial` and the CLI exits 1.

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

The full spec is generated at `http://localhost:8001/docs`. The ones you will actually use:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Server and adb status, device counts, adb path and server version (no token needed) |
| `GET` | `/api/config` | What the dashboard cannot infer (stream port) |
| `GET` | `/api/devices` | Device list with lease state and `state`/`state_hint`. `?refresh=1` bypasses the cache |
| `GET` | `/api/device/{serial}/screenshot` | JPEG screenshot |
| `GET` | `/api/info/{serial}` | Details (manufacturer, CPU, current app, IP) |
| `POST` | `/api/devices/occupy` | Claim any free device |
| `POST` | `/api/device/{serial}/occupy` | Claim a specific device |
| `POST` | `/api/device/{serial}/release` | Hand it back |
| `GET` | `/api/leases` | Who holds what |
| `POST` | `/api/device/{serial}/input` | tap / swipe / key / text |
| `POST` | `/api/install/{serial}` | Upload and install an APK |
| `GET` | `/api/packages/{serial}` | Third-party packages |
| `POST` | `/api/uninstall/{serial}` | Remove a package |
| `POST` | `/api/app/{serial}/{action}` | `launch` / `stop` / `clear` |
| `POST` | `/api/alias/{serial}` | Set a device alias (empty resets to the model name) |
| `POST` | `/api/audio/start/{serial}` | Device audio to the PC (needs the scrcpy binary) |
| `POST` | `/api/audio/stop/{serial}` | Stop audio |
| `POST` | `/api/wireless/{serial}` | Switch to wireless debugging |
| `POST` | `/api/usb/{serial}` | Switch back to USB |
| `POST` | `/api/device/{serial}/reset-stream` | Clear a leftover stream process on the device |
| `POST` | `/api/macros/start_record/{serial}` | Start recording (stores the resolution too) |
| `POST` | `/api/macros/stop_record/{serial}` | Stop and save under a name |
| `POST` | `/api/macros/play/{serial}` | Replay (loop count, coordinates rescaled) |
| `GET` | `/api/macros` | Macro list with step count and recording resolution |
| `DELETE` | `/api/macros/{name}` | Delete a macro |
| `POST` | `/api/logcat/{serial}/start` | Start capture (`level`, `clear`, `to_file`) |
| `POST` | `/api/logcat/{serial}/stop` | Stop capture |
| `GET` | `/api/logcat/{serial}` | Buffer (`tail`, `contains`) plus detected crashes |
| `GET` | `/api/logcat/{serial}/download` | Whole buffer as a text file |
| `GET` | `/api/logcat` | Which devices are being captured, and crash counts |
| `POST` | `/api/batch/input` | Input on several devices |
| `POST` | `/api/batch/app` | App control on several devices |
| `POST` | `/api/batch/macro` | Macro replay on several devices |
| `POST` | `/api/batch/install` | APK install on several devices |
| `WS` | `/ws/video/{serial}` | screenrecord H.264 stream |
| `WS` | `/ws/control/{serial}` | Live input channel |

Claiming a device:

```bash
curl -X POST localhost:8001/api/devices/occupy \
  -H 'Content-Type: application/json' \
  -d '{"owner":"ci-smoke","ttl_seconds":300}'
```

```json
{ "status": "success", "serial": "R3CN30ABCDE", "owner": "ci-smoke", "expires_at": 1786000000.0 }
```

### Reading a capture back

A long capture is tens of thousands of lines and nobody reads it top to bottom.

```bash
python analyze_logs.py logs/logcat_R3CN30_20251228-004055.txt
```

```
2264 lines, 2 without a standard header

by level          top Fatal tags        crashes
  F Fatal      1     1  AndroidRuntime     line 1604  [java crash] F/AndroidRuntime(22117): FATAL EXCEPTION: main
  E Error     46   top Error tags
  W Warn       1    37  HfLooper
  D Debug   2183     9  mtk_storageproxyd
```

Exits 2 if any crash was found, so it drops straight into a CI step.

---

## Tests

No device required — the adb layer is faked, so the whole surface is exercised without a phone.

```bash
pip install -r requirements-dev.txt
python tests/run_all.py
```

```
test_leases_and_input.py     ok       26 passed, 0 failed
test_features.py             ok      167 passed, 0 failed
test_edge_cases.py           ok       15 passed, 0 failed
test_cli.py                  ok       24 passed, 0 failed
232 passed, 0 failed across 4 suites
```

Each suite runs in its own process with a temporary directory as cwd, so one suite's monkeypatching and runtime state (`device_leases.json`, `macros/`) cannot leak into another, and the working tree stays clean.

What is covered: lease conflicts, TTL expiry and pool exhaustion; input injection rejection; macro rescaling and v1 compatibility; path traversal; app-control argv; crash pattern matching; logcat dead-session recovery and file capture; batch partial-failure isolation; four kinds of wireless serial; the device cache; lease persistence; the access-token boundary; selective cleanup of leftover stream processes; and every CLI subcommand.

`cli.py` is driven as a real subprocess, because an argument-handling bug lived there that no in-process test could see.

**Anything that genuinely needs hardware** is not automated: mirroring quality and latency, audio, real crash detection, the wireless switch. Those were verified by hand on real devices.

---

## Design notes

- **No sessions.** Driving a device does not open and close one. One action is one HTTP request, and the server holds no per-device session between them. The only state kept is the lease and a device-detail cache for polling.
- **Leases expire.** A TTL means a device frees itself even when whoever claimed it dies.
- **Enforcement is split by reversibility.** Live WebSocket control is not blocked — the dashboard only shows who holds the device, because being locked out of *looking* at a screen in a hurry is worse. Irreversible HTTP actions (the input API, app control, every batch endpoint) do enforce the lease: wiping app data on a device mid-CI-run destroys that run.
- **Batch admits partial failure.** Run against ten devices and one will be unplugged or held. Rather than failing the call, each device gets `success` / `error` / `skipped`, and anything less than clean is reported as `partial`.
- **Input never goes through a shell.** Coordinates and keycodes are parsed as integers and passed to adb as argv. No string is assembled and handed to a shell.
- **Device details are cached.** The dashboard polls every two seconds, and interrogating one device over adb measured 0.8–2.6s on real hardware — slower than the poll itself, and growing with every device added. Model, resolution and SDK cannot change while a device stays attached, so they are read once; battery (20s) and IP (60s) are cached briefly. Poll responses went from **0.8–2.6s to about 25ms**. `?refresh=1` forces a re-read.
- **Leases are persisted.** `device_leases.json` survives a restart, which matters precisely when a restart happens: the CI job holding a device is still running, and only the farm would think that device is free. Leases that expired while the server was down are not restored.
- **`/api/health` reports the adb server version.** A second adb at a different version on the machine keeps restarting the server, and every wireless device dies with it — invisible from the client path alone. Below version 41 the response carries an explanation.
- **Screenshots are serialised per device.** Concurrent screenshot requests to one device make adb unstable, so an `asyncio.Lock` per device lets one through at a time.

The reasoning and the boundaries are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (Korean).

---

## Known limits

- **Android only.** No iOS.
- **Authentication is optional and coarse.** Without `DEVICE_FARM_TOKEN`, anyone who can reach the port can drive the devices and install APKs. Fine on an internal network; turn it on before exposing the farm. It is one shared secret, not per-user accounts.
- **No Korean or emoji input.** `adb shell input text` is ASCII only; the device needs its own IME.
- **Macros are still coordinate-based.** Resolution differences are corrected by proportional scaling, but devices with a different aspect ratio or a re-flowed layout (foldables, tablets) will not match. It does not find UI elements. Macros recorded before this feature have no resolution and replay unscaled.
- **The in-memory log buffer is 20k lines per device** and is lost on restart. For long runs enable `to_file` when starting the capture; the full log lands in `logs/`.
- **PiP is Chromium-only.** Firefox has no such API, so the button does not appear at all. Browser policy requires a real user click to enter it.

---

## Credits

The lease model and the session-less control API follow [Toss's device farm (Nebula) article](https://toss.tech/article/device-farm-nebula), scaled down to what this project needs. Nebula's distributed locking, agent layer, iOS mirroring and custom driver are out of scope here.
