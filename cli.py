"""Command-line client for the QA Device Farm.

Lets a terminal or a CI job use the farm without a browser:

    python cli.py devices
    python cli.py health
    python cli.py occupy --owner ci-smoke --ttl 300
    python cli.py tap    --serial R3CN30 --x 540 --y 1200
    python cli.py release --serial R3CN30 --owner ci-smoke

`occupy` without --serial takes any free device and prints its serial, which is
what a pipeline wants: ask for an Android, get one, run, hand it back.
"""

import argparse
import json
import sys

import requests

DEFAULT_BASE = "http://localhost:8001"


def call(base, method, path, payload=None):
    url = f"{base}{path}"
    try:
        resp = requests.request(method, url, json=payload, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"cannot reach farm at {base}: {e}")

    try:
        body = resp.json()
    except ValueError:
        sys.exit(f"{resp.status_code} {resp.text[:200]}")

    print(json.dumps(body, indent=2, ensure_ascii=False))
    # A held device (409) is a normal pipeline outcome, so report it as failure
    # without a stack trace.
    if not resp.ok:
        sys.exit(1)
    return body


def main():
    p = argparse.ArgumentParser(description="QA Device Farm CLI")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"farm URL (default {DEFAULT_BASE})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list connected devices")
    sub.add_parser("health", help="server and adb status")
    sub.add_parser("leases", help="who holds what")

    occupy = sub.add_parser("occupy", help="claim a device")
    occupy.add_argument("--serial", help="specific device; omit to take any free one")
    occupy.add_argument("--owner", required=True)
    occupy.add_argument("--ttl", type=int, default=600, help="lease seconds (default 600)")

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

    a = p.parse_args()

    if a.cmd == "devices":
        return call(a.base, "GET", "/api/devices")
    if a.cmd == "health":
        return call(a.base, "GET", "/api/health")
    if a.cmd == "leases":
        return call(a.base, "GET", "/api/leases")

    if a.cmd == "occupy":
        body = {"owner": a.owner, "ttl_seconds": a.ttl}
        path = f"/api/device/{a.serial}/occupy" if a.serial else "/api/devices/occupy"
        return call(a.base, "POST", path, body)

    if a.cmd == "release":
        return call(a.base, "POST", f"/api/device/{a.serial}/release", {"owner": a.owner})

    if a.cmd == "screenshot":
        url = f"{a.base}/api/device/{a.serial}/screenshot"
        resp = requests.get(url, timeout=30)
        if not resp.ok:
            sys.exit(f"screenshot failed: {resp.status_code}")
        with open(a.out, "wb") as f:
            f.write(resp.content)
        print(a.out)
        return None

    payloads = {
        "tap": {"type": "tap", "x": a.x, "y": a.y},
        "swipe": {"type": "swipe", "x1": a.x1, "y1": a.y1, "x2": a.x2, "y2": a.y2,
                  "duration": a.duration},
        "key": {"type": "key", "keycode": a.keycode},
        "text": {"type": "text", "text": a.value},
    }
    payload = payloads[a.cmd]
    if a.owner:
        payload["owner"] = a.owner
    return call(a.base, "POST", f"/api/device/{a.serial}/input", payload)


if __name__ == "__main__":
    main()
