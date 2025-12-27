"""Summarise a captured logcat.

    python analyze_logs.py logs/logcat_R3CN30_20251228-004055.txt

An overnight capture is tens of thousands of lines and nobody reads it top to
bottom. This answers the question you actually have after a run: did anything
crash, what was noisy, and which tags produced the errors.

Reads the format the farm writes (`adb logcat -v time`):

    08-08 00:40:57.075 E/AndroidRuntime(9911): FATAL EXCEPTION: main
"""

import argparse
import re
import sys
from collections import Counter

# `-v time` lines look like: MM-DD HH:MM:SS.mmm L/Tag(pid): message
LINE_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<level>[VDIWEF])/(?P<tag>[^(]+?)\s*\(\s*(?P<pid>\d+)\s*\):\s?(?P<msg>.*)$"
)

LEVEL_NAMES = {"V": "Verbose", "D": "Debug", "I": "Info",
               "W": "Warn", "E": "Error", "F": "Fatal"}

# Same signals the server flags live, kept in step with server.CRASH_PATTERNS.
CRASH_PATTERNS = [
    ("java crash", re.compile(r"FATAL EXCEPTION")),
    ("native crash", re.compile(r"F/libc|Fatal signal \d+ \(SIG")),
    ("ANR", re.compile(r"ANR in ")),
]


def classify(line):
    for label, pattern in CRASH_PATTERNS:
        if pattern.search(line):
            return label
    return None


def analyse(path, top):
    levels = Counter()
    tags_by_level = {"W": Counter(), "E": Counter(), "F": Counter()}
    crashes = []
    unparsed = total = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            total += 1

            kind = classify(line)
            if kind:
                crashes.append((lineno, kind, line.strip()))

            m = LINE_RE.match(line)
            if not m:
                # Buffer markers ("--------- beginning of main") and wrapped
                # stack traces land here; they are counted, not treated as noise
                # to hide.
                unparsed += 1
                continue
            level = m.group("level")
            levels[level] += 1
            if level in tags_by_level:
                tags_by_level[level][m.group("tag").strip()] += 1

    print(f"{path}")
    print(f"  {total} lines, {unparsed} without a standard header "
          f"(buffer markers and stack-trace continuations)")

    print("\n레벨별")
    for level in "FEWIDV":
        if levels[level]:
            print(f"  {level} {LEVEL_NAMES[level]:8s} {levels[level]:>7}")

    for level, title in (("F", "Fatal"), ("E", "Error"), ("W", "Warn")):
        counts = tags_by_level[level]
        if not counts:
            continue
        print(f"\n{title} 상위 태그")
        for tag, n in counts.most_common(top):
            print(f"  {n:>6}  {tag}")

    print("\n크래시")
    if not crashes:
        print("  없음")
    else:
        for lineno, kind, line in crashes:
            print(f"  line {lineno:>6}  [{kind}] {line[:110]}")

    # A crash makes this a failed run; useful straight from a CI step.
    return 2 if crashes else 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("logfile", help="a logcat capture written by the farm")
    p.add_argument("--top", type=int, default=10, help="tags to list per level (default 10)")
    a = p.parse_args()
    try:
        return analyse(a.logfile, a.top)
    except FileNotFoundError:
        sys.exit(f"no such file: {a.logfile}")


if __name__ == "__main__":
    sys.exit(main())
