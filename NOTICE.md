# Third-party components

This repository bundles software written by other people. Their licenses apply
to their files, not the MIT license in `LICENSE`.

## ws-scrcpy (`ws-scrcpy/`)

- Upstream: https://github.com/NetrisTV/ws-scrcpy
- License: MIT — Copyright (C) 2021 by Netris, JSC. See `ws-scrcpy/LICENSE`.
- Vendored at commit `2bde541`.

Local modifications, both under `ws-scrcpy/src/app/`:

| File | Change |
|---|---|
| `index.ts` | Forces the stream defaults this farm runs on (720p bound, 7 Mbps, 60 fps, 10-frame I-frame interval, fit-to-screen) instead of the upstream prompt, and instantiates `MacroController`. |
| `MacroController.ts` | New file. An in-player macro panel. **Currently disabled** — `initUI()` returns before injecting anything and the body is commented out, because macro control was moved to the main dashboard. Kept for reference. |

`ws-scrcpy/dist/` and `ws-scrcpy/node_modules/` are build output and are not
committed; run `npm install && npm run dist` inside `ws-scrcpy/` to rebuild.

## scrcpy server binary (`ws-scrcpy/vendor/Genymobile/scrcpy/`)

- Upstream: https://github.com/Genymobile/scrcpy
- License: Apache-2.0. See `ws-scrcpy/vendor/Genymobile/scrcpy/LICENSE`.

## jmuxer (`static/jmuxer.min.js`)

- Upstream: https://github.com/samirkumardas/jmuxer
- License: MIT.
