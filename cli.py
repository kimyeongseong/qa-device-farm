"""Command-line client for the QA Device Farm.

Lets a terminal or a CI job use the farm without a browser:

    python cli.py devices
    python cli.py health
    python cli.py occupy --owner ci-smoke --ttl 300
    python cli.py tap    --serial R3CN30 --x 540 --y 1200
    python cli.py release --serial R3CN30 --owner ci-smoke

`occupy` without --serial takes any free device and prints its serial, which is
what a pipeline wants: ask for an Android, get one, run, hand it back.

Emulators (AVDs) are farm devices too. Boot one, hold it, run, shut it down:

    python cli.py avd-start --name Pixel_6_API_34 --wait --owner ci-smoke
    python cli.py avd-stop  --name Pixel_6_API_34 --owner ci-smoke
"""

import argparse
import json
import os
import sys

import requests

DEFAULT_BASE = "http://localhost:8001"

# Set DEVICE_FARM_TOKEN when the farm requires one; --token overrides it. Taking
# it from the environment keeps the secret out of CI logs and shell history.
TOKEN = os.environ.get("DEVICE_FARM_TOKEN", "").strip()

def auth_headers():
    return {"X-Farm-Token": TOKEN} if TOKEN else {}


def split_serials(raw):
    serials = [s.strip() for s in raw.split(",") if s.strip()]
    if not serials:
        sys.exit("--serials is empty")
    return serials


def call(base, method, path, payload=None, timeout=30):
    url = f"{base}{path}"
    try:
        resp = requests.request(method, url, json=payload,
                                headers=auth_headers(), timeout=timeout)
    except requests.RequestException as e:
        sys.exit(f"cannot reach farm at {base}: {e}")

    try:
        body = resp.json()
    except ValueError:
        sys.exit(f"{resp.status_code} {resp.text[:200]}")

    print(json.dumps(body, indent=2, ensure_ascii=False))
    # A held device (409) is a normal pipeline outcome, so report it as failure
    # without a stack trace. A batch that only partly succeeded answers 200 but
    # is still a failed step as far as CI is concerned.
    if not resp.ok or (isinstance(body, dict) and body.get("status") == "partial"):
        sys.exit(1)
    return body


def main():
    p = argparse.ArgumentParser(description="QA Device Farm CLI")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"farm URL (default {DEFAULT_BASE})")
    p.add_argument("--token", help="access token (default: $DEVICE_FARM_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list connected devices")
    sub.add_parser("health", help="server and adb status")
    sub.add_parser("leases", help="who holds what")

    occupy = sub.add_parser("occupy", help="claim a device")
    occupy.add_argument("--serial", help="specific device; omit to take any free one")
    occupy.add_argument("--owner", required=True)
    occupy.add_argument("--ttl", type=int, default=600, help="lease seconds (default 600)")
    occupy.add_argument("--kind", choices=["physical", "virtual"],
                        help="with no --serial: only real phones, or only emulators")

    release = sub.add_parser("release", help="hand a device back")
    release.add_argument("--serial", required=True)
    release.add_argument("--owner", required=True)

    tap = sub.add_parser("tap")
    tap.add_argument("--serial", required=True)
    tap.add_argument("--x", type=int, required=True)
    tap.add_argument("--y", type=int, required=True)
    tap.add_argument("--owner")

    swipe = sub.add_parser("swipe")
    swipe.add_argument("--serial", required=True)
    swipe.add_argument("--x1", type=int, required=True)
    swipe.add_argument("--y1", type=int, required=True)
    swipe.add_argument("--x2", type=int, required=True)
    swipe.add_argument("--y2", type=int, required=True)
    swipe.add_argument("--duration", type=int, default=300)
    swipe.add_argument("--owner")

    key = sub.add_parser("key")
    key.add_argument("--serial", required=True)
    key.add_argument("--keycode", type=int, required=True)
    key.add_argument("--owner")

    text = sub.add_parser("text", help="type ASCII text")
    text.add_argument("--serial", required=True)
    text.add_argument("--value", required=True)
    text.add_argument("--owner")

    shot = sub.add_parser("screenshot")
    shot.add_argument("--serial", required=True)
    shot.add_argument("--out", default="screenshot.jpg")

    appc = sub.add_parser("app", help="launch / stop / clear an app")
    appc.add_argument("--serial", required=True)
    appc.add_argument("--action", required=True, choices=["launch", "stop", "clear"])
    appc.add_argument("--package", required=True)
    appc.add_argument("--owner")

    sub.add_parser("macros", help="list saved macros")

    macrm = sub.add_parser("macro-delete")
    macrm.add_argument("--name", required=True)

    log = sub.add_parser("logcat", help="capture device logs")
    log.add_argument("action", choices=["start", "stop", "tail", "save", "status"])
    log.add_argument("--serial")
    log.add_argument("--level", default="V", choices=["V", "D", "I", "W", "E", "F"])
    log.add_argument("--contains", help="only lines containing this text")
    log.add_argument("--lines", type=int, default=200)
    log.add_argument("--out", default=None, help="file for `save` (default logcat_<serial>.txt)")
    log.add_argument("--owner")
    log.add_argument("--to-file", action="store_true",
                     help="also write the capture to logs/ on the server "
                          "(the memory buffer is capped and drops old lines)")

    # Batch verbs take --serials as a comma-separated list.
    bapp = sub.add_parser("batch-app")
    bapp.add_argument("--serials", required=True)
    bapp.add_argument("--action", required=True, choices=["launch", "stop", "clear"])
    bapp.add_argument("--package", required=True)
    bapp.add_argument("--owner")

    bmac = sub.add_parser("batch-macro")
    bmac.add_argument("--serials", required=True)
    bmac.add_argument("--name", required=True)
    bmac.add_argument("--count", type=int, default=1)
    bmac.add_argument("--owner")

    bins = sub.add_parser("batch-install")
    bins.add_argument("--serials", required=True)
    bins.add_argument("--apk", required=True)
    bins.add_argument("--owner")

    sub.add_parser("avds", help="list emulators (AVDs) and whether they are running")
    sub.add_parser("avd-images", help="system images installed in the SDK")
    sub.add_parser("avd-profiles", help="hardware profiles for avd-create --device")

    avd_new = sub.add_parser("avd-create", help="define a new emulator")
    avd_new.add_argument("--name", required=True)
    avd_new.add_argument("--image", required=True,
                         help="e.g. 'system-images;android-34;google_apis;x86_64'")
    avd_new.add_argument("--device", help="hardware profile, e.g. pixel_6")

    avd_up = sub.add_parser("avd-start", help="boot an emulator")
    avd_up.add_argument("--name", required=True)
    avd_up.add_argument("--wait", action="store_true", help="return only once Android has booted")
    avd_up.add_argument("--timeout", type=int, default=300, help="boot wait seconds (default 300)")
    avd_up.add_argument("--wipe", action="store_true", help="factory-reset before boot")
    avd_up.add_argument("--cold", action="store_true", help="cold boot, skip the quick-boot snapshot")
    avd_up.add_argument("--window", action="store_true", help="show the emulator window on the farm host")
    avd_up.add_argument("--gpu", choices=["auto", "host", "swiftshader_indirect", "angle_indirect", "guest"])
    avd_up.add_argument("--owner", help="lease the new emulator to this owner")
    avd_up.add_argument("--ttl", type=int, default=600)

    avd_down = sub.add_parser("avd-stop", help="shut an emulator down")
    avd_down.add_argument("--name", required=True)
    avd_down.add_argument("--owner")

    avd_rm = sub.add_parser("avd-delete", help="delete an emulator definition")
    avd_rm.add_argument("--name", required=True)

    a = p.parse_args()
    global TOKEN
    if a.token:
        TOKEN = a.token.strip()

    if a.cmd == "devices":
        return call(a.base, "GET", "/api/devices")
    if a.cmd == "health":
        return call(a.base, "GET", "/api/health")
    if a.cmd == "leases":
        return call(a.base, "GET", "/api/leases")

    if a.cmd == "occupy":
        body = {"owner": a.owner, "ttl_seconds": a.ttl}
        if a.kind:
            body["kind"] = a.kind
        path = f"/api/device/{a.serial}/occupy" if a.serial else "/api/devices/occupy"
        return call(a.base, "POST", path, body)

    if a.cmd == "release":
        return call(a.base, "POST", f"/api/device/{a.serial}/release", {"owner": a.owner})

    if a.cmd == "screenshot":
        url = f"{a.base}/api/device/{a.serial}/screenshot"
        resp = requests.get(url, headers=auth_headers(), timeout=30)
        if not resp.ok:
            sys.exit(f"screenshot failed: {resp.status_code}")
        with open(a.out, "wb") as f:
            f.write(resp.content)
        print(a.out)
        return None

    if a.cmd == "app":
        body = {"package": a.package}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", f"/api/app/{a.serial}/{a.action}", body)

    if a.cmd == "macros":
        return call(a.base, "GET", "/api/macros")

    if a.cmd == "avds":
        return call(a.base, "GET", "/api/avds")
    if a.cmd == "avd-images":
        return call(a.base, "GET", "/api/sdk/system-images")
    if a.cmd == "avd-profiles":
        return call(a.base, "GET", "/api/sdk/device-profiles", timeout=120)
    if a.cmd == "avd-create":
        body = {"name": a.name, "image": a.image}
        if a.device:
            body["device"] = a.device
        return call(a.base, "POST", "/api/avds", body, timeout=240)
    if a.cmd == "avd-start":
        body = {"wait": a.wait, "timeout": a.timeout, "wipe": a.wipe,
                "cold_boot": a.cold, "headless": not a.window, "ttl_seconds": a.ttl}
        if a.gpu:
            body["gpu"] = a.gpu
        if a.owner:
            body["owner"] = a.owner
        # With --wait the server holds the request until boot finishes.
        return call(a.base, "POST", f"/api/avds/{a.name}/start", body,
                    timeout=(a.timeout + 30) if a.wait else 30)
    if a.cmd == "avd-stop":
        body = {"owner": a.owner} if a.owner else {}
        return call(a.base, "POST", f"/api/avds/{a.name}/stop", body, timeout=60)
    if a.cmd == "avd-delete":
        return call(a.base, "DELETE", f"/api/avds/{a.name}", timeout=90)

    if a.cmd == "macro-delete":
        return call(a.base, "DELETE", f"/api/macros/{a.name}")

    if a.cmd == "logcat":
        if a.action == "status":
            return call(a.base, "GET", "/api/logcat")
        if not a.serial:
            sys.exit("logcat %s needs --serial" % a.action)
        if a.action == "start":
            body = {"clear": True, "level": a.level, "to_file": a.to_file}
            if a.owner:
                body["owner"] = a.owner
            return call(a.base, "POST", f"/api/logcat/{a.serial}/start", body)
        if a.action == "stop":
            return call(a.base, "POST", f"/api/logcat/{a.serial}/stop", {})
        if a.action == "save":
            resp = requests.get(f"{a.base}/api/logcat/{a.serial}/download",
                                headers=auth_headers(), timeout=60)
            if not resp.ok:
                sys.exit(f"download failed: {resp.status_code} {resp.text[:200]}")
            out = a.out or f"logcat_{a.serial}.txt"
            with open(out, "wb") as f:
                f.write(resp.content)
            print(out)
            return None
        # tail: print the log itself rather than a JSON blob, and make a crash
        # fail the step so CI notices without anyone reading the output.
        params = {"tail": a.lines}
        if a.contains:
            params["contains"] = a.contains
        resp = requests.get(f"{a.base}/api/logcat/{a.serial}", params=params,
                            headers=auth_headers(), timeout=30)
        if not resp.ok:
            sys.exit(f"not capturing on {a.serial} (start it first)")
        d = resp.json()
        for line in d["lines"]:
            print(line)
        if d["crashes"]:
            print(f"\n!! {len(d['crashes'])} crash(es) detected:", file=sys.stderr)
            for cr in d["crashes"]:
                print(f"   [{cr['kind']}] {cr['line']}", file=sys.stderr)
            sys.exit(2)
        return None

    if a.cmd == "batch-app":
        body = {"serials": split_serials(a.serials), "action": a.action, "package": a.package}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", "/api/batch/app", body)

    if a.cmd == "batch-macro":
        body = {"serials": split_serials(a.serials), "name": a.name, "count": a.count}
        if a.owner:
            body["owner"] = a.owner
        return call(a.base, "POST", "/api/batch/macro", body)

    if a.cmd == "batch-install":
        data = {"serials": ",".join(split_serials(a.serials))}
        if a.owner:
            data["owner"] = a.owner
        with open(a.apk, "rb") as fh:
            resp = requests.post(f"{a.base}/api/batch/install", data=data,
                                 files={"file": (os.path.basename(a.apk), fh)},
                                 headers=auth_headers(), timeout=600)
        body = resp.json()
        print(json.dumps(body, indent=2, ensure_ascii=False))
        # A partial batch is a failure for a pipeline, even though HTTP said 200.
        if not resp.ok or body.get("status") == "partial":
            sys.exit(1)
        return body

    # Build only the payload for the command that ran. Each input subparser
    # declares its own arguments, so touching another one's (a.x for `key`)
    # raises AttributeError before anything is sent.
    if a.cmd == "tap":
        payload = {"type": "tap", "x": a.x, "y": a.y}
    elif a.cmd == "swipe":
        payload = {"type": "swipe", "x1": a.x1, "y1": a.y1,
                   "x2": a.x2, "y2": a.y2, "duration": a.duration}
    elif a.cmd == "key":
        payload = {"type": "key", "keycode": a.keycode}
    elif a.cmd == "text":
        payload = {"type": "text", "text": a.value}
    else:
        sys.exit(f"unhandled command: {a.cmd}")

    if a.owner:
        payload["owner"] = a.owner
    return call(a.base, "POST", f"/api/device/{a.serial}/input", payload)


if __name__ == "__main__":
    main()
