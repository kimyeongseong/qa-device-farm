"""Run every test file and summarise.

    python tests/run_all.py

Each test file is a standalone script that prints one PASS/FAIL line per check
and exits non-zero if anything failed. They run in separate processes so one
suite's monkeypatching of the server module cannot leak into another's.

No device is required: the adb layer is faked. The paths that genuinely need
hardware -- streaming, audio, real input -- are verified by hand; see the
verification notes in the README.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SUITES = [
    ("test_leases_and_input.py", "leases, lease conflicts, TTL, input validation"),
    ("test_features.py", "macros, app control, logcat, batch, cache, wireless serials"),
    ("test_edge_cases.py", "dead capture recovery, corrupt files, odd serials"),
    ("test_cli.py", "cli.py driven as a real subprocess"),
]


def main() -> int:
    # Device output is echoed in some assertions; a cp949 console would raise
    # on it and fail a suite for the wrong reason.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    total_pass = total_fail = 0
    failed_suites = []

    for name, blurb in SUITES:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, name)],
            capture_output=True, text=True, cwd=ROOT, env=env,
            # Read the child as UTF-8 rather than the console codepage. Test
            # output carries Korean and device log lines; decoding those as
            # cp949 raised inside subprocess's reader thread and silently threw
            # away a whole suite's results while still reporting success.
            encoding="utf-8", errors="replace",
        )
        out = proc.stdout or ""
        passed = sum(1 for line in out.splitlines() if line.startswith("PASS"))
        failed = sum(1 for line in out.splitlines() if line.startswith("FAIL"))
        total_pass += passed
        total_fail += failed

        status = "ok" if proc.returncode == 0 else "FAILED"
        print(f"{name:28s} {status:7s} {passed:3d} passed, {failed} failed   ({blurb})")

        if proc.returncode != 0:
            failed_suites.append(name)
            for line in out.splitlines():
                if line.startswith("FAIL"):
                    print("    " + line)
            if proc.stderr.strip():
                print("    stderr:", proc.stderr.strip().splitlines()[-1])

    print("-" * 78)
    print(f"{total_pass} passed, {total_fail} failed across {len(SUITES)} suites")
    if failed_suites:
        print("failing suites: " + ", ".join(failed_suites))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
