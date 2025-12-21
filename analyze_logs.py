"""Count endpoint hits in an exported server log.

Usage:
    python analyze_logs.py raw_logs.txt
"""

import re
import sys
from collections import Counter


def parse_logs(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    actions = []

    # Regex to find paths starting with /
    # The format seems to be: Date Time [ID] /path/action ...
    # We'll just look for the first token that starts with /

    for line in lines:
        match = re.search(r'(/[a-zA-Z0-9_/]+)', line)
        if match:
            actions.append(match.group(1))

    counts = Counter(actions)

    print("Action Counts Summary:")
    print("----------------------")
    for action, count in counts.most_common():
        print(f"{action}: {count}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <logfile>")
    parse_logs(sys.argv[1])
