# Third-party components

This repository bundles software written by other people. Their licenses apply
to their files, not the MIT license in `LICENSE`.

## ws-scrcpy (`ws-scrcpy/`)

- Upstream: https://github.com/NetrisTV/ws-scrcpy
- License: MIT — Copyright (C) 2021 by Netris, JSC. See `ws-scrcpy/LICENSE`.
- Vendored at commit `2bde541`.

### Local modifications

**Features** — under `ws-scrcpy/src/app/`:

| File | Change |
|---|---|
| `index.ts` | Forces the stream defaults this farm runs on (720p bound, 7 Mbps, 60 fps, 10-frame I-frame interval, fit-to-screen) instead of the upstream prompt, and instantiates `PipController`. |
| `PipController.ts` | New file. A floating Picture-in-Picture button, so a device stays visible while the operator works in another window and several devices can be watched at once. Hands the MSE `<video>` straight to PiP; for the canvas decoders (Broadway, TinyH264, WebCodecs, MJPEG) it bridges `canvas.captureStream()` through a hidden video, since PiP only accepts video elements. |

**Correctness** — under `ws-scrcpy/src/server/`:

| File | Change |
|---|---|
| `goog-device/services/ControlCenter.ts` | Collapses the two adb transports of one device onto a single entry. Android 11+ wireless debugging advertises over mDNS, so adb reports the same phone as both `HA2F2NVC` and `adb-HA2F2NVC-<suffix>._adb-tls-connect._tcp`; upstream tracked each separately and started a scrcpy server over both, and the second died with `java.net.BindException: Address already in use` on device port 8886. Which transport won varied per run, so the picture could arrive from one session while control went to the one that had already exited. The mDNS name is kept only when the plain serial is absent — with the cable out it is the only way in. |

**Build repair** — upstream at this commit cannot be installed or built on a
current Node. Every change below exists to fix that, and each one is scoped to
code this farm does not ship:

| File | Change | Why |
|---|---|---|
| `build.config.override.json` | `INCLUDE_APPL: false`, `INCLUDE_ADB_SHELL: false` | This farm is Android-only and exposes device shells through its own API, so neither the iOS/Appium server nor the in-browser xterm shell is built. |
| `package.json` | Dropped `node-pty`, `xterm`, `xterm-addon-attach`, `xterm-addon-fit`, `ios-device-lib`, `appium-xcuitest-driver` | `node-pty@0.10` (2021) needs a native build that fails on Node 24, which made `npm install` impossible. The rest belong to the two features switched off above. `appium-xcuitest-driver`, hidden in `optionalDependencies`, was the sole source of every remaining advisory: removing these took the dependency tree from 878 packages / 54 advisories (2 critical, 17 high) to 446 packages / 0. |
| `package-lock.json` | Regenerated | Upstream's lock did not match its own `package.json`, so `npm ci` refused to run at all. |
| `webpack/ws-scrcpy.common.ts` | `ts-loader` gets `transpileOnly: true` | ts-loader type-checks the whole program from `tsconfig.json`'s `include`, which covers the iOS server that `INCLUDE_APPL: false` already strips from the bundle. Its five pre-existing type errors made webpack's production mode emit nothing, so `npm run dist` produced an empty `dist/`. `npm run lint` still type-lints the code this project owns. |

`MacroController.ts` was removed; it was a disabled in-player macro panel whose
body was entirely commented out, and macro control lives on the dashboard.

`ws-scrcpy/dist/` and `ws-scrcpy/node_modules/` are build output and are not
committed.

## scrcpy server binary (`ws-scrcpy/vendor/Genymobile/scrcpy/`)

- Upstream: https://github.com/Genymobile/scrcpy
- License: Apache-2.0. See `ws-scrcpy/vendor/Genymobile/scrcpy/LICENSE`.

## jmuxer (`static/jmuxer.min.js`)

- Upstream: https://github.com/samirkumardas/jmuxer
- License: MIT.
